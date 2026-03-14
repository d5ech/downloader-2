"""
tasks.py — RQ task definitions executed by the worker pool.

Pipeline
--------
Each job runs six named phases, writing its current phase to Redis so the
API can surface richer status than RQ's binary queued/started/finished/failed:

    queued → scraping → extracting → downloading → saving → complete
                                                          ↘ error

Retry strategy
--------------
RQ retries the whole task on any unhandled exception.  The only exceptions
that are NOT retried are subclasses of ``NonRetryableError`` — these represent
permanent failures (bad URL, auth error) where re-running would always fail.
The ``on_failure_callback`` enforces this by zeroing ``job.retries_left``
before RQ has a chance to re-enqueue.

The default retry schedule (3 attempts, 30/60/120 s intervals) is configured
in ``api.py`` when the job is enqueued.
"""

from __future__ import annotations

import sys
import os
import time
import traceback as tb

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.scraper import scrape_ad_url, MediaResult
from backend.downloader import download_assets
from backend.utils import get_logger, get_redis_connection, parse_ad_url, AdUrlError

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# How long (seconds) phase keys live in Redis after the job finishes.
PHASE_TTL = 86_400   # 24 h

# Redis key templates
_PHASE_KEY    = "job:{job_id}:phase"
_PROGRESS_KEY = "job:{job_id}:progress"

# RQ retry schedule — referenced by api.py when enqueueing
MAX_RETRIES     = 3
RETRY_INTERVALS = [30, 60, 120]   # seconds between successive attempts

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class WorkerError(Exception):
    """Base class for all worker pipeline errors."""


class NonRetryableError(WorkerError):
    """
    Permanent failure — retrying will never succeed.

    Examples: invalid URL, ad does not exist, access denied.
    The ``on_failure_callback`` inspects this type and zeroes the retry
    counter so RQ drops the job immediately.
    """


class ScraperError(WorkerError):
    """
    Transient scraper failure (network timeout, browser crash, etc.).

    RQ will automatically re-enqueue the job according to its retry policy.
    """


class DownloadError(WorkerError):
    """
    Transient download failure (CDN timeout, rate-limit, etc.).

    RQ will automatically re-enqueue the job according to its retry policy.
    """


# ---------------------------------------------------------------------------
# RQ callbacks
# ---------------------------------------------------------------------------


def on_failure_callback(job, connection, type, value, traceback) -> None:
    """
    Called by RQ after a job fails.

    If the exception is a :class:`NonRetryableError`, zeroes ``retries_left``
    so RQ does not re-enqueue the job, then marks the phase as
    ``error_permanent`` in Redis.

    For all other exceptions the function is a no-op; RQ will re-enqueue
    according to the ``Retry`` policy supplied at enqueue time.
    """
    if isinstance(value, NonRetryableError):
        try:
            job.retries_left = 0
            job.save()
        except Exception:
            pass   # best-effort; don't shadow the original error
        _set_phase(connection, job.id, "error_permanent")
        logger.error("[job:%s] Permanent failure (%s): %s", job.id, type.__name__, value)
    else:
        _set_phase(connection, job.id, "error")
        logger.warning(
            "[job:%s] Transient failure (%s): %s — will retry if attempts remain",
            job.id, type.__name__, value,
        )


def on_success_callback(job, connection, result, *args, **kwargs) -> None:
    """Called by RQ after a job finishes successfully."""
    _set_phase(connection, job.id, "complete")
    logger.info("[job:%s] Completed successfully", job.id)


# ---------------------------------------------------------------------------
# Phase tracking
# ---------------------------------------------------------------------------


def _set_phase(conn, job_id: str, phase: str) -> None:
    """
    Write the current pipeline phase to Redis.

    The key expires after ``PHASE_TTL`` seconds so stale entries are
    automatically cleaned up.
    """
    try:
        conn.set(_PHASE_KEY.format(job_id=job_id), phase, ex=PHASE_TTL)
        logger.debug("[job:%s] phase → %s", job_id, phase)
    except Exception as exc:
        # Never let a Redis write failure abort the job
        logger.warning("[job:%s] Could not set phase %r: %s", job_id, phase, exc)


