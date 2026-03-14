"""
tests/test_scraper.py

Unit tests for backend.scraper — focuses on the pure-Python logic that does
not require a real browser:

  - _iter_json_chunks
  - _extract_media_urls  (drives _recursive_search)
  - _extract_ad_id_from_url
  - scrape_ad_library_url (shim) — exercised via a monkey-patched scrape_ad_url
  - _trigger_video_playback
  - _try_click_play_button
  - _try_click_video_element
  - _try_js_play

Playwright / network I/O is NOT exercised here; that belongs to integration
tests that run against a live browser.
"""

from __future__ import annotations

import json
import sys
import os
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.scraper import (
    MediaResult,
    AdRecord,
    AdAsset,
    _iter_json_chunks,
    _extract_media_urls,
    _extract_ad_id_from_url,
    _parse_and_collect,
    scrape_ad_library_url,
    _recursive_search,
    _trigger_video_playback,
    _try_click_play_button,
    _try_click_video_element,
    _try_js_play,
    PLAYBACK_TIMEOUT_MS,
)


# ===========================================================================
# Helpers
# ===========================================================================

def _body(obj) -> str:
    return json.dumps(obj)


# ===========================================================================
# _iter_json_chunks
# ===========================================================================

class TestIterJsonChunks:
    def test_single_object(self):
        chunks = list(_iter_json_chunks('{"a": 1}'))
        assert chunks == [{"a": 1}]

    def test_single_array(self):
        chunks = list(_iter_json_chunks("[1, 2, 3]"))
        assert chunks == [[1, 2, 3]]

    def test_newline_delimited(self):
        body = '{"a": 1}\n{"b": 2}\n{"c": 3}'
        chunks = list(_iter_json_chunks(body))
        assert chunks == [{"a": 1}, {"b": 2}, {"c": 3}]

    def test_empty_string_yields_nothing(self):
        assert list(_iter_json_chunks("")) == []

    def test_whitespace_only_yields_nothing(self):
        assert list(_iter_json_chunks("   \n  ")) == []

    def test_skips_invalid_lines_in_ndjson(self):
        body = '{"a": 1}\nnot-json\n{"b": 2}'
        chunks = list(_iter_json_chunks(body))
        assert chunks == [{"a": 1}, {"b": 2}]

    def test_nested_object(self):
        obj = {"outer": {"inner": [1, 2, 3]}}
        chunks = list(_iter_json_chunks(json.dumps(obj)))
        assert chunks == [obj]

    def test_unicode_values(self):
        obj = {"key": "héllo wörld"}
        chunks = list(_iter_json_chunks(json.dumps(obj)))
        assert chunks[0]["key"] == "héllo wörld"


# ===========================================================================
# _extract_media_urls / _recursive_search
# ===========================================================================

