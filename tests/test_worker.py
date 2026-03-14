"""
tests/test_worker.py

Unit tests for workers/tasks.py and workers/worker.py.

No real Redis, RQ, browser, or HTTP connections are used — all external
dependencies are replaced with mocks or lightweight stubs.

Coverage
--------
tasks.py:
  - Exception hierarchy
  - on_failure_callback        (NonRetryable zeros retries; others update phase)
  - on_success_callback
  - _set_phase / get_job_phase
  - _set_progress
  - _step_validate             (happy path, invalid URL → NonRetryableError)
  - _step_scrape               (happy path, scraper raises → ScraperError)
  - _step_extract              (happy / empty media → NonRetryableError)
  - _step_download             (happy / all-fail → DownloadError)
  - _step_save                 (fields added to result)
  - run_ad_download            (full pipeline, partial failure, each error type)

worker.py:
  - _connect_redis             (success, retries, exhausted)
  - _build_worker              (name format, exception handlers)
  - _handle_sigterm            (delegates to worker.request_stop)
  - _handle_sigint             (raises SystemExit)
  - QUEUE_NAMES / BURST_MODE   (env-driven config)
"""

from __future__ import annotations

import sys
import os
import types
import signal
from unittest.mock import MagicMock, patch, call

import pytest

# ---------------------------------------------------------------------------
# Stub out rq / redis before importing the workers package
# ---------------------------------------------------------------------------
for _mod in ("redis", "rq", "rq.job", "rq.exceptions", "playwright",
             "playwright.sync_api"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

sys.modules["rq"].Queue  = MagicMock       # type: ignore[attr-defined]
sys.modules["rq"].Worker = MagicMock       # type: ignore[attr-defined]

class _NoSuchJobError(Exception):
    pass

sys.modules["rq.exceptions"].NoSuchJobError = _NoSuchJobError  # type: ignore

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from workers.tasks import (
    WorkerError,
    NonRetryableError,
    ScraperError,
    DownloadError,
    on_failure_callback,
    on_success_callback,
    _set_phase,
    _set_progress,
    get_job_phase,
    _step_validate,
    _step_scrape,
    _step_extract,
    _step_download,
    _step_save,
    run_ad_download,
    PHASE_TTL,
    MAX_RETRIES,
    RETRY_INTERVALS,
)


# ===========================================================================
# Helpers
# ===========================================================================

JOB_ID = "test-job-1234"

VALID_URL     = "https://www.facebook.com/ads/library/?id=1457638199042352"
BARE_ID       = "1457638199042352"
INVALID_URL   = "https://evil.com/steal"


def _redis():
    """Return a MagicMock that mimics redis.Redis."""
    r = MagicMock()
    r.get.return_value = None
    return r


def _media(videos=1, images=1, thumbnails=1):
    """Return a mock MediaResult."""
    from backend.scraper import MediaResult
    return MediaResult(
        videos=[f"https://cdn.fb.com/v{i}.mp4"   for i in range(videos)],
        images=[f"https://cdn.fb.com/i{i}.jpg"   for i in range(images)],
        thumbnails=[f"https://cdn.fb.com/t{i}.jpg" for i in range(thumbnails)],
    )


def _download_result(succeeded=2, failed=0):
    assets = (
        [{"filename": f"video_{i}.mp4", "success": True,  "asset_type": "video"}
         for i in range(1, succeeded + 1)] +
        [{"filename": f"bad_{i}.mp4",   "success": False, "asset_type": "video"}
         for i in range(1, failed + 1)]
    )
    return {
        "job_id":     JOB_ID,
        "output_dir": f"/tmp/jobs/{JOB_ID}",
        "assets":     assets,
        "summary":    {"total": succeeded + failed, "succeeded": succeeded, "failed": failed},
    }


# ===========================================================================
# Exception hierarchy
# ===========================================================================

class TestExceptions:
    def test_worker_error_is_exception(self):
        assert issubclass(WorkerError, Exception)

    def test_non_retryable_is_worker_error(self):
        assert issubclass(NonRetryableError, WorkerError)

    def test_scraper_error_is_worker_error(self):
        assert issubclass(ScraperError, WorkerError)

    def test_download_error_is_worker_error(self):
        assert issubclass(DownloadError, WorkerError)

    def test_non_retryable_is_not_scraper(self):
        assert not issubclass(NonRetryableError, ScraperError)

    def test_all_can_be_instantiated_with_message(self):
        for cls in (WorkerError, NonRetryableError, ScraperError, DownloadError):
            e = cls("test message")
            assert "test message" in str(e)


# ===========================================================================
# on_failure_callback
# ===========================================================================

class TestOnFailureCallback:
    def _make_job(self, job_id=JOB_ID):
        job = MagicMock()
        job.id = job_id
        job.retries_left = 3
        return job

    def test_non_retryable_zeroes_retries(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, NonRetryableError, NonRetryableError("bad url"), None)
        assert job.retries_left == 0

    def test_non_retryable_saves_job(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, NonRetryableError, NonRetryableError("bad url"), None)
        job.save.assert_called_once()

    def test_non_retryable_sets_permanent_error_phase(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, NonRetryableError, NonRetryableError("bad url"), None)
        key = conn.set.call_args[0][0]
        assert "error_permanent" in conn.set.call_args[0][1]

    def test_transient_error_does_not_zero_retries(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, ScraperError, ScraperError("timeout"), None)
        assert job.retries_left == 3   # unchanged

    def test_transient_error_sets_error_phase(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, ScraperError, ScraperError("timeout"), None)
        phase_val = conn.set.call_args[0][1]
        assert phase_val == "error"

    def test_job_save_not_called_for_transient(self):
        job  = self._make_job()
        conn = _redis()
        on_failure_callback(job, conn, DownloadError, DownloadError("cdn timeout"), None)
        job.save.assert_not_called()

    def test_callback_does_not_raise_if_save_fails(self):
        job       = self._make_job()
        job.save.side_effect = Exception("Redis gone")
        conn      = _redis()
        # Must not propagate the inner exception
        on_failure_callback(job, conn, NonRetryableError, NonRetryableError("bad"), None)


# ===========================================================================
# on_success_callback
# ===========================================================================

class TestOnSuccessCallback:
    def test_sets_complete_phase(self):
        job  = MagicMock(); job.id = JOB_ID
        conn = _redis()
        on_success_callback(job, conn, {"result": "ok"})
        phase_val = conn.set.call_args[0][1]
        assert phase_val == "complete"

    def test_complete_phase_has_ttl(self):
        job  = MagicMock(); job.id = JOB_ID
        conn = _redis()
        on_success_callback(job, conn, {})
        _, kwargs = conn.set.call_args
        assert kwargs.get("ex") == PHASE_TTL


# ===========================================================================
# _set_phase / _set_progress / get_job_phase
# ===========================================================================

class TestPhaseHelpers:
    def test_set_phase_writes_correct_key(self):
        conn = _redis()
        _set_phase(conn, JOB_ID, "scraping")
        key = conn.set.call_args[0][0]
        assert JOB_ID in key

    def test_set_phase_writes_correct_value(self):
        conn = _redis()
        _set_phase(conn, JOB_ID, "downloading")
        val = conn.set.call_args[0][1]
        assert val == "downloading"

    def test_set_phase_passes_ttl(self):
        conn = _redis()
        _set_phase(conn, JOB_ID, "scraping")
        _, kwargs = conn.set.call_args
        assert kwargs.get("ex") == PHASE_TTL

    def test_set_phase_swallows_redis_error(self):
        conn      = _redis()
        conn.set.side_effect = Exception("connection refused")
        _set_phase(conn, JOB_ID, "scraping")   # must not raise

    def test_set_progress_writes_fraction(self):
        conn = _redis()
        _set_progress(conn, JOB_ID, 3, 10)
        val = conn.set.call_args[0][1]
        assert "3" in val and "10" in val

    def test_get_job_phase_returns_decoded_string(self):
        with patch("workers.tasks.get_redis_connection") as mock_conn:
            mock_conn.return_value.get.return_value = b"scraping"
            assert get_job_phase(JOB_ID) == "scraping"

    def test_get_job_phase_returns_none_on_miss(self):
        with patch("workers.tasks.get_redis_connection") as mock_conn:
            mock_conn.return_value.get.return_value = None
            assert get_job_phase(JOB_ID) is None

    def test_get_job_phase_returns_none_on_error(self):
        with patch("workers.tasks.get_redis_connection") as mock_conn:
            mock_conn.return_value.get.side_effect = Exception("no redis")
            assert get_job_phase(JOB_ID) is None


# ===========================================================================
# _step_validate
# ===========================================================================

class TestStepValidate:
    def test_valid_url_returned_canonicalised(self):
        url = _step_validate(VALID_URL, JOB_ID)
        assert url.startswith("https://www.facebook.com/ads/library/")

    def test_bare_id_accepted(self):
        url = _step_validate(BARE_ID, JOB_ID)
        assert "1457638199042352" in url

    def test_invalid_url_raises_non_retryable(self):
        with pytest.raises(NonRetryableError):
            _step_validate(INVALID_URL, JOB_ID)

    def test_empty_string_raises_non_retryable(self):
        with pytest.raises(NonRetryableError):
            _step_validate("", JOB_ID)

    def test_alphanumeric_id_raises_non_retryable(self):
        with pytest.raises(NonRetryableError):
            _step_validate("abc123", JOB_ID)

    def test_non_retryable_not_scraper_error(self):
        with pytest.raises(NonRetryableError) as exc_info:
            _step_validate(INVALID_URL, JOB_ID)
        assert not isinstance(exc_info.value, ScraperError)


# ===========================================================================
# _step_scrape
# ===========================================================================

class TestStepScrape:
    def test_returns_media_result(self):
        media = _media()
        conn  = _redis()
        with patch("workers.tasks.scrape_ad_url", return_value=media):
            result = _step_scrape(VALID_URL, JOB_ID, conn)
        assert result is media

    def test_sets_scraping_phase(self):
        conn = _redis()
        with patch("workers.tasks.scrape_ad_url", return_value=_media()):
            _step_scrape(VALID_URL, JOB_ID, conn)
        phases = [c[0][1] for c in conn.set.call_args_list]
        assert "scraping" in phases

    def test_scraper_exception_raises_scraper_error(self):
        conn = _redis()
        with patch("workers.tasks.scrape_ad_url", side_effect=Exception("browser crash")):
            with pytest.raises(ScraperError):
                _step_scrape(VALID_URL, JOB_ID, conn)

    def test_scraper_error_message_contains_original(self):
        conn = _redis()
        with patch("workers.tasks.scrape_ad_url", side_effect=Exception("timeout after 10s")):
            with pytest.raises(ScraperError, match="timeout after 10s"):
                _step_scrape(VALID_URL, JOB_ID, conn)

    def test_non_retryable_re_raised_unchanged(self):
        """NonRetryableError from scraper should pass through, not wrapped."""
        conn = _redis()
        with patch("workers.tasks.scrape_ad_url",
                   side_effect=NonRetryableError("access denied")):
            with pytest.raises(NonRetryableError):
                _step_scrape(VALID_URL, JOB_ID, conn)

    def test_scraper_called_with_correct_url(self):
        conn = _redis()
        with patch("workers.tasks.scrape_ad_url", return_value=_media()) as mock_scrape:
            _step_scrape(VALID_URL, JOB_ID, conn)
        mock_scrape.assert_called_once_with(VALID_URL)


# ===========================================================================
# _step_extract
# ===========================================================================

class TestStepExtract:
    def test_returns_flat_list(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=2, images=1, thumbnails=1), JOB_ID, conn)
        assert isinstance(asset_list, list)

    def test_total_count(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=2, images=3, thumbnails=1), JOB_ID, conn)
        assert len(asset_list) == 6

    def test_video_asset_type(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=1, images=0, thumbnails=0), JOB_ID, conn)
        assert asset_list[0]["asset_type"] == "video"

    def test_image_asset_type(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=0, images=1, thumbnails=0), JOB_ID, conn)
        assert asset_list[0]["asset_type"] == "image"

    def test_thumbnail_asset_type(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=0, images=0, thumbnails=1), JOB_ID, conn)
        assert asset_list[0]["asset_type"] == "thumbnail"

    def test_each_asset_has_url(self):
        conn       = _redis()
        asset_list = _step_extract(_media(videos=2, images=2, thumbnails=2), JOB_ID, conn)
        for asset in asset_list:
            assert "url" in asset
            assert asset["url"].startswith("http")

    def test_sets_extracting_phase(self):
        conn = _redis()
        _step_extract(_media(), JOB_ID, conn)
        phases = [c[0][1] for c in conn.set.call_args_list]
        assert "extracting" in phases

    def test_empty_media_raises_non_retryable(self):
        conn = _redis()
        from backend.scraper import MediaResult
        with pytest.raises(NonRetryableError):
            _step_extract(MediaResult(), JOB_ID, conn)

    def test_non_retryable_message_mentions_no_media(self):
        conn = _redis()
        from backend.scraper import MediaResult
        with pytest.raises(NonRetryableError, match="No media"):
            _step_extract(MediaResult(), JOB_ID, conn)

    def test_video_urls_preserved(self):
        conn  = _redis()
        media = _media(videos=2, images=0, thumbnails=0)
        asset_list = _step_extract(media, JOB_ID, conn)
        urls = {a["url"] for a in asset_list}
        assert set(media.videos).issubset(urls)