def _set_progress(conn, job_id: str, done: int, total: int) -> None:
    """Write a simple n/total progress counter to Redis."""
    try:
        value = f"{done}/{total}"
        conn.set(_PROGRESS_KEY.format(job_id=job_id), value, ex=PHASE_TTL)
    except Exception:
        pass


def get_job_phase(job_id: str) -> str | None:
    """
    Return the current phase string for *job_id*, or ``None`` if the key
    has expired or does not exist.  Convenience helper for the API layer.
    """
    try:
        raw = get_redis_connection().get(_PHASE_KEY.format(job_id=job_id))
        return raw.decode() if isinstance(raw, bytes) else raw
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Pipeline steps
# ---------------------------------------------------------------------------


def _step_validate(url: str, job_id: str) -> str:
    """
    Validate and canonicalise the URL.

    Raises :class:`NonRetryableError` for anything that is not a valid
    Facebook Ad Library URL so RQ drops the job without retrying.
    """
    try:
        result = parse_ad_url(url)
        canonical = result["url"]
        logger.info("[job:%s] URL validated → %s", job_id, canonical)
        return canonical
    except AdUrlError as exc:
        raise NonRetryableError(f"Invalid Ad Library URL: {exc}") from exc


def _step_scrape(url: str, job_id: str, conn) -> MediaResult:
    """
    Launch Playwright, navigate to *url*, and extract media URLs.

    Raises :class:`ScraperError` on any browser / network failure so RQ
    knows the error is transient and retries the job.
    """
    _set_phase(conn, job_id, "scraping")
    logger.info("[job:%s] Scraping %s", job_id, url)
    try:
        media = scrape_ad_url(url)
        return media
    except NonRetryableError:
        raise
    except Exception as exc:
        raise ScraperError(f"Scraper failed for {url}: {exc}") from exc


def _step_extract(media: MediaResult, job_id: str, conn) -> list[dict]:
    """
    Convert a :class:`MediaResult` into the flat asset-list format expected
    by :func:`~backend.downloader.download_assets`.

    Raises :class:`NonRetryableError` when no media was found at all (the
    ad may have been removed; retrying would produce the same result).
    """
    _set_phase(conn, job_id, "extracting")

    asset_list: list[dict] = []
    for url in media.videos:
        asset_list.append({"url": url, "asset_type": "video"})
    for url in media.images:
        asset_list.append({"url": url, "asset_type": "image"})
    for url in media.thumbnails:
        asset_list.append({"url": url, "asset_type": "thumbnail"})

    logger.info(
        "[job:%s] Extracted %d assets (%dv / %di / %dt)",
        job_id,
        len(asset_list),
        len(media.videos),
        len(media.images),
        len(media.thumbnails),
    )

    if not asset_list:
        raise NonRetryableError(
            "No media assets found. The ad may have been removed or is image-only."
        )

    return asset_list


def _step_download(asset_list: list[dict], job_id: str, conn) -> dict:
    """
    Stream every asset to ``/tmp/jobs/{job_id}/`` and write ``metadata.json``.

    Per-file failures are recorded in the result but do not abort the batch.
    If *every* file fails, raises :class:`DownloadError` so RQ can retry.
    """
    _set_phase(conn, job_id, "downloading")
    _set_progress(conn, job_id, 0, len(asset_list))
    logger.info("[job:%s] Downloading %d assets", job_id, len(asset_list))

    try:
        result = download_assets(asset_list, job_id)
    except Exception as exc:
        raise DownloadError(f"Download batch failed: {exc}") from exc

    succeeded = result["summary"]["succeeded"]
    failed    = result["summary"]["failed"]
    _set_progress(conn, job_id, succeeded, len(asset_list))

    logger.info(
        "[job:%s] Download complete — %d succeeded, %d failed",
        job_id, succeeded, failed,
    )

    if succeeded == 0 and failed > 0:
        raise DownloadError(
            f"All {failed} download(s) failed. "
            "Possible CDN rate-limit or expired URLs — will retry."
        )

    return result