class TestExtractMediaUrls:

    # ── video keys ──────────────────────────────────────────────────────────

    def test_video_hd_url(self):
        res = _extract_media_urls({"video_hd_url": "https://cdn.fb.com/v.mp4"})
        assert res["videos"] == ["https://cdn.fb.com/v.mp4"]
        assert res["images"] == []
        assert res["thumbnails"] == []

    def test_video_sd_url(self):
        res = _extract_media_urls({"video_sd_url": "https://cdn.fb.com/sd.mp4"})
        assert "https://cdn.fb.com/sd.mp4" in res["videos"]

    def test_playable_url(self):
        res = _extract_media_urls({"playable_url": "https://cdn.fb.com/play.mp4"})
        assert "https://cdn.fb.com/play.mp4" in res["videos"]

    def test_playable_url_quality_hd(self):
        res = _extract_media_urls({"playable_url_quality_hd": "https://cdn.fb.com/hd.mp4"})
        assert "https://cdn.fb.com/hd.mp4" in res["videos"]

    # ── image keys ──────────────────────────────────────────────────────────

    def test_image_url(self):
        res = _extract_media_urls({"image_url": "https://cdn.fb.com/img.jpg"})
        assert res["images"] == ["https://cdn.fb.com/img.jpg"]
        assert res["videos"] == []

    def test_original_image_url(self):
        res = _extract_media_urls({"original_image_url": "https://cdn.fb.com/orig.jpg"})
        assert "https://cdn.fb.com/orig.jpg" in res["images"]

    def test_resized_image_url(self):
        res = _extract_media_urls({"resized_image_url": "https://cdn.fb.com/resize.jpg"})
        assert "https://cdn.fb.com/resize.jpg" in res["images"]

    # ── thumbnail keys ───────────────────────────────────────────────────────

    def test_thumbnail_url(self):
        res = _extract_media_urls({"thumbnail_url": "https://cdn.fb.com/thumb.jpg"})
        assert res["thumbnails"] == ["https://cdn.fb.com/thumb.jpg"]

    def test_thumbnail_image_url(self):
        res = _extract_media_urls({"thumbnail_image_url": "https://cdn.fb.com/ti.jpg"})
        assert "https://cdn.fb.com/ti.jpg" in res["thumbnails"]

    # ── unknown / non-URL values are ignored ─────────────────────────────────

    def test_unknown_key_ignored(self):
        res = _extract_media_urls({"unknown_key": "https://cdn.fb.com/x.mp4"})
        assert res == {"videos": [], "images": [], "thumbnails": []}

    def test_non_http_value_ignored(self):
        res = _extract_media_urls({"video_hd_url": "ftp://cdn.fb.com/v.mp4"})
        assert res["videos"] == []

    def test_none_value_ignored(self):
        res = _extract_media_urls({"video_hd_url": None})
        assert res["videos"] == []

    def test_integer_value_ignored(self):
        res = _extract_media_urls({"video_hd_url": 12345})
        assert res["videos"] == []

    # ── deduplication ────────────────────────────────────────────────────────

    def test_duplicate_urls_deduplicated(self):
        obj = {
            "video_hd_url": "https://cdn.fb.com/v.mp4",
            "nested": {"video_hd_url": "https://cdn.fb.com/v.mp4"},
        }
        res = _extract_media_urls(obj)
        assert res["videos"].count("https://cdn.fb.com/v.mp4") == 1

    # ── recursion ────────────────────────────────────────────────────────────

    def test_nested_dict(self):
        obj = {"level1": {"level2": {"video_hd_url": "https://cdn.fb.com/deep.mp4"}}}
        res = _extract_media_urls(obj)
        assert "https://cdn.fb.com/deep.mp4" in res["videos"]

    def test_value_in_list(self):
        obj = {"edges": [{"video_hd_url": "https://cdn.fb.com/a.mp4"},
                          {"video_hd_url": "https://cdn.fb.com/b.mp4"}]}
        res = _extract_media_urls(obj)
        assert "https://cdn.fb.com/a.mp4" in res["videos"]
        assert "https://cdn.fb.com/b.mp4" in res["videos"]

    def test_mixed_types_in_list(self):
        obj = {"items": [1, None, "string", {"image_url": "https://cdn.fb.com/i.jpg"}]}
        res = _extract_media_urls(obj)
        assert "https://cdn.fb.com/i.jpg" in res["images"]

    def test_all_three_buckets_populated(self):
        obj = {
            "video_hd_url":   "https://cdn.fb.com/v.mp4",
            "image_url":      "https://cdn.fb.com/i.jpg",
            "thumbnail_url":  "https://cdn.fb.com/t.jpg",
        }
        res = _extract_media_urls(obj)
        assert len(res["videos"])     == 1
        assert len(res["images"])     == 1
        assert len(res["thumbnails"]) == 1

    def test_top_level_list(self):
        obj = [{"video_hd_url": "https://cdn.fb.com/v.mp4"}]
        res = _extract_media_urls(obj)
        assert "https://cdn.fb.com/v.mp4" in res["videos"]

    def test_empty_dict(self):
        assert _extract_media_urls({}) == {"videos": [], "images": [], "thumbnails": []}

    def test_empty_list(self):
        assert _extract_media_urls([]) == {"videos": [], "images": [], "thumbnails": []}

    def test_scalar_input(self):
        # Scalars are valid JSON — should return empty buckets, not raise
        assert _extract_media_urls(None) == {"videos": [], "images": [], "thumbnails": []}
        assert _extract_media_urls("hello") == {"videos": [], "images": [], "thumbnails": []}
        assert _extract_media_urls(42) == {"videos": [], "images": [], "thumbnails": []}

    def test_depth_cap_does_not_raise(self):
        # Build a 60-level deep dict — should not blow the stack
        obj: dict = {}
        cursor = obj
        for _ in range(60):
            cursor["child"] = {}
            cursor = cursor["child"]
        cursor["video_hd_url"] = "https://cdn.fb.com/deep.mp4"
        # Should complete without RecursionError; deep node may or may not be
        # found depending on the depth cap, but it must not raise.
        _extract_media_urls(obj)

    def test_multiple_videos_from_deeply_nested_list(self):
        obj = {
            "data": {
                "ads": [
                    {"creative": {"video_hd_url": "https://cdn.fb.com/1.mp4"}},
                    {"creative": {"video_sd_url": "https://cdn.fb.com/2.mp4"}},
                    {"creative": {"playable_url": "https://cdn.fb.com/3.mp4"}},
                ]
            }
        }
        res = _extract_media_urls(obj)
        assert len(res["videos"]) == 3

    def test_real_world_shaped_payload(self):
        """Simulate a simplified Facebook GraphQL ad payload."""
        payload = {
            "data": {
                "ad_library_main": {
                    "ad_cards": {
                        "edges": [
                            {
                                "node": {
                                    "ad_creative_bodies": ["Buy now!"],
                                    "snapshot": {
                                        "videos": [
                                            {
                                                "video_hd_url": "https://video.fcdn.net/hd.mp4",
                                                "video_sd_url": "https://video.fcdn.net/sd.mp4",
                                                "thumbnail_url": "https://static.fcdn.net/thumb.jpg",
                                            }
                                        ],
                                        "images": [
                                            {
                                                "original_image_url": "https://static.fcdn.net/img.jpg"
                                            }
                                        ],
                                    },
                                }
                            }
                        ]
                    }
                }
            }
        }
        res = _extract_media_urls(payload)
        assert "https://video.fcdn.net/hd.mp4"      in res["videos"]
        assert "https://video.fcdn.net/sd.mp4"       in res["videos"]
        assert "https://static.fcdn.net/thumb.jpg"   in res["thumbnails"]
        assert "https://static.fcdn.net/img.jpg"     in res["images"]