# ===========================================================================
# _step_download
# ===========================================================================

class TestStepDownload:
    def test_returns_result_dict(self):
        conn = _redis()
        r    = _download_result(succeeded=2)
        with patch("workers.tasks.download_assets", return_value=r):
            result = _step_download([{"url": "u", "asset_type": "video"}], JOB_ID, conn)
        assert isinstance(result, dict)

    def test_sets_downloading_phase(self):
        conn = _redis()
        with patch("workers.tasks.download_assets", return_value=_download_result()):
            _step_download([{"url": "u", "asset_type": "video"}], JOB_ID, conn)
        phases = [c[0][1] for c in conn.set.call_args_list]
        assert "downloading" in phases

    def test_download_assets_called_with_job_id(self):
        conn = _redis()
        with patch("workers.tasks.download_assets", return_value=_download_result()) as mock_dl:
            _step_download([{"url": "u"}], JOB_ID, conn)
        _, kwargs = mock_dl.call_args
        assert kwargs.get("job_id") == JOB_ID or mock_dl.call_args[0][1] == JOB_ID

    def test_partial_failure_does_not_raise(self):
        """Some files failed but at least one succeeded → no exception."""
        conn = _redis()
        r    = _download_result(succeeded=1, failed=1)
        with patch("workers.tasks.download_assets", return_value=r):
            result = _step_download([{"url": "u1"}, {"url": "u2"}], JOB_ID, conn)
        assert result["summary"]["succeeded"] == 1

    def test_all_failed_raises_download_error(self):
        conn = _redis()
        r    = _download_result(succeeded=0, failed=2)
        with patch("workers.tasks.download_assets", return_value=r):
            with pytest.raises(DownloadError):
                _step_download([{"url": "u1"}, {"url": "u2"}], JOB_ID, conn)

    def test_download_exception_raises_download_error(self):
        conn = _redis()
        with patch("workers.tasks.download_assets", side_effect=OSError("disk full")):
            with pytest.raises(DownloadError):
                _step_download([{"url": "u"}], JOB_ID, conn)

    def test_download_error_message_contains_original(self):
        conn = _redis()
        with patch("workers.tasks.download_assets", side_effect=OSError("no space left")):
            with pytest.raises(DownloadError, match="no space left"):
                _step_download([{"url": "u"}], JOB_ID, conn)

    def test_progress_updated(self):
        conn = _redis()
        with patch("workers.tasks.download_assets", return_value=_download_result(succeeded=3)):
            _step_download([{"url": f"u{i}"} for i in range(3)], JOB_ID, conn)
        # _set_progress writes to Redis; verify at least 2 calls (initial 0/n + final)
        assert conn.set.call_count >= 2


