"""
tests/test_downloader.py

Unit tests for backend.downloader.

All HTTP calls are intercepted with unittest.mock — no real network traffic.

Coverage:
  - download_media             (happy path, streaming, timeout, retry, errors)
  - download_assets            (filenames, metadata.json, counters, errors)
  - _build_session             (retry adapter, headers)
  - _stream_to_file            (chunk writes, empty chunks, byte count)
  - _ext_from_url              (extension extraction)
  - _write_metadata            (file creation, JSON shape)
  - _error_record              (shape)
  - _utcnow                    (ISO-8601 format)
  - download_media_assets shim (flattening AdRecord list)
"""

from __future__ import annotations

import json
import sys
import os
import io
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch, call, PropertyMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.downloader import (
    download_media,
    download_assets,
    download_media_assets,
    _build_session,
    _stream_to_file,
    _ext_from_url,
    _write_metadata,
    _error_record,
    _utcnow,
    CHUNK_SIZE,
    REQUEST_TIMEOUT,
    MAX_RETRIES,
    JOBS_ROOT,
)


# ===========================================================================
# Helpers / fixtures
# ===========================================================================

def _make_response(
    content: bytes = b"data",
    status_code: int = 200,
    content_type: str = "video/mp4",
    raise_for_status: Exception | None = None,
) -> MagicMock:
    """
    Build a minimal mock of a ``requests.Response``.

    ``iter_content`` yields the content in ``CHUNK_SIZE`` slices so the
    streaming code path is exercised regardless of content length.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Content-Type": content_type}

    # Yield real chunks so _stream_to_file accumulates correct byte counts
    def _iter_content(chunk_size=CHUNK_SIZE):
        for i in range(0, max(len(content), 1), chunk_size):
            chunk = content[i : i + chunk_size]
            if chunk:
                yield chunk

    resp.iter_content = _iter_content

    if raise_for_status:
        resp.raise_for_status.side_effect = raise_for_status
    else:
        resp.raise_for_status.return_value = None

    return resp


@pytest.fixture()
def tmp_file(tmp_path) -> Path:
    return tmp_path / "output.mp4"


@pytest.fixture()
def mock_session():
    return MagicMock()


# ===========================================================================
# _utcnow
# ===========================================================================

class TestUtcnow:
    def test_returns_string(self):
        assert isinstance(_utcnow(), str)

    def test_iso8601_format(self):
        ts = _utcnow()
        from datetime import datetime
        # Should parse without raising
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_contains_utc_offset(self):
        assert "+" in _utcnow() or "Z" in _utcnow()


# ===========================================================================
# _error_record
# ===========================================================================

class TestErrorRecord:
    def test_shape(self):
        r = _error_record(1, "video", "https://x.com/v.mp4", "timeout")
        assert set(r.keys()) == {
            "index", "asset_type", "url", "ad_id",
            "filename", "local_path", "size_bytes",
            "downloaded_at", "success", "error",
        }

    def test_success_is_false(self):
        r = _error_record(1, "image", "https://x.com/i.jpg", "err")
        assert r["success"] is False

    def test_nullable_fields_are_none(self):
        r = _error_record(1, "video", "https://x.com/v.mp4", "err")
        assert r["filename"] is None
        assert r["local_path"] is None
        assert r["size_bytes"] is None
        assert r["downloaded_at"] is None

    def test_error_message_stored(self):
        r = _error_record(3, "video", "https://x.com/v.mp4", "HTTP 404")
        assert r["error"] == "HTTP 404"

    def test_ad_id_default_empty(self):
        r = _error_record(1, "video", "https://x.com/v.mp4", "err")
        assert r["ad_id"] == ""

    def test_ad_id_kwarg(self):
        r = _error_record(1, "video", "https://x.com/v.mp4", "err", ad_id="ABC")
        assert r["ad_id"] == "ABC"

    def test_index_stored(self):
        r = _error_record(7, "image", "https://x.com/i.jpg", "err")
        assert r["index"] == 7


# ===========================================================================
# _ext_from_url
# ===========================================================================

class TestExtFromUrl:
    def test_mp4(self):
        assert _ext_from_url("https://cdn.fb.com/video.mp4") == ".mp4"

    def test_jpg(self):
        assert _ext_from_url("https://cdn.fb.com/img.jpg") == ".jpg"

    def test_strips_query_string(self):
        assert _ext_from_url("https://cdn.fb.com/v.mp4?token=abc") == ".mp4"

    def test_no_extension_returns_empty(self):
        assert _ext_from_url("https://cdn.fb.com/video") == ""

    def test_extension_is_lowercase(self):
        assert _ext_from_url("https://cdn.fb.com/img.JPG") == ".jpg"

    def test_trailing_slash_handled(self):
        assert _ext_from_url("https://cdn.fb.com/path/") == ""

    def test_webm(self):
        assert _ext_from_url("https://cdn.fb.com/clip.webm") == ".webm"


# ===========================================================================
# _stream_to_file
# ===========================================================================

class TestStreamToFile:
    def test_writes_bytes(self, tmp_path):
        dest     = tmp_path / "out.bin"
        response = _make_response(b"hello world")
        _stream_to_file(response, dest)
        assert dest.read_bytes() == b"hello world"

    def test_returns_byte_count(self, tmp_path):
        dest     = tmp_path / "out.bin"
        response = _make_response(b"abcde")
        n = _stream_to_file(response, dest)
        assert n == 5

    def test_large_content_chunked(self, tmp_path):
        # 3× CHUNK_SIZE + 1 byte to force multiple chunk writes
        content  = b"x" * (CHUNK_SIZE * 3 + 1)
        dest     = tmp_path / "out.bin"
        response = _make_response(content)
        n = _stream_to_file(response, dest)
        assert n == len(content)
        assert dest.read_bytes() == content

    def test_empty_chunks_skipped(self, tmp_path):
        dest     = tmp_path / "out.bin"
        response = MagicMock()
        response.iter_content = lambda chunk_size: iter([b"ab", b"", b"cd"])
        n = _stream_to_file(response, dest)
        assert n == 4
        assert dest.read_bytes() == b"abcd"

    def test_zero_byte_content(self, tmp_path):
        dest     = tmp_path / "out.bin"
        response = _make_response(b"")
        n = _stream_to_file(response, dest)
        assert n == 0
        assert dest.exists()


# ===========================================================================
# _build_session
# ===========================================================================

class TestBuildSession:
    def test_returns_session(self):
        import requests
        sess = _build_session()
        assert isinstance(sess, requests.Session)

    def test_https_adapter_has_retry(self):
        sess    = _build_session()
        adapter = sess.get_adapter("https://example.com")
        assert adapter.max_retries.total == MAX_RETRIES

    def test_http_adapter_has_retry(self):
        sess    = _build_session()
        adapter = sess.get_adapter("http://example.com")
        assert adapter.max_retries.total == MAX_RETRIES

    def test_backoff_factor(self):
        sess    = _build_session()
        adapter = sess.get_adapter("https://example.com")
        assert adapter.max_retries.backoff_factor == 1

    def test_status_forcelist(self):
        sess    = _build_session()
        adapter = sess.get_adapter("https://example.com")
        assert 429 in adapter.max_retries.status_forcelist
        assert 500 in adapter.max_retries.status_forcelist
        assert 503 in adapter.max_retries.status_forcelist

    def test_user_agent_header(self):
        sess = _build_session()
        assert "Mozilla" in sess.headers["User-Agent"]

    def test_referer_header(self):
        sess = _build_session()
        assert "facebook.com" in sess.headers["Referer"]


# ===========================================================================
# _write_metadata
# ===========================================================================

class TestWriteMetadata:
    def _result(self, job_id="j1", assets=None) -> dict:
        return {
            "job_id":     job_id,
            "output_dir": f"/tmp/jobs/{job_id}",
            "assets":     assets or [],
            "summary":    {"total": 0, "succeeded": 0, "failed": 0},
        }

    def test_creates_metadata_json(self, tmp_path):
        _write_metadata(tmp_path, self._result())
        assert (tmp_path / "metadata.json").exists()

    def test_valid_json(self, tmp_path):
        _write_metadata(tmp_path, self._result())
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert isinstance(data, dict)

    def test_job_id_present(self, tmp_path):
        _write_metadata(tmp_path, self._result("myjob"))
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert data["job_id"] == "myjob"

    def test_created_at_present(self, tmp_path):
        _write_metadata(tmp_path, self._result())
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert "created_at" in data

    def test_summary_present(self, tmp_path):
        _write_metadata(tmp_path, self._result())
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert "summary" in data

    def test_assets_list_present(self, tmp_path):
        _write_metadata(tmp_path, self._result())
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert isinstance(data["assets"], list)

    def test_asset_entries_preserved(self, tmp_path):
        assets = [{"index": 1, "filename": "video_1.mp4", "success": True}]
        _write_metadata(tmp_path, self._result(assets=assets))
        data = json.loads((tmp_path / "metadata.json").read_text())
        assert data["assets"][0]["filename"] == "video_1.mp4"


# ===========================================================================
# download_media
# ===========================================================================

class TestDownloadMedia:
    def test_returns_path(self, tmp_file):
        resp = _make_response(b"bytes", content_type="video/mp4")
        with patch("backend.downloader._build_session") as mock_sess_factory:
            mock_sess_factory.return_value.get.return_value = resp
            result = download_media("https://cdn.fb.com/v.mp4", tmp_file)
        assert isinstance(result, Path)

    def test_file_written(self, tmp_file):
        resp = _make_response(b"video-data", content_type="video/mp4")
        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.return_value = resp
            download_media("https://cdn.fb.com/v.mp4", tmp_file)
        assert tmp_file.read_bytes() == b"video-data"

    def test_resolved_path_returned(self, tmp_file):
        resp = _make_response(b"x")
        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.return_value = resp
            result = download_media("https://cdn.fb.com/v.mp4", tmp_file)
        assert result == tmp_file.resolve()

    def test_uses_stream_true(self, tmp_file):
        resp = _make_response(b"x")
        with patch("backend.downloader._build_session") as msf:
            sess = msf.return_value
            sess.get.return_value = resp
            download_media("https://cdn.fb.com/v.mp4", tmp_file)
        _, kwargs = sess.get.call_args
        assert kwargs.get("stream") is True

    def test_uses_correct_timeout(self, tmp_file):
        resp = _make_response(b"x")
        with patch("backend.downloader._build_session") as msf:
            sess = msf.return_value
            sess.get.return_value = resp
            download_media("https://cdn.fb.com/v.mp4", tmp_file)
        _, kwargs = sess.get.call_args
        assert kwargs.get("timeout") == REQUEST_TIMEOUT

    def test_raise_for_status_called(self, tmp_file):
        resp = _make_response(b"x")
        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.return_value = resp
            download_media("https://cdn.fb.com/v.mp4", tmp_file)
        resp.raise_for_status.assert_called_once()

    def test_http_error_propagates(self, tmp_file):
        import requests as req
        resp = _make_response(raise_for_status=req.HTTPError("404"))
        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.return_value = resp
            with pytest.raises(req.HTTPError):
                download_media("https://cdn.fb.com/v.mp4", tmp_file)

    def test_timeout_error_propagates(self, tmp_file):
        import requests as req
        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.side_effect = req.Timeout("timed out")
            with pytest.raises(req.Timeout):
                download_media("https://cdn.fb.com/v.mp4", tmp_file)

    def test_accepts_provided_session(self, tmp_file):
        """When a session is passed in, _build_session must NOT be called."""
        resp    = _make_response(b"x")
        sess    = MagicMock()
        sess.get.return_value = resp
        with patch("backend.downloader._build_session") as msf:
            download_media("https://cdn.fb.com/v.mp4", tmp_file, session=sess)
            msf.assert_not_called()
        sess.get.assert_called_once()

    def test_chunk_size_8192(self, tmp_file):
        """iter_content must be called with chunk_size == 8192."""
        content = b"a" * CHUNK_SIZE * 2
        resp    = _make_response(content)
        # Replace the real iter_content function with a MagicMock that still
        # yields the data so _stream_to_file can write it.
        real_iter = resp.iter_content
        mock_iter = MagicMock(side_effect=real_iter)
        resp.iter_content = mock_iter

        with patch("backend.downloader._build_session") as msf:
            msf.return_value.get.return_value = resp
            download_media("https://cdn.fb.com/v.mp4", tmp_file)

        mock_iter.assert_called_once_with(chunk_size=8192)


# ===========================================================================
# download_assets
# ===========================================================================

class TestDownloadAssets:
    """Tests use a real temporary directory so metadata.json can be inspected."""

    def _patch_get(self, responses: list[MagicMock]):
        """
        Return a context manager that patches ``requests.Session.get``
        to return successive responses from *responses*.
        """
        idx = {"n": 0}

        def _side_effect(*a, **kw):
            r = responses[min(idx["n"], len(responses) - 1)]
            idx["n"] += 1
            return r

        return patch("backend.downloader.requests.Session.get", side_effect=_side_effect)

    # ── Output directory ─────────────────────────────────────────────────

    def test_creates_job_directory(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"data", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "job1")
        assert Path(result["output_dir"]).exists()

    def test_output_dir_contains_job_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"data", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "myjob")
        assert "myjob" in result["output_dir"]

    # ── Filename sequencing ───────────────────────────────────────────────

    def test_video_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"data", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["filename"] == "video_1.mp4"

    def test_image_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"data", content_type="image/jpeg")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/i.jpg", "asset_type": "image"}], "j")
        assert result["assets"][0]["filename"] == "image_1.jpg"

    def test_thumbnail_filename(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"data", content_type="image/jpeg")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/t.jpg", "asset_type": "thumbnail"}], "j")
        assert result["assets"][0]["filename"] == "thumbnail_1.jpg"

    def test_sequential_numbering_same_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resps = [_make_response(b"x", content_type="video/mp4") for _ in range(3)]
        assets = [{"url": f"https://x.com/{i}.mp4", "asset_type": "video"} for i in range(3)]
        with self._patch_get(resps):
            result = download_assets(assets, "j")
        names = [r["filename"] for r in result["assets"]]
        assert names == ["video_1.mp4", "video_2.mp4", "video_3.mp4"]

    def test_independent_counters_per_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resps = [
            _make_response(b"x", content_type="video/mp4"),
            _make_response(b"x", content_type="image/jpeg"),
            _make_response(b"x", content_type="video/mp4"),
        ]
        assets = [
            {"url": "https://x.com/1.mp4", "asset_type": "video"},
            {"url": "https://x.com/2.jpg", "asset_type": "image"},
            {"url": "https://x.com/3.mp4", "asset_type": "video"},
        ]
        with self._patch_get(resps):
            result = download_assets(assets, "j")
        names = [r["filename"] for r in result["assets"]]
        assert names == ["video_1.mp4", "image_1.jpg", "video_2.mp4"]

    def test_unknown_type_uses_asset_prefix(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4"}], "j")
        assert result["assets"][0]["filename"].startswith("asset_")

    # ── Extension resolution ──────────────────────────────────────────────

    def test_extension_from_content_type(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="image/png")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/img", "asset_type": "image"}], "j")
        assert result["assets"][0]["filename"].endswith(".png")

    def test_webm_extension(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/webm")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v", "asset_type": "video"}], "j")
        assert result["assets"][0]["filename"].endswith(".webm")

    def test_fallback_to_url_extension(self, tmp_path, monkeypatch):
        """Unknown Content-Type falls back to URL-derived extension."""
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="application/octet-stream")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["filename"].endswith(".mp4")

    def test_bin_extension_when_no_hint(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="application/octet-stream")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/data", "asset_type": "video"}], "j")
        assert result["assets"][0]["filename"].endswith(".bin")

    # ── File actually written ─────────────────────────────────────────────

    def test_file_exists_after_download(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"content", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        lp = result["assets"][0]["local_path"]
        assert Path(lp).exists()

    def test_file_content_correct(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"video-bytes", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        lp = result["assets"][0]["local_path"]
        assert Path(lp).read_bytes() == b"video-bytes"

    def test_size_bytes_recorded(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"abc", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["size_bytes"] == 3

    # ── Success record shape ──────────────────────────────────────────────

    def test_success_record_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["success"] is True
        assert result["assets"][0]["error"] is None
        assert result["assets"][0]["downloaded_at"] is not None

    def test_index_starts_at_one(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["index"] == 1

    # ── Summary dict ─────────────────────────────────────────────────────

    def test_summary_total(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resps = [_make_response(b"x", content_type="video/mp4") for _ in range(2)]
        assets = [{"url": f"https://x.com/{i}.mp4", "asset_type": "video"} for i in range(2)]
        with self._patch_get(resps):
            result = download_assets(assets, "j")
        assert result["summary"]["total"] == 2

    def test_summary_succeeded(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resps = [_make_response(b"x", content_type="video/mp4") for _ in range(2)]
        assets = [{"url": f"https://x.com/{i}.mp4", "asset_type": "video"} for i in range(2)]
        with self._patch_get(resps):
            result = download_assets(assets, "j")
        assert result["summary"]["succeeded"] == 2
        assert result["summary"]["failed"] == 0

    # ── Error handling ────────────────────────────────────────────────────

    def test_missing_url_is_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        result = download_assets([{"asset_type": "video"}], "j")
        assert result["assets"][0]["success"] is False
        assert result["summary"]["failed"] == 1

    def test_http_error_recorded(self, tmp_path, monkeypatch):
        import requests as req
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(raise_for_status=req.HTTPError("404"))
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["success"] is False
        assert "404" in result["assets"][0]["error"]

    def test_failed_asset_has_none_local_path(self, tmp_path, monkeypatch):
        import requests as req
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(raise_for_status=req.HTTPError("500"))
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert result["assets"][0]["local_path"] is None

    def test_one_failure_does_not_stop_others(self, tmp_path, monkeypatch):
        import requests as req
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resps = [
            _make_response(raise_for_status=req.HTTPError("500")),
            _make_response(b"x", content_type="video/mp4"),
        ]
        assets = [
            {"url": "https://x.com/bad.mp4",  "asset_type": "video"},
            {"url": "https://x.com/good.mp4", "asset_type": "video"},
        ]
        with self._patch_get(resps):
            result = download_assets(assets, "j")
        assert result["summary"]["succeeded"] == 1
        assert result["summary"]["failed"]    == 1
        assert result["assets"][1]["success"] is True

    # ── metadata.json ─────────────────────────────────────────────────────

    def test_metadata_json_created(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        meta = Path(result["output_dir"]) / "metadata.json"
        assert meta.exists()

    def test_metadata_json_is_valid(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        meta = Path(result["output_dir"]) / "metadata.json"
        data = json.loads(meta.read_text())
        assert isinstance(data, dict)

    def test_metadata_contains_job_id(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4"}], "uniquejob99")
        meta = json.loads((Path(result["output_dir"]) / "metadata.json").read_text())
        assert meta["job_id"] == "uniquejob99"

    def test_metadata_contains_summary(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4"}], "j")
        meta = json.loads((Path(result["output_dir"]) / "metadata.json").read_text())
        assert "summary" in meta

    def test_metadata_created_at_present(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(b"x", content_type="video/mp4")
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4"}], "j")
        meta = json.loads((Path(result["output_dir"]) / "metadata.json").read_text())
        assert "created_at" in meta

    def test_metadata_written_even_on_failure(self, tmp_path, monkeypatch):
        """metadata.json must exist even if every download failed."""
        import requests as req
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        resp = _make_response(raise_for_status=req.HTTPError("404"))
        with self._patch_get([resp]):
            result = download_assets([{"url": "https://x.com/v.mp4", "asset_type": "video"}], "j")
        assert (Path(result["output_dir"]) / "metadata.json").exists()

    def test_empty_asset_list(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        result = download_assets([], "empty")
        assert result["summary"]["total"] == 0
        assert result["assets"] == []
        assert (Path(result["output_dir"]) / "metadata.json").exists()

    # ── Return structure ──────────────────────────────────────────────────

    def test_return_keys(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        result = download_assets([], "j")
        assert set(result.keys()) == {"job_id", "output_dir", "assets", "summary"}

    def test_job_id_in_result(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        result = download_assets([], "myjobid")
        assert result["job_id"] == "myjobid"


# ===========================================================================
# download_media_assets (legacy shim)
# ===========================================================================

class TestDownloadMediaAssetsShim:
    """Verify that the legacy shim correctly flattens AdRecord-like objects."""

    class _FakeAsset:
        def __init__(self, url, asset_type):
            self.url        = url
            self.asset_type = asset_type

    class _FakeRecord:
        def __init__(self, ad_id, assets):
            self.ad_id  = ad_id
            self.assets = assets

    def test_delegates_to_download_assets(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        calls = []

        def _fake_download_assets(asset_list, job_id):
            calls.append((asset_list, job_id))
            return {"job_id": job_id, "output_dir": str(tmp_path / job_id),
                    "assets": [], "summary": {"total": 0, "succeeded": 0, "failed": 0}}

        monkeypatch.setattr("backend.downloader.download_assets", _fake_download_assets)

        rec = self._FakeRecord("123", [self._FakeAsset("https://x.com/v.mp4", "video")])
        download_media_assets([rec], "j")
        assert len(calls) == 1

    def test_flattens_assets(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        received: list = []

        def _capture(asset_list, job_id):
            received.extend(asset_list)
            return {"job_id": job_id, "output_dir": str(tmp_path / job_id),
                    "assets": [], "summary": {"total": 0, "succeeded": 0, "failed": 0}}

        monkeypatch.setattr("backend.downloader.download_assets", _capture)

        records = [
            self._FakeRecord("1", [
                self._FakeAsset("https://x.com/v.mp4", "video"),
                self._FakeAsset("https://x.com/i.jpg", "image"),
            ]),
            self._FakeRecord("2", [
                self._FakeAsset("https://x.com/t.jpg", "thumbnail"),
            ]),
        ]
        download_media_assets(records, "j")
        assert len(received) == 3

    def test_ad_id_propagated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        received: list = []

        def _capture(asset_list, job_id):
            received.extend(asset_list)
            return {"job_id": job_id, "output_dir": "", "assets": [],
                    "summary": {"total": 0, "succeeded": 0, "failed": 0}}

        monkeypatch.setattr("backend.downloader.download_assets", _capture)

        rec = self._FakeRecord("AD99", [self._FakeAsset("https://x.com/v.mp4", "video")])
        download_media_assets([rec], "j")
        assert received[0]["ad_id"] == "AD99"

    def test_empty_records(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.downloader.JOBS_ROOT", tmp_path)
        calls: list = []

        def _capture(asset_list, job_id):
            calls.append(asset_list)
            return {"job_id": job_id, "output_dir": "", "assets": [],
                    "summary": {"total": 0, "succeeded": 0, "failed": 0}}

        monkeypatch.setattr("backend.downloader.download_assets", _capture)
        download_media_assets([], "j")
        assert calls[0] == []