# ===========================================================================
# _parse_and_collect
# ===========================================================================

class TestParseAndCollect:
    def _make_result(self) -> tuple[MediaResult, threading.Lock]:
        return MediaResult(), threading.Lock()

    def test_collects_from_valid_json(self):
        result, lock = self._make_result()
        body = _body({"video_hd_url": "https://cdn.fb.com/v.mp4"})
        _parse_and_collect(body, result, lock)
        assert "https://cdn.fb.com/v.mp4" in result.videos

    def test_ignores_non_graphql_junk(self):
        result, lock = self._make_result()
        _parse_and_collect("this is not json at all", result, lock)
        assert result.total() == 0

    def test_ndjson_body(self):
        result, lock = self._make_result()
        body = (
            '{"video_hd_url": "https://cdn.fb.com/a.mp4"}\n'
            '{"image_url": "https://cdn.fb.com/b.jpg"}'
        )
        _parse_and_collect(body, result, lock)
        assert "https://cdn.fb.com/a.mp4" in result.videos
        assert "https://cdn.fb.com/b.jpg" in result.images

    def test_no_duplicates_across_multiple_calls(self):
        result, lock = self._make_result()
        body = _body({"video_hd_url": "https://cdn.fb.com/v.mp4"})
        _parse_and_collect(body, result, lock)
        _parse_and_collect(body, result, lock)
        assert result.videos.count("https://cdn.fb.com/v.mp4") == 1


# ===========================================================================
# _extract_ad_id_from_url
# ===========================================================================

class TestExtractAdIdFromUrl:
    def test_id_param(self):
        assert _extract_ad_id_from_url(
            "https://www.facebook.com/ads/library/?id=123456"
        ) == "123456"

    def test_ad_id_param(self):
        assert _extract_ad_id_from_url(
            "https://www.facebook.com/ads/library/?ad_id=789"
        ) == "789"

    def test_no_id_returns_none(self):
        assert _extract_ad_id_from_url(
            "https://www.facebook.com/ads/library/?country=US"
        ) is None

    def test_extra_params(self):
        url = "https://www.facebook.com/ads/library/?active_status=all&id=99&country=US"
        assert _extract_ad_id_from_url(url) == "99"


# ===========================================================================
# MediaResult helpers
# ===========================================================================

class TestMediaResult:
    def test_as_dict_shape(self):
        r = MediaResult(videos=["v"], images=["i"], thumbnails=["t"])
        d = r.as_dict()
        assert set(d.keys()) == {"videos", "images", "thumbnails"}
        assert d["videos"] == ["v"]

    def test_total(self):
        r = MediaResult(videos=["v1", "v2"], images=["i1"], thumbnails=[])
        assert r.total() == 3

    def test_empty_total(self):
        assert MediaResult().total() == 0


# ===========================================================================
# scrape_ad_library_url shim (monkey-patched)
# ===========================================================================

