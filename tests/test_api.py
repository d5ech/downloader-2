"""
tests/test_api.py

Unit tests for backend.api using FastAPI's TestClient.

All Redis / RQ interactions are mocked so no external services are required.

Coverage:
  POST /download    — validation, enqueue, response shape
  GET  /status      — status mapping for every RQ state, 404
  GET  /result      — complete/queued/running/error/404, file list extraction
  GET  /health      — liveness probe
  _map_status       — full mapping table
  _files_from_result — extraction helpers
"""

from __future__ import annotations

import sys
import os
import uuid
from unittest.mock import MagicMock, patch, PropertyMock

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub out heavy optional imports before the module is loaded
# (rq and redis are not installed in the test environment)
import types

for _mod in ("redis", "rq", "rq.job", "rq.exceptions"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)

# Provide the minimal symbols that api.py uses at import time
sys.modules["rq"].Queue = MagicMock                          # type: ignore[attr-defined]
sys.modules["rq.job"].Job = MagicMock                        # type: ignore[attr-defined]

# NoSuchJobError used in _fetch_job
class _NoSuchJobError(Exception):
    pass

sys.modules["rq.exceptions"].NoSuchJobError = _NoSuchJobError  # type: ignore[attr-defined]

from backend.api import (
    app,
    _map_status,
    _files_from_result,
    _RQ_TO_API_STATUS,
    DownloadRequest,
    StatusResponse,
    ResultResponse,
)

client = TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# Helpers
# ===========================================================================

VALID_URL    = "https://www.facebook.com/ads/library/?id=1457638199042352"
VALID_AD_ID  = "1457638199042352"

JOB_ID       = str(uuid.uuid4())


def _mock_job(
    *,
    job_id: str = JOB_ID,
    rq_status: str = "finished",
    result: dict | None = None,
    exc_info: str | None = None,
) -> MagicMock:
    """Build a minimal RQ Job mock."""
    job = MagicMock()
    job.id = job_id

    # Support both .value (enum) and plain string
    status_mock = MagicMock()
    status_mock.value = rq_status
    job.get_status.return_value = status_mock

    job.result   = result
    job.exc_info = exc_info
    return job


def _patch_fetch(job: MagicMock):
    """Patch _fetch_job to return *job* without touching Redis."""
    return patch("backend.api._fetch_job", return_value=job)


def _patch_queue(job: MagicMock):
    """Patch _get_queue so enqueue() returns *job*."""
    q_mock = MagicMock()
    q_mock.enqueue.return_value = job
    return patch("backend.api._get_queue", return_value=q_mock)


# ===========================================================================
# GET /health
# ===========================================================================

class TestHealth:
    def test_200(self):
        r = client.get("/health")
        assert r.status_code == 200

    def test_status_ok(self):
        r = client.get("/health")
        assert r.json()["status"] == "ok"


# ===========================================================================
# _map_status
# ===========================================================================

class TestMapStatus:
    def test_queued(self):
        assert _map_status("queued") == "queued"

    def test_deferred_maps_to_queued(self):
        assert _map_status("deferred") == "queued"

    def test_started_maps_to_running(self):
        assert _map_status("started") == "running"

    def test_finished_maps_to_complete(self):
        assert _map_status("finished") == "complete"

    def test_failed_maps_to_error(self):
        assert _map_status("failed") == "error"

    def test_stopped_maps_to_error(self):
        assert _map_status("stopped") == "error"

    def test_canceled_maps_to_error(self):
        assert _map_status("canceled") == "error"

    def test_unknown_maps_to_error(self):
        assert _map_status("something_weird") == "error"

    def test_all_rq_statuses_covered(self):
        """Every key in _RQ_TO_API_STATUS must map to one of the four API values."""
        valid = {"queued", "running", "complete", "error"}
        for rq, api in _RQ_TO_API_STATUS.items():
            assert api in valid, f"{rq!r} maps to unknown status {api!r}"


# ===========================================================================
# _files_from_result
# ===========================================================================

class TestFilesFromResult:
    def test_none_returns_empty(self):
        assert _files_from_result(None) == []

    def test_empty_dict_returns_empty(self):
        assert _files_from_result({}) == []

    def test_no_assets_key_returns_empty(self):
        assert _files_from_result({"job_id": "x"}) == []

    def test_successful_assets_extracted(self):
        result = {"assets": [
            {"filename": "video_1.mp4",  "success": True},
            {"filename": "image_1.jpg",  "success": True},
        ]}
        assert _files_from_result(result) == ["video_1.mp4", "image_1.jpg"]

    def test_failed_assets_excluded(self):
        result = {"assets": [
            {"filename": "video_1.mp4",  "success": True},
            {"filename": "video_2.mp4",  "success": False},
        ]}
        assert _files_from_result(result) == ["video_1.mp4"]

    def test_assets_without_filename_excluded(self):
        result = {"assets": [
            {"filename": None,          "success": True},
            {"filename": "image_1.jpg", "success": True},
        ]}
        assert _files_from_result(result) == ["image_1.jpg"]

    def test_non_dict_result_returns_empty(self):
        assert _files_from_result("not a dict") == []  # type: ignore[arg-type]

    def test_preserves_order(self):
        assets = [{"filename": f"{i}.mp4", "success": True} for i in range(5)]
        result = {"assets": assets}
        assert _files_from_result(result) == [f"{i}.mp4" for i in range(5)]

    def test_empty_assets_list(self):
        assert _files_from_result({"assets": []}) == []