# ===========================================================================
# _step_save
# ===========================================================================

class TestStepSave:
    def test_returns_dict(self):
        conn   = _redis()
        result = _step_save(_download_result(), JOB_ID, conn)
        assert isinstance(result, dict)

    def test_job_id_set(self):
        conn   = _redis()
        result = _step_save(_download_result(), JOB_ID, conn)
        assert result["job_id"] == JOB_ID

    def test_completed_at_added(self):
        conn   = _redis()
        result = _step_save(_download_result(), JOB_ID, conn)
        assert "completed_at" in result

    def test_completed_at_is_string(self):
        conn   = _redis()
        result = _step_save(_download_result(), JOB_ID, conn)
        assert isinstance(result["completed_at"], str)

    def test_sets_saving_phase(self):
        conn = _redis()
        _step_save(_download_result(), JOB_ID, conn)
        phases = [c[0][1] for c in conn.set.call_args_list]
        assert "saving" in phases


# ===========================================================================
# run_ad_download (full pipeline)
# ===========================================================================

class TestRunAdDownload:
    """Tests for the main pipeline function with all steps mocked."""

    def _run(self, url=VALID_URL, job_id=JOB_ID, **overrides):
        media  = overrides.pop("media",  _media())
        dl_res = overrides.pop("dl_res", _download_result())
        conn   = overrides.pop("conn",   _redis())

        with patch("workers.tasks.get_redis_connection", return_value=conn), \
             patch("workers.tasks.scrape_ad_url",        return_value=media), \
             patch("workers.tasks.download_assets",      return_value=dl_res):
            return run_ad_download(url=url, job_id=job_id, **overrides)

    # ── Happy path ────────────────────────────────────────────────────────

    def test_returns_dict(self):
        result = self._run()
        assert isinstance(result, dict)

    def test_result_contains_job_id(self):
        result = self._run()
        assert result["job_id"] == JOB_ID

    def test_result_contains_assets(self):
        result = self._run()
        assert "assets" in result

    def test_result_contains_summary(self):
        result = self._run()
        assert "summary" in result

    def test_result_contains_completed_at(self):
        result = self._run()
        assert "completed_at" in result

    def test_complete_phase_set(self):
        conn = _redis()
        self._run(conn=conn)
        phases = [c[0][1] for c in conn.set.call_args_list]
        assert "complete" in phases

    def test_bare_id_accepted(self):
        result = self._run(url=BARE_ID)
        assert isinstance(result, dict)

    # ── max_assets cap ────────────────────────────────────────────────────

    def test_max_assets_caps_list(self):
        media = _media(videos=10, images=10, thumbnails=5)
        calls = []

        def _capture(asset_list, job_id):
            calls.append(len(asset_list))
            return _download_result(succeeded=5)

        conn = _redis()
        with patch("workers.tasks.get_redis_connection", return_value=conn), \
             patch("workers.tasks.scrape_ad_url",        return_value=media), \
             patch("workers.tasks.download_assets",      side_effect=_capture):
            run_ad_download(url=VALID_URL, job_id=JOB_ID, max_assets=5)

        assert calls[0] == 5

    # ── Invalid URL → NonRetryableError ───────────────────────────────────

    def test_invalid_url_raises_non_retryable(self):
        conn = _redis()
        with patch("workers.tasks.get_redis_connection", return_value=conn):
            with pytest.raises(NonRetryableError):
                run_ad_download(url=INVALID_URL, job_id=JOB_ID)

    # ── Scraper failure → ScraperError ────────────────────────────────────

    def test_scraper_failure_raises_scraper_error(self):
        conn = _redis()
        with patch("workers.tasks.get_redis_connection", return_value=conn), \
             patch("workers.tasks.scrape_ad_url", side_effect=RuntimeError("playwright died")):
            with pytest.raises(ScraperError):
                run_ad_download(url=VALID_URL, job_id=JOB_ID)

    # ── No media → NonRetryableError ─────────────────────────────────────

    def test_no_media_raises_non_retryable(self):
        from backend.scraper import MediaResult
        conn = _redis()
        with patch("workers.tasks.get_redis_connection", return_value=conn), \
             patch("workers.tasks.scrape_ad_url",        return_value=MediaResult()):
            with pytest.raises(NonRetryableError):
                run_ad_download(url=VALID_URL, job_id=JOB_ID)

    # ── All downloads fail → DownloadError ───────────────────────────────

    def test_all_downloads_fail_raises_download_error(self):
        conn   = _redis()
        dl_res = _download_result(succeeded=0, failed=3)
        with patch("workers.tasks.get_redis_connection", return_value=conn), \
             patch("workers.tasks.scrape_ad_url",        return_value=_media()), \
             patch("workers.tasks.download_assets",      return_value=dl_res):
            with pytest.raises(DownloadError):
                run_ad_download(url=VALID_URL, job_id=JOB_ID)

    # ── Partial success ───────────────────────────────────────────────────

    def test_partial_download_failure_does_not_raise(self):
        """As long as at least one file succeeded, the job completes."""
        dl_res = _download_result(succeeded=1, failed=2)
        result = self._run(dl_res=dl_res)
        assert result["summary"]["succeeded"] == 1

    # ── Phase progression ─────────────────────────────────────────────────

    def test_all_phases_written_in_order(self):
        conn = _redis()
        self._run(conn=conn)
        phases = [c[0][1] for c in conn.set.call_args_list
                  if not c[0][0].endswith(":progress")]
        expected = ["scraping", "extracting", "downloading", "saving", "complete"]
        # All expected phases must appear, in order
        phase_iter = iter(phases)
        for expected_phase in expected:
            assert any(p == expected_phase for p in phase_iter), \
                f"Phase {expected_phase!r} not found in {phases}"

    # ── Retry constants exposed ───────────────────────────────────────────

    def test_max_retries_is_positive(self):
        assert MAX_RETRIES > 0

    def test_retry_intervals_match_max_retries(self):
        assert len(RETRY_INTERVALS) == MAX_RETRIES