class TestScrapeAdLibraryUrlShim:
    """
    We patch scrape_ad_url at the module level so these tests never launch
    a real browser.
    """

    def test_returns_list_of_ad_records(self, monkeypatch):
        fake = MediaResult(
            videos=["https://cdn.fb.com/v.mp4"],
            images=["https://cdn.fb.com/i.jpg"],
            thumbnails=["https://cdn.fb.com/t.jpg"],
        )
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: fake)

        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=123", max_assets=20
        )
        assert isinstance(records, list)
        assert len(records) == 1
        assert isinstance(records[0], AdRecord)

    def test_assets_are_ad_asset_instances(self, monkeypatch):
        fake = MediaResult(
            videos=["https://cdn.fb.com/v.mp4"],
            images=["https://cdn.fb.com/i.jpg"],
        )
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: fake)

        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=123"
        )
        for asset in records[0].assets:
            assert isinstance(asset, AdAsset)

    def test_asset_types_are_correct(self, monkeypatch):
        fake = MediaResult(
            videos=["https://cdn.fb.com/v.mp4"],
            images=["https://cdn.fb.com/i.jpg"],
            thumbnails=["https://cdn.fb.com/t.jpg"],
        )
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: fake)

        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=123"
        )
        types = {a.asset_type for a in records[0].assets}
        assert types == {"video", "image", "thumbnail"}

    def test_max_assets_respected(self, monkeypatch):
        fake = MediaResult(
            videos=[f"https://cdn.fb.com/{i}.mp4" for i in range(10)],
            images=[f"https://cdn.fb.com/{i}.jpg" for i in range(10)],
        )
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: fake)

        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=123",
            max_assets=5,
        )
        assert len(records[0].assets) == 5

    def test_ad_id_extracted_from_url(self, monkeypatch):
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: MediaResult())
        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=999888"
        )
        assert records[0].ad_id == "999888"

    def test_unknown_ad_id_when_url_has_no_id(self, monkeypatch):
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: MediaResult())
        records = scrape_ad_library_url("https://www.facebook.com/ads/library/")
        assert records[0].ad_id == "unknown"

    def test_empty_media_result(self, monkeypatch):
        monkeypatch.setattr("backend.scraper.scrape_ad_url", lambda url: MediaResult())
        records = scrape_ad_library_url(
            "https://www.facebook.com/ads/library/?id=1"
        )
        assert records[0].assets == []


# ===========================================================================
# Playback-trigger helpers — mock Page
# ===========================================================================

class MockPage:
    """
    Minimal stand-in for a Playwright Page used by playback-trigger unit tests.

    Attributes
    ----------
    click_raises :
        If set, ``click()`` raises this exception instead of succeeding.
    click_calls :
        Accumulates every (selector, kwargs) call made to ``click()``.
    evaluate_return :
        Value returned by ``evaluate()``.
    evaluate_calls :
        Accumulates every JS expression passed to ``evaluate()``.
    wait_for_load_state_raises :
        If set, ``wait_for_load_state`` raises this exception (simulates timeout).
    wait_calls :
        Accumulates every call made to ``wait_for_load_state``.
    """

    def __init__(
        self,
        *,
        click_raises: Exception | None = None,
        evaluate_return: bool = True,
        wait_for_load_state_raises: Exception | None = None,
    ):
        self.click_raises = click_raises
        self.click_calls: list[tuple] = []
        self.evaluate_return = evaluate_return
        self.evaluate_calls: list[str] = []
        self.wait_for_load_state_raises = wait_for_load_state_raises
        self.wait_calls: list[tuple] = []

    def click(self, selector: str, **kwargs):
        self.click_calls.append((selector, kwargs))
        if self.click_raises:
            raise self.click_raises

    def evaluate(self, expression: str, **kwargs):
        self.evaluate_calls.append(expression)
        return self.evaluate_return

    def wait_for_load_state(self, state: str, **kwargs):
        self.wait_calls.append((state, kwargs))
        if self.wait_for_load_state_raises:
            raise self.wait_for_load_state_raises


# ---------------------------------------------------------------------------
# _try_click_play_button
# ---------------------------------------------------------------------------