def _step_save(result: dict, job_id: str, conn) -> dict:
    """
    Finalise the result dict and mark the phase as ``saving``.

    ``metadata.json`` is already written by :func:`download_assets`; this
    step just attaches provenance fields and transitions the phase.
    """
    _set_phase(conn, job_id, "saving")

    result["job_id"]    = job_id
    result["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    logger.info(
        "[job:%s] %d file(s) saved to %s",
        job_id,
        result["summary"]["succeeded"],
        result.get("output_dir", ""),
    )
    return result


# ---------------------------------------------------------------------------
# Main task entry point
# ---------------------------------------------------------------------------


def run_ad_download(
    url:        str,
    job_id:     str,
    max_assets: int = 20,
) -> dict:
    """
    Full six-step pipeline executed by an RQ worker:

    1. **Validate**   — canonicalise URL; raise ``NonRetryableError`` if invalid.
    2. **Scrape**     — headless Playwright, intercept GraphQL, collect URLs.
    3. **Extract**    — flatten ``MediaResult`` into typed asset-list.
    4. **Download**   — stream every file; tolerate partial failures.
    5. **Save**       — finalise result dict and metadata.
    6. **Complete**   — update phase in Redis; return result for RQ storage.

    Phase updates are written to ``job:{job_id}:phase`` in Redis so the API
    can surface richer status without polling the RQ job object.

    Retry behaviour
    ---------------
    * :class:`NonRetryableError` → ``on_failure_callback`` zeroes retries.
    * :class:`ScraperError` / :class:`DownloadError` → RQ re-enqueues up to
      ``MAX_RETRIES`` times with the configured ``RETRY_INTERVALS``.

    Args:
        url:        Facebook Ad Library URL (validated & canonicalised here).
        job_id:     UUID assigned by the API; also used as the output directory.
        max_assets: Cap on the total number of downloaded files (unused by the
                    scraper directly but stored in the result for traceability).

    Returns:
        Result dict written to Redis by RQ::

            {
                "job_id":       str,
                "output_dir":   str,
                "completed_at": str,           # ISO-8601 UTC
                "assets":       list[dict],    # per-file metadata
                "summary":      {total, succeeded, failed},
            }
    """
    conn = get_redis_connection()
    started_at = time.monotonic()
    logger.info("[job:%s] Pipeline starting | url=%s", job_id, url)

    try:
        # ── Step 1: validate ────────────────────────────────────────────────
        url = _step_validate(url, job_id)

        # ── Step 2: scrape ──────────────────────────────────────────────────
        media = _step_scrape(url, job_id, conn)

        # ── Step 3: extract ─────────────────────────────────────────────────
        asset_list = _step_extract(media, job_id, conn)

        # Honour max_assets cap after extraction
        if max_assets and len(asset_list) > max_assets:
            logger.info(
                "[job:%s] Capping asset list from %d to %d",
                job_id, len(asset_list), max_assets,
            )
            asset_list = asset_list[:max_assets]

        # ── Step 4: download ────────────────────────────────────────────────
        result = _step_download(asset_list, job_id, conn)

        # ── Step 5: save / finalise ─────────────────────────────────────────
        result = _step_save(result, job_id, conn)

        # ── Step 6: mark complete ───────────────────────────────────────────
        _set_phase(conn, job_id, "complete")
        elapsed = time.monotonic() - started_at
        logger.info(
            "[job:%s] Pipeline finished — %d/%d files downloaded in %.1fs",
            job_id,
            result["summary"]["succeeded"],
            result["summary"]["total"],
            elapsed,
        )
        return result

    except (NonRetryableError, ScraperError, DownloadError) as exc:
        elapsed = time.monotonic() - started_at
        logger.error(
            "[job:%s] Pipeline aborted after %.1fs — %s: %s",
            job_id, elapsed, type(exc).__name__, exc,
        )
        raise   # let on_failure_callback handle phase + retry decisions

    except Exception as exc:
        elapsed = time.monotonic() - started_at
        _set_phase(conn, job_id, "error")
        logger.error(
            "[job:%s] Unexpected error after %.1fs: %s\n%s",
            job_id, elapsed, exc, tb.format_exc(),
        )
        raise