# ===========================================================================
# worker.py — _connect_redis
# ===========================================================================

class TestConnectRedis:
    def test_returns_connection_on_first_try(self):
        from workers.worker import _connect_redis
        mock_conn = MagicMock()
        mock_conn.ping.return_value = True
        with patch("workers.worker.get_redis_connection", return_value=mock_conn):
            conn = _connect_redis(max_retries=3)
        assert conn is mock_conn

    def test_retries_on_failure_then_succeeds(self):
        from workers.worker import _connect_redis
        mock_conn = MagicMock()
        attempts  = {"n": 0}

        def _ping():
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise ConnectionError("not ready")

        mock_conn.ping.side_effect = _ping
        with patch("workers.worker.get_redis_connection", return_value=mock_conn), \
             patch("workers.worker.time.sleep"):
            conn = _connect_redis(max_retries=5)
        assert conn is mock_conn
        assert attempts["n"] == 3

    def test_raises_after_max_retries(self):
        from workers.worker import _connect_redis
        mock_conn = MagicMock()
        mock_conn.ping.side_effect = ConnectionError("always down")
        with patch("workers.worker.get_redis_connection", return_value=mock_conn), \
             patch("workers.worker.time.sleep"):
            with pytest.raises(RuntimeError, match="Cannot connect"):
                _connect_redis(max_retries=3)

    def test_sleeps_between_retries(self):
        from workers.worker import _connect_redis
        mock_conn = MagicMock()
        call_count = {"n": 0}

        def _ping():
            call_count["n"] += 1
            if call_count["n"] < 3:
                raise ConnectionError("not ready")

        mock_conn.ping.side_effect = _ping
        with patch("workers.worker.get_redis_connection", return_value=mock_conn), \
             patch("workers.worker.time.sleep") as mock_sleep:
            _connect_redis(max_retries=5)
        assert mock_sleep.call_count == 2   # slept between attempts 1→2 and 2→3