class TestTryClickPlayButton:
    def test_returns_true_on_first_successful_click(self):
        page = MockPage()
        assert _try_click_play_button(page) is True

    def test_only_one_click_on_immediate_success(self):
        page = MockPage()
        _try_click_play_button(page)
        assert len(page.click_calls) == 1

    def test_returns_false_when_all_selectors_fail(self):
        page = MockPage(click_raises=Exception("not found"))
        assert _try_click_play_button(page) is False

    def test_tries_all_selectors_before_giving_up(self):
        page = MockPage(click_raises=Exception("not found"))
        _try_click_play_button(page)
        # Should have tried every selector in the list (≥ 4)
        assert len(page.click_calls) >= 4

    def test_stops_at_first_success(self):
        attempts = []

        class PartialPage(MockPage):
            def click(self, selector, **kwargs):
                attempts.append(selector)
                # Fail on the first selector, succeed on the second
                if len(attempts) < 2:
                    raise Exception("not found")

        _try_click_play_button(PartialPage())
        assert len(attempts) == 2

    def test_click_called_with_timeout_kwarg(self):
        page = MockPage()
        _try_click_play_button(page)
        _, kwargs = page.click_calls[0]
        assert "timeout" in kwargs


# ---------------------------------------------------------------------------
# _try_click_video_element
# ---------------------------------------------------------------------------

class TestTryClickVideoElement:
    def test_returns_true_when_video_exists(self):
        page = MockPage()
        assert _try_click_video_element(page) is True

    def test_clicks_video_selector(self):
        page = MockPage()
        _try_click_video_element(page)
        selector, _ = page.click_calls[0]
        assert selector == "video"

    def test_returns_false_when_no_video(self):
        page = MockPage(click_raises=Exception("No element"))
        assert _try_click_video_element(page) is False

    def test_click_called_with_timeout_kwarg(self):
        page = MockPage()
        _try_click_video_element(page)
        _, kwargs = page.click_calls[0]
        assert "timeout" in kwargs

    def test_no_extra_clicks_made(self):
        page = MockPage()
        _try_click_video_element(page)
        assert len(page.click_calls) == 1


# ---------------------------------------------------------------------------
# _try_js_play
# ---------------------------------------------------------------------------

class TestTryJsPlay:
    def test_returns_true_when_video_found(self):
        page = MockPage(evaluate_return=True)
        assert _try_js_play(page) is True

    def test_returns_false_when_no_video(self):
        page = MockPage(evaluate_return=False)
        assert _try_js_play(page) is False

    def test_evaluate_called_once(self):
        page = MockPage()
        _try_js_play(page)
        assert len(page.evaluate_calls) == 1

    def test_js_expression_targets_video(self):
        page = MockPage()
        _try_js_play(page)
        expr = page.evaluate_calls[0]
        assert "video" in expr.lower()

    def test_js_expression_calls_play(self):
        page = MockPage()
        _try_js_play(page)
        assert "play()" in page.evaluate_calls[0]

    def test_js_expression_dispatches_click_event(self):
        page = MockPage()
        _try_js_play(page)
        assert "dispatchEvent" in page.evaluate_calls[0]

    def test_falsy_evaluate_result_returns_false(self):
        page = MockPage(evaluate_return=None)  # type: ignore[arg-type]
        assert _try_js_play(page) is False


# ---------------------------------------------------------------------------
# _trigger_video_playback
# ---------------------------------------------------------------------------

