"""
downloader.py — HTTP asset downloader.

Public API
----------
``download_media(url, output_path)``
    Stream a single URL to a local file; return the resolved Path.

``download_assets(asset_list, job_id)``
    Batch-download a list of asset dicts to ``/tmp/jobs/{job_id}/``,
    write a ``metadata.json`` manifest, and return a result dict.

``download_media_assets(records, job_id)``
    Legacy shim consumed by workers/tasks.py — delegates to
    ``download_assets`` after flattening ``list[AdRecord]``.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

try:
    from backend.utils import get_logger   # package import (tests, workers)
except ImportError:
    from utils import get_logger           # direct run from backend/

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

CHUNK_SIZE      = 8_192   # 8 KB — per spec
REQUEST_TIMEOUT = 15      # seconds for both connect and read
MAX_RETRIES     = 3

# Where batch jobs land.  Override via environment for Docker / staging.
JOBS_ROOT = Path(os.environ.get("JOBS_ROOT", "/tmp/jobs"))

# MIME type → preferred extension  (avoids stdlib quirks like .jpe / .jpeg)
_MIME_TO_EXT: dict[str, str] = {
    "video/mp4":       ".mp4",
    "video/webm":      ".webm",
    "video/quicktime": ".mov",
    "image/jpeg":      ".jpg",
    "image/png":       ".png",
    "image/gif":       ".gif",
    "image/webp":      ".webp",
}

# Filename prefix used per asset_type for sequential numbering
_TYPE_PREFIX: dict[str, str] = {
    "video":     "video",
    "image":     "image",
    "thumbnail": "thumbnail",
    "unknown":   "asset",
}

# ---------------------------------------------------------------------------
# Public functions
# ---------------------------------------------------------------------------


def download_media(
    url: str,
    output_path: str | Path,
    *,
    session: requests.Session | None = None,
) -> Path:
    """
    Stream a single URL to *output_path*.

    The parent directory of *output_path* must already exist.

    Args:
        url:         HTTP(S) URL to fetch.
        output_path: Destination file path (str or Path).
        session:     Optional pre-built :class:`requests.Session`.  A new
                     session (with retry adapter) is created when omitted.

    Returns:
        Resolved :class:`~pathlib.Path` of the saved file.

    Raises:
        requests.HTTPError:  Non-2xx response after all retries are exhausted.
        requests.Timeout:    Server did not respond within ``REQUEST_TIMEOUT``.
        requests.ConnectionError: Network-level failure after retries.
        OSError:             File could not be written.
    """
    dest    = Path(output_path)
    _sess   = session or _build_session()

    logger.debug("download_media: GET %s → %s", url, dest)

    response = _sess.get(url, stream=True, timeout=REQUEST_TIMEOUT)
    response.raise_for_status()

    size_bytes = _stream_to_file(response, dest)
    logger.debug("Saved %.1f KB to %s", size_bytes / 1024, dest)

    return dest.resolve()


def download_assets(
    asset_list: list[dict[str, Any]],
    job_id: str,
) -> dict[str, Any]:
    """
    Download a batch of assets into ``/tmp/jobs/{job_id}/``.

    Each asset dict must contain at least a ``"url"`` key.  Optional keys:

    * ``"asset_type"`` — ``"video"``, ``"image"``, ``"thumbnail"``, or
      ``"unknown"`` (default ``"unknown"``).
    * ``"ad_id"``      — opaque string stored in the metadata only.

    Files are named sequentially by type: ``video_1.mp4``, ``video_2.mp4``,
    ``image_1.jpg``, ``thumbnail_1.jpg``, etc.  Extensions are derived from
    the ``Content-Type`` response header; the URL path is used as a fallback.

    A ``metadata.json`` manifest is written to the job directory on completion
    (even when some downloads fail).

    Args:
        asset_list: List of asset descriptor dicts.
        job_id:     Unique job identifier used as the output sub-directory.

    Returns:
        Result dict::

            {
                "job_id":      str,
                "output_dir":  str,          # absolute path to job directory
                "assets": [
                    {
                        "index":        int,
                        "asset_type":   str,
                        "url":          str,
                        "ad_id":        str,
                        "filename":     str | None,   # None on failure
                        "local_path":   str | None,   # None on failure
                        "size_bytes":   int | None,   # None on failure
                        "downloaded_at": str | None,  # ISO-8601 UTC
                        "success":      bool,
                        "error":        str | None,
                    },
                    ...
                ],
                "summary": {
                    "total":     int,
                    "succeeded": int,
                    "failed":    int,
                },
            }
    """
    job_dir = JOBS_ROOT / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    logger.info("download_assets: job=%s dir=%s assets=%d", job_id, job_dir, len(asset_list))

    session  = _build_session()
    counters: dict[str, int]   = {}   # prefix → sequential counter
    records:  list[dict]       = []

    for idx, spec in enumerate(asset_list, start=1):
        url        = (spec.get("url") or "").strip()
        asset_type = (spec.get("asset_type") or "unknown").lower()
        ad_id      = spec.get("ad_id") or ""

        if not url:
            records.append(_error_record(idx, asset_type, url, "No URL provided", ad_id=ad_id))
            continue

        prefix                = _TYPE_PREFIX.get(asset_type, "asset")
        counters[prefix]      = counters.get(prefix, 0) + 1
        n                     = counters[prefix]

        # Optimistically choose an extension from the URL before opening the
        # connection; it will be replaced with the Content-Type value below.
        ext_guess = _ext_from_url(url)

        try:
            response = session.get(url, stream=True, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()

            # Finalise extension from the Content-Type header
            ct  = response.headers.get("Content-Type", "").split(";")[0].strip()
            ext = _MIME_TO_EXT.get(ct) or ext_guess or ".bin"

            filename = f"{prefix}_{n}{ext}"
            dest     = job_dir / filename

            size_bytes = _stream_to_file(response, dest)

            if size_bytes == 0:
                dest.unlink(missing_ok=True)
                raise ValueError("Server returned an empty response body (0 bytes)")

            records.append({
                "index":         idx,
                "asset_type":    asset_type,
                "url":           url,
                "ad_id":         ad_id,
                "filename":      filename,
                "local_path":    str(dest.resolve()),
                "size_bytes":    size_bytes,
                "downloaded_at": _utcnow(),
                "success":       True,
                "error":         None,
            })
            logger.info("  [%d/%d] %s — %.1f KB", idx, len(asset_list), filename, size_bytes / 1024)

        except Exception as exc:
            logger.error("  [%d/%d] FAILED %s — %s", idx, len(asset_list), url, exc)
            records.append(_error_record(idx, asset_type, url, str(exc), ad_id=ad_id))

    succeeded = sum(1 for r in records if r["success"])
    result: dict[str, Any] = {
        "job_id":     job_id,
        "output_dir": str(job_dir.resolve()),
        "assets":     records,
        "summary": {
            "total":     len(records),
            "succeeded": succeeded,
            "failed":    len(records) - succeeded,
        },
    }

    _write_metadata(job_dir, result)
    logger.info(
        "download_assets complete — %d/%d succeeded, metadata → %s/metadata.json",
        succeeded, len(records), job_dir,
    )
    return result


# ---------------------------------------------------------------------------
# Legacy shim — keeps workers/tasks.py working unchanged
# ---------------------------------------------------------------------------


def download_media_assets(records: list, job_id: str) -> dict:
    """
    Compatibility wrapper for the ``list[AdRecord]`` interface used by
    ``workers/tasks.py``.  Flattens the records into the flat ``asset_list``
    format expected by :func:`download_assets`.
    """
    asset_list: list[dict] = []
    for record in records:
        for asset in getattr(record, "assets", []):
            asset_list.append({
                "url":        asset.url,
                "asset_type": asset.asset_type,
                "ad_id":      getattr(record, "ad_id", ""),
            })
    return download_assets(asset_list, job_id)


# ---------------------------------------------------------------------------
# Session builder
# ---------------------------------------------------------------------------


def _build_session() -> requests.Session:
    """
    Return a :class:`requests.Session` with:

    * A urllib3 ``Retry`` policy — ``MAX_RETRIES`` attempts with
      exponential back-off on 429/5xx responses.
    * Realistic browser headers to avoid trivial bot-detection.
    """
    session = requests.Session()

    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,               # waits 1 s, 2 s, 4 s between attempts
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
        raise_on_status=False,          # let raise_for_status() handle it
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://",  adapter)

    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "Accept":  "*/*",
        "Referer": "https://www.facebook.com/",
    })
    return session


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _stream_to_file(response: requests.Response, dest: Path) -> int:
    """
    Write *response* body to *dest* in ``CHUNK_SIZE`` (8 192 B) chunks.

    Returns the total number of bytes written.
    """
    total = 0
    with dest.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:           # filter keep-alive empty chunks
                fh.write(chunk)
                total += len(chunk)
    return total


def _ext_from_url(url: str) -> str:
    """
    Derive a file extension from the URL path as a best-effort fallback.

    Returns an extension string (e.g. ``".mp4"``) or ``""`` if none can
    be determined.
    """
    path = url.split("?")[0].rstrip("/")
    _, ext = os.path.splitext(path)
    return ext.lower() if ext else ""


def _write_metadata(job_dir: Path, result: dict) -> None:
    """Serialise *result* to ``{job_dir}/metadata.json``."""
    payload = {
        "job_id":     result["job_id"],
        "created_at": _utcnow(),
        "output_dir": result["output_dir"],
        "summary":    result["summary"],
        "assets":     result["assets"],
    }
    meta_path = job_dir / "metadata.json"
    meta_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.debug("metadata.json written to %s", meta_path)


def _error_record(
    idx:        int,
    asset_type: str,
    url:        str,
    error:      str,
    *,
    ad_id:      str = "",
) -> dict:
    """Return a failed-asset record in the standard shape."""
    return {
        "index":         idx,
        "asset_type":    asset_type,
        "url":           url,
        "ad_id":         ad_id,
        "filename":      None,
        "local_path":    None,
        "size_bytes":    None,
        "downloaded_at": None,
        "success":       False,
        "error":         error,
    }


def _utcnow() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()