# ===========================================================================
# worker.py — _build_worker
# ===========================================================================

class TestBuildWorker:
    def test_returns_worker_instance(self):
        from workers.worker import _build_worker
        mock_worker_cls = MagicMock()
        mock_worker_cls.return_value = MagicMock()

        with patch("workers.worker.Worker", mock_worker_cls):
            w = _build_worker(queues=[], conn=MagicMock())

        assert w is mock_worker_cls.return_value

    def test_exception_handler_passed(self):
        from workers.worker import _build_worker, _exception_handler
        mock_worker_cls = MagicMock()

        with patch("workers.worker.Worker", mock_worker_cls):
            _build_worker(queues=[], conn=MagicMock())

        _, kwargs = mock_worker_cls.call_args
        handlers = kwargs.get("exception_handlers", [])
        assert _exception_handler in handlers

    def test_auto_name_includes_pid(self):
        from workers.worker import _build_worker
        captured = {}
        mock_worker_cls = MagicMock()

        def _capture(*a, **kw):
            captured["name"] = kw.get("name", "")
            return MagicMock()

        mock_worker_cls.side_effect = _capture
        with patch("workers.worker.Worker", mock_worker_cls), \
             patch("workers.worker.WORKER_NAME", None):
            _build_worker(queues=[], conn=MagicMock())

        assert str(os.getpid()) in captured["name"]