class TestTriggerVideoPlayback:
    """Tests for the orchestrator that ties the three strategies together."""

    def _make(self, existing_videos: list[str] | None = None) -> tuple[MediaResult, threading.Lock]:
        r = MediaResult(videos=list(existing_videos or []))
        return r, threading.Lock()

    # ── No video element present ──────────────────────────────────────────

    def test_returns_empty_when_no_interaction_succeeds(self):
        page = MockPage(click_raises=Exception("no element"), evaluate_return=False)
        result, lock = self._make()
        new = _trigger_video_playback(page, result, lock)
        assert new == []

    def test_does_not_mutate_result_when_no_element(self):
        page = MockPage(click_raises=Exception("no element"), evaluate_return=False)
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        assert result.videos == []

    # ── Video element found, no new URLs emitted ──────────────────────────

    def test_returns_empty_when_no_new_urls_after_click(self):
        page = MockPage()   # click succeeds; wait_for_load_state does nothing
        result, lock = self._make()
        new = _trigger_video_playback(page, result, lock)
        assert new == []

    def test_wait_called_after_successful_interaction(self):
        page = MockPage()
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        assert len(page.wait_calls) == 1

    def test_wait_uses_playback_timeout(self):
        page = MockPage()
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        _, kwargs = page.wait_calls[0]
        assert kwargs.get("timeout") == PLAYBACK_TIMEOUT_MS

    def test_wait_requests_networkidle(self):
        page = MockPage()
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        state, _ = page.wait_calls[0]
        assert state == "networkidle"

    # ── New URLs discovered after playback ────────────────────────────────

    def test_returns_only_new_videos(self):
        """Simulate the response handler appending a URL during wait."""

        class AppendingPage(MockPage):
            def __init__(self, result_ref, lock_ref):
                super().__init__()
                self._result = result_ref
                self._lock = lock_ref

            def wait_for_load_state(self, state, **kwargs):
                super().wait_for_load_state(state, **kwargs)
                # Simulate a GraphQL response arriving during the wait
                with self._lock:
                    self._result.videos.append("https://cdn.fb.com/lazy.mp4")

        result, lock = self._make()
        page = AppendingPage(result, lock)
        new = _trigger_video_playback(page, result, lock)
        assert new == ["https://cdn.fb.com/lazy.mp4"]

    def test_existing_videos_not_included_in_return(self):
        """Pre-existing video URLs must not appear in the returned list."""

        class AppendingPage(MockPage):
            def __init__(self, result_ref, lock_ref):
                super().__init__()
                self._result = result_ref
                self._lock = lock_ref

            def wait_for_load_state(self, state, **kwargs):
                super().wait_for_load_state(state, **kwargs)
                with self._lock:
                    self._result.videos.append("https://cdn.fb.com/new.mp4")

        result, lock = self._make(existing_videos=["https://cdn.fb.com/old.mp4"])
        page = AppendingPage(result, lock)
        new = _trigger_video_playback(page, result, lock)
        assert "https://cdn.fb.com/old.mp4" not in new
        assert "https://cdn.fb.com/new.mp4" in new

    def test_multiple_new_urls_all_returned(self):
        class AppendingPage(MockPage):
            def __init__(self, result_ref, lock_ref):
                super().__init__()
                self._result = result_ref
                self._lock = lock_ref

            def wait_for_load_state(self, state, **kwargs):
                super().wait_for_load_state(state, **kwargs)
                with self._lock:
                    self._result.videos.extend([
                        "https://cdn.fb.com/a.mp4",
                        "https://cdn.fb.com/b.mp4",
                    ])

        result, lock = self._make()
        page = AppendingPage(result, lock)
        new = _trigger_video_playback(page, result, lock)
        assert set(new) == {"https://cdn.fb.com/a.mp4", "https://cdn.fb.com/b.mp4"}

    # ── Timeout resilience ────────────────────────────────────────────────

    def test_timeout_during_wait_does_not_raise(self):
        page = MockPage(wait_for_load_state_raises=Exception("Timeout 3000ms exceeded"))
        result, lock = self._make()
        # Must not propagate the exception
        new = _trigger_video_playback(page, result, lock)
        assert isinstance(new, list)

    def test_returns_urls_collected_before_timeout(self):
        """URLs added to result before the timeout fires must still be returned."""

        class TimeoutPage(MockPage):
            def __init__(self, result_ref, lock_ref):
                super().__init__(
                    wait_for_load_state_raises=Exception("Timeout 3000ms exceeded")
                )
                self._result = result_ref
                self._lock = lock_ref

            def wait_for_load_state(self, state, **kwargs):
                # Append URL before raising timeout
                with self._lock:
                    self._result.videos.append("https://cdn.fb.com/partial.mp4")
                super().wait_for_load_state(state, **kwargs)

        result, lock = self._make()
        page = TimeoutPage(result, lock)
        new = _trigger_video_playback(page, result, lock)
        assert "https://cdn.fb.com/partial.mp4" in new

    # ── Strategy priority ─────────────────────────────────────────────────

    def test_play_button_tried_before_video_element(self):
        """If the play button click succeeds, video element should not be tried."""
        page = MockPage()   # click always succeeds
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        selectors_tried = [s for s, _ in page.click_calls]
        # Should have stopped after one click (the play button)
        assert len(selectors_tried) == 1

    def test_js_play_used_when_all_clicks_fail(self):
        page = MockPage(click_raises=Exception("no element"), evaluate_return=True)
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        assert len(page.evaluate_calls) == 1

    def test_no_wait_when_no_interaction_succeeds(self):
        """wait_for_load_state must NOT be called if nothing was clicked."""
        page = MockPage(click_raises=Exception("no element"), evaluate_return=False)
        result, lock = self._make()
        _trigger_video_playback(page, result, lock)
        assert page.wait_calls == []