# ===========================================================================
# POST /download
# ===========================================================================

class TestPostDownload:
    def test_202_on_valid_url(self):
        job = _mock_job(rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_URL})
        assert r.status_code == 202

    def test_returns_job_id(self):
        job = _mock_job(rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_URL})
        assert "job_id" in r.json()

    def test_returns_queued_status(self):
        job = _mock_job(rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_URL})
        assert r.json()["status"] == "queued"

    def test_job_id_matches_enqueued(self):
        job = _mock_job(job_id=JOB_ID, rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_URL})
        assert r.json()["job_id"] == JOB_ID

    def test_bare_id_accepted(self):
        """A bare numeric ad ID should also be accepted and canonicalised."""
        job = _mock_job(rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_AD_ID})
        assert r.status_code == 202

    def test_url_canonicalised(self):
        """The canonical https:// URL is forwarded, not the raw input."""
        job   = _mock_job(rq_status="queued")
        q_mock = MagicMock()
        q_mock.enqueue.return_value = job
        with patch("backend.api._get_queue", return_value=q_mock):
            client.post("/download", json={"url": VALID_AD_ID})
        _, kwargs = q_mock.enqueue.call_args
        url_sent = kwargs["kwargs"]["url"]
        assert url_sent.startswith("https://www.facebook.com/ads/library/")

    def test_invalid_url_returns_422(self):
        r = client.post("/download", json={"url": "not-a-valid-url"})
        assert r.status_code == 422

    def test_wrong_domain_returns_422(self):
        r = client.post("/download", json={"url": "https://evil.com/ads/library/?id=123"})
        assert r.status_code == 422

    def test_missing_url_field_returns_422(self):
        r = client.post("/download", json={})
        assert r.status_code == 422

    def test_non_numeric_id_in_url_returns_422(self):
        r = client.post("/download", json={"url": "https://www.facebook.com/ads/library/?id=abc"})
        assert r.status_code == 422

    def test_enqueue_called_once(self):
        job    = _mock_job(rq_status="queued")
        q_mock = MagicMock()
        q_mock.enqueue.return_value = job
        with patch("backend.api._get_queue", return_value=q_mock):
            client.post("/download", json={"url": VALID_URL})
        q_mock.enqueue.assert_called_once()

    def test_enqueue_receives_url_kwarg(self):
        job    = _mock_job(rq_status="queued")
        q_mock = MagicMock()
        q_mock.enqueue.return_value = job
        with patch("backend.api._get_queue", return_value=q_mock):
            client.post("/download", json={"url": VALID_URL})
        _, kwargs = q_mock.enqueue.call_args
        assert "url" in kwargs["kwargs"]

    def test_enqueue_receives_job_id_kwarg(self):
        job    = _mock_job(rq_status="queued")
        q_mock = MagicMock()
        q_mock.enqueue.return_value = job
        with patch("backend.api._get_queue", return_value=q_mock):
            client.post("/download", json={"url": VALID_URL})
        _, kwargs = q_mock.enqueue.call_args
        assert "job_id" in kwargs["kwargs"]

    def test_enqueue_job_id_matches_response(self):
        # Pin uuid4 so the generated ID is predictable, then verify that the
        # same ID appears both in the worker kwargs and in the HTTP response.
        fixed_uuid = JOB_ID
        job        = _mock_job(job_id=fixed_uuid, rq_status="queued")
        q_mock     = MagicMock()
        q_mock.enqueue.return_value = job
        with patch("backend.api._get_queue", return_value=q_mock), \
             patch("backend.api.uuid.uuid4", return_value=fixed_uuid):
            r = client.post("/download", json={"url": VALID_URL})
        _, eq_kwargs = q_mock.enqueue.call_args
        # Worker kwargs job_id and response job_id must be identical
        assert eq_kwargs["kwargs"]["job_id"] == r.json()["job_id"] == fixed_uuid

    def test_response_has_no_extra_keys(self):
        job = _mock_job(rq_status="queued")
        with _patch_queue(job):
            r = client.post("/download", json={"url": VALID_URL})
        assert set(r.json().keys()) == {"job_id", "status"}


# ===========================================================================
# GET /status/{job_id}
# ===========================================================================