# ===========================================================================
# worker.py — signal handlers
# ===========================================================================

class TestSignalHandlers:
    def test_sigterm_calls_request_stop(self):
        import workers.worker as wm
        mock_worker = MagicMock()
        wm._worker_ref.clear()
        wm._worker_ref.append(mock_worker)
        wm._handle_sigterm(signal.SIGTERM, None)
        mock_worker.request_stop.assert_called_once()
        wm._worker_ref.clear()

    def test_sigterm_no_worker_does_not_raise(self):
        import workers.worker as wm
        wm._worker_ref.clear()
        wm._handle_sigterm(signal.SIGTERM, None)   # must not raise

    def test_sigint_raises_system_exit(self):
        from workers.worker import _handle_sigint
        with pytest.raises(SystemExit):
            _handle_sigint(signal.SIGINT, None)


# ===========================================================================
# worker.py — env-driven config
# ===========================================================================

class TestWorkerConfig:
    def test_queue_names_default(self, monkeypatch):
        monkeypatch.delenv("RQ_QUEUES", raising=False)
        import importlib
        import workers.worker as wm
        importlib.reload(wm)
        assert "default" in wm.QUEUE_NAMES

    def test_burst_mode_false_by_default(self, monkeypatch):
        monkeypatch.delenv("RQ_BURST", raising=False)
        import importlib
        import workers.worker as wm
        importlib.reload(wm)
        assert wm.BURST_MODE is False

    def test_burst_mode_enabled_via_env(self, monkeypatch):
        monkeypatch.setenv("RQ_BURST", "true")
        import importlib
        import workers.worker as wm
        importlib.reload(wm)
        assert wm.BURST_MODE is True
        monkeypatch.delenv("RQ_BURST")
        importlib.reload(wm)