class TestGetStatus:
    def _get(self, rq_status: str) -> dict:
        job = _mock_job(job_id=JOB_ID, rq_status=rq_status)
        with _patch_fetch(job):
            r = client.get(f"/status/{JOB_ID}")
        assert r.status_code == 200
        return r.json()

    def test_200_for_existing_job(self):
        job = _mock_job(rq_status="queued")
        with _patch_fetch(job):
            r = client.get(f"/status/{JOB_ID}")
        assert r.status_code == 200

    def test_queued_status(self):
        assert self._get("queued")["status"] == "queued"

    def test_started_returns_running(self):
        assert self._get("started")["status"] == "running"

    def test_finished_returns_complete(self):
        assert self._get("finished")["status"] == "complete"

    def test_failed_returns_error(self):
        assert self._get("failed")["status"] == "error"

    def test_deferred_returns_queued(self):
        assert self._get("deferred")["status"] == "queued"

    def test_stopped_returns_error(self):
        assert self._get("stopped")["status"] == "error"

    def test_response_contains_job_id(self):
        assert self._get("queued")["job_id"] == JOB_ID

    def test_response_keys(self):
        assert set(self._get("queued").keys()) == {"job_id", "status"}

    def test_404_for_missing_job(self):
        with patch("backend.api._fetch_job", side_effect=__import__("fastapi").HTTPException(status_code=404, detail="not found")):
            r = client.get(f"/status/nonexistent-id")
        assert r.status_code == 404


# ===========================================================================
# GET /result/{job_id}
# ===========================================================================

class TestGetResult:

    _RESULT_WITH_FILES = {
        "job_id": JOB_ID,
        "output_dir": f"/tmp/jobs/{JOB_ID}",
        "assets": [
            {"filename": "video_1.mp4",   "success": True,  "asset_type": "video"},
            {"filename": "image_1.jpg",   "success": True,  "asset_type": "image"},
            {"filename": "video_2.mp4",   "success": False, "asset_type": "video"},
        ],
        "summary": {"total": 3, "succeeded": 2, "failed": 1},
    }

    def test_200_when_complete(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.status_code == 200

    def test_files_list_in_response(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert "files" in r.json()

    def test_only_successful_files_returned(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        files = r.json()["files"]
        assert "video_2.mp4" not in files     # success=False

    def test_successful_files_present(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        files = r.json()["files"]
        assert "video_1.mp4" in files
        assert "image_1.jpg" in files

    def test_file_count_correct(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert len(r.json()["files"]) == 2

    def test_status_is_complete(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.json()["status"] == "complete"

    def test_job_id_in_response(self):
        job = _mock_job(job_id=JOB_ID, rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.json()["job_id"] == JOB_ID

    def test_response_keys(self):
        job = _mock_job(rq_status="finished", result=self._RESULT_WITH_FILES)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert set(r.json().keys()) == {"job_id", "status", "files"}

    def test_empty_files_when_no_assets(self):
        job = _mock_job(rq_status="finished", result={"assets": [], "summary": {}})
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.json()["files"] == []

    def test_empty_files_when_result_is_none(self):
        job = _mock_job(rq_status="finished", result=None)
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.json()["files"] == []

    def test_202_when_queued(self):
        job = _mock_job(rq_status="queued")
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.status_code == 202

    def test_202_when_running(self):
        job = _mock_job(rq_status="started")
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.status_code == 202

    def test_500_when_failed(self):
        job = _mock_job(rq_status="failed", exc_info="Something went wrong")
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.status_code == 500

    def test_500_detail_contains_error_info(self):
        job = _mock_job(rq_status="failed", exc_info="Traceback: ZeroDivisionError")
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert "ZeroDivisionError" in r.json().get("detail", "")

    def test_404_for_missing_job(self):
        with patch("backend.api._fetch_job", side_effect=__import__("fastapi").HTTPException(status_code=404, detail="not found")):
            r = client.get(f"/result/nonexistent-id")
        assert r.status_code == 404

    def test_deferred_returns_202(self):
        job = _mock_job(rq_status="deferred")
        with _patch_fetch(job):
            r = client.get(f"/result/{JOB_ID}")
        assert r.status_code == 202


# ===========================================================================
# DownloadRequest validator
# ===========================================================================

class TestDownloadRequestValidator:
    """Pydantic model validation — does not require an HTTP round-trip."""

    def _make(self, url: str) -> DownloadRequest:
        return DownloadRequest(url=url)

    def test_valid_url_accepted(self):
        m = self._make(VALID_URL)
        assert VALID_AD_ID in m.url

    def test_url_canonicalised_to_https(self):
        m = self._make(VALID_AD_ID)
        assert m.url.startswith("https://")

    def test_invalid_url_raises_value_error(self):
        with pytest.raises(Exception):
            self._make("not-a-url")

    def test_wrong_domain_raises(self):
        with pytest.raises(Exception):
            self._make("https://twitter.com/ads/library/?id=123")

    def test_non_numeric_id_raises(self):
        with pytest.raises(Exception):
            self._make("https://www.facebook.com/ads/library/?id=abc")
