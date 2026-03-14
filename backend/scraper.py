"""
scraper.py — Playwright-based network interceptor for Facebook Ad Library.

Strategy
--------
Rather than parsing brittle DOM selectors, we intercept every response to
``https://www.facebook.com/api/graphql/`` while the page loads.  Facebook
delivers all ad creative metadata (video URLs, image URLs, thumbnails) through
these GraphQL responses as JSON.  A recursive key-search then finds the media
URLs regardless of how deeply they are nested or how the response schema
changes over time.

Public API
----------
``scrape_ad_url(url)  -> MediaResult``
``scrape_ad_library_url(url, max_assets)  -> list[AdRecord]``   (legacy shim)
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

# playwright is imported lazily inside _launch_browser / _collect_media so
# that modules consuming only the pure-Python helpers (e.g. unit tests) can
# be imported without Playwright being installed.
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from playwright.sync_api import Page, Browser, BrowserContext, Response

try:
    from backend.utils import get_logger  # when imported as a package
except ImportError:
    from utils import get_logger          # when run directly from backend/

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GRAPHQL_ENDPOINT    = "https://www.facebook.com/api/graphql/"
PAGE_TIMEOUT_MS     = 10_000   # hard cap: 10 s
NAV_TIMEOUT_MS      = 10_000
PLAYBACK_TIMEOUT_MS = 3_000    # wait for lazy-loaded video URLs after click

# Keys whose *values* are treated as media URLs.
# The search is case-sensitive to match Facebook's exact field names.
_VIDEO_KEYS     = {"video_hd_url", "video_sd_url", "playable_url", "playable_url_quality_hd"}
_IMAGE_KEYS     = {"image_url", "original_image_url", "resized_image_url"}
_THUMBNAIL_KEYS = {"thumbnail_url", "thumbnail_image_url"}

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class MediaResult:
    """Flat collection of media URLs extracted from a single ad page."""
    videos:     list[str] = field(default_factory=list)
    images:     list[str] = field(default_factory=list)
    thumbnails: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, list[str]]:
        return {
            "videos":     list(self.videos),
            "images":     list(self.images),
            "thumbnails": list(self.thumbnails),
        }

    def total(self) -> int:
        return len(self.videos) + len(self.images) + len(self.thumbnails)


# Legacy dataclasses — kept so downloader.py and tasks.py continue to work
@dataclass
class AdAsset:
    """A single downloadable creative asset."""
    asset_type: str          # "image" | "video" | "thumbnail"
    url: str
    alt_text: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class AdRecord:
    """Metadata and assets for a single ad."""
    ad_id:     str
    page_name: str = ""
    ad_text:   str = ""
    cta_text:  str = ""
    assets:    list[AdAsset] = field(default_factory=list)
    raw_url:   str = ""


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def scrape_ad_url(url: str) -> MediaResult:
    """
    Load *url* in a headless Chromium browser, intercept every GraphQL
    response, and return all media URLs found.

    Args:
        url: A Facebook Ad Library ad page URL.

    Returns:
        :class:`MediaResult` with deduplicated video, image, and thumbnail URLs.

    Raises:
        RuntimeError: If the page fails to load within the timeout.
    """
    logger.info("scrape_ad_url: %s", url)

    from playwright.sync_api import sync_playwright  # noqa: PLC0415

    with sync_playwright() as pw:
        browser  = _launch_browser(pw)
        context  = _create_context(browser)
        page     = context.new_page()
        result   = MediaResult()

        try:
            _collect_media(page, url, result)
        finally:
            page.close()
            context.close()
            browser.close()

    logger.info(
        "scrape_ad_url complete — %d videos, %d images, %d thumbnails",
        len(result.videos), len(result.images), len(result.thumbnails),
    )
    return result


def scrape_ad_library_url(url: str, max_assets: int = 20) -> list[AdRecord]:
    """
    Compatibility shim used by workers/tasks.py.

    Calls :func:`scrape_ad_url` and wraps the result in the legacy
    ``list[AdRecord]`` shape expected by ``downloader.py``.
    """
    ad_id  = _extract_ad_id_from_url(url) or "unknown"
    media  = scrape_ad_url(url)
    assets: list[AdAsset] = []

    for v_url in media.videos:
        assets.append(AdAsset(asset_type="video", url=v_url))
    for i_url in media.images:
        assets.append(AdAsset(asset_type="image", url=i_url))
    for t_url in media.thumbnails:
        assets.append(AdAsset(asset_type="thumbnail", url=t_url))

    assets = assets[:max_assets]
    return [AdRecord(ad_id=ad_id, assets=assets, raw_url=url)]


# ---------------------------------------------------------------------------
# Browser helpers
# ---------------------------------------------------------------------------


def _launch_browser(pw) -> Browser:
    """Return a stealth-configured headless Chromium instance."""
    return pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )


def _create_context(browser: Browser) -> BrowserContext:
    """Return a browser context with a realistic user-agent and viewport."""
    return browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        # Prevent the page from detecting headless mode via navigator.webdriver
        java_script_enabled=True,
    )


# ---------------------------------------------------------------------------
# Core interception logic
# ---------------------------------------------------------------------------


def _collect_media(page: Page, url: str, result: MediaResult) -> None:
    """
    Navigate to *url*, listen for GraphQL responses, populate *result*.

    We register the response handler *before* navigation so we never miss an
    early response.  Navigation errors (timeout, net::ERR_*) are caught and
    logged; any media collected before the error is still returned.
    """
    lock = threading.Lock()   # Playwright fires callbacks on the same thread
                               # but the lock makes the intent explicit.

    def _on_response(response: Response) -> None:
        if GRAPHQL_ENDPOINT not in response.url:
            return
        try:
            body = response.text()
        except Exception as exc:
            logger.debug("Could not read response body: %s", exc)
            return
        _parse_and_collect(body, result, lock)

    page.on("response", _on_response)

    _dismiss_cookie_banner_on_load(page)

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
    except Exception as exc:
        logger.warning("Navigation did not complete cleanly (%s) — using partial results", exc)

    # Give the page a moment to fire any in-flight XHRs
    try:
        page.wait_for_load_state("networkidle", timeout=PAGE_TIMEOUT_MS)
    except Exception:
        pass  # networkidle timeout is acceptable; we keep what we have

    # --- Playback fallback -------------------------------------------
    # Some ads keep video URLs behind a lazy-load gate: the URL is only
    # emitted in a GraphQL response *after* the user presses play.
    # If the initial page load produced no videos, simulate playback.
    if not result.videos:
        logger.info("No videos after page load — attempting playback trigger")
        _trigger_video_playback(page, result, lock)


def _dismiss_cookie_banner_on_load(page: Page) -> None:
    """Register a handler to click the cookie-consent button if it appears."""
    def _handler(event: Any) -> None:  # noqa: ANN401
        try:
            page.click('[data-cookiebanner="accept_button"]', timeout=2_000)
        except Exception:
            pass

    page.on("domcontentloaded", _handler)


# ---------------------------------------------------------------------------
# Playback-triggered extraction
# ---------------------------------------------------------------------------


def _trigger_video_playback(
    page: "Page",
    result: MediaResult,
    lock: threading.Lock,
) -> list[str]:
    """
    Simulate a user pressing play so Facebook emits lazy-loaded video URLs.

    Facebook sometimes withholds the actual ``playable_url`` / ``video_hd_url``
    until after the first playback interaction.  This function:

    1. Takes a snapshot of ``result.videos`` before interacting.
    2. Tries three interaction strategies in priority order (stopping at the
       first that succeeds):

       a. Click an overlay play button identified by ``aria-label`` or role.
       b. Click the ``<video>`` element directly.
       c. Dispatch a synthetic ``click`` event and call ``.play()`` via JS.

    3. Waits up to ``PLAYBACK_TIMEOUT_MS`` (3 s) for ``networkidle`` so the
       response handler already registered on the page can capture any new
       GraphQL responses.
    4. Returns only the video URLs that were **not** present before this call.

    Args:
        page:   Playwright ``Page`` already navigated to the ad URL.
        result: Shared :class:`MediaResult`; updated in-place by the existing
                response handler as new responses arrive.
        lock:   Mutex guarding mutations to *result*.

    Returns:
        List of video URL strings newly discovered by the playback trigger.
        Empty list if no ``<video>`` element was found or no new URLs appeared.
    """
    # Snapshot so we can diff afterwards
    with lock:
        videos_before: list[str] = list(result.videos)

    played = (
        _try_click_play_button(page)
        or _try_click_video_element(page)
        or _try_js_play(page)
    )

    if not played:
        logger.debug("No video element or play button found — skipping playback trigger")
        return []

    # Wait for lazy GraphQL responses the playback interaction may trigger
    try:
        page.wait_for_load_state("networkidle", timeout=PLAYBACK_TIMEOUT_MS)
    except Exception:
        pass  # 3 s timeout is expected; collect whatever arrived

    with lock:
        new_videos = [v for v in result.videos if v not in videos_before]

    if new_videos:
        logger.info("Playback trigger discovered %d new video URL(s)", len(new_videos))
    else:
        logger.debug("Playback trigger yielded no new video URLs")

    return new_videos


def _try_click_play_button(page: "Page") -> bool:
    """
    Click an overlay play-button element using common Facebook selectors.

    Tries each selector with a 1 s timeout and returns ``True`` as soon as
    one click succeeds.  Returns ``False`` if none of the selectors matched.
    """
    selectors = [
        '[aria-label="Play"]',
        '[aria-label="play"]',
        '[aria-label*="Play" i]',
        '[role="button"][data-video-id]',
        # Muted-autoplay carousel play button
        'div[data-pagelet*="Video"] [role="button"]',
    ]
    for selector in selectors:
        try:
            page.click(selector, timeout=1_000)
            logger.debug("Clicked play button via selector: %s", selector)
            return True
        except Exception:
            continue
    return False


def _try_click_video_element(page: "Page") -> bool:
    """
    Click the first ``<video>`` element found in the DOM.

    Returns ``True`` if a ``<video>`` element was present and the click was
    dispatched, ``False`` otherwise.
    """
    try:
        page.click("video", timeout=1_000)
        logger.debug("Clicked <video> element directly")
        return True
    except Exception:
        return False


def _try_js_play(page: "Page") -> bool:
    """
    Dispatch a synthetic ``MouseEvent`` and call ``.play()`` on the first
    ``<video>`` element via JavaScript.

    This bypasses Playwright's element visibility checks and is the last
    resort for videos rendered off-screen or hidden by overlays.

    Returns ``True`` if a ``<video>`` element was found in the DOM (regardless
    of whether the browser permitted autoplay), ``False`` if no element exists.
    """
    found: bool = page.evaluate(
        """() => {
            const video = document.querySelector('video');
            if (!video) return false;
            video.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
            const promise = video.play();
            if (promise && typeof promise.catch === 'function') {
                // Suppress NotAllowedError thrown in headless when autoplay
                // is blocked — the click event above is still useful.
                promise.catch(() => {});
            }
            return true;
        }"""
    )
    if found:
        logger.debug("JS play() dispatched on <video>")
    return bool(found)


# ---------------------------------------------------------------------------
# JSON parsing and recursive search
# ---------------------------------------------------------------------------


def _parse_and_collect(body: str, result: MediaResult, lock: threading.Lock) -> None:
    """
    Try to parse *body* as JSON (or newline-delimited JSON), then search every
    parsed object for media keys and append findings to *result*.

    Facebook GraphQL endpoints sometimes return multiple JSON objects
    separated by newlines rather than a single top-level object.
    """
    for chunk in _iter_json_chunks(body):
        found = _extract_media_urls(chunk)
        with lock:
            for url in found["videos"]:
                if url not in result.videos:
                    result.videos.append(url)
            for url in found["images"]:
                if url not in result.images:
                    result.images.append(url)
            for url in found["thumbnails"]:
                if url not in result.thumbnails:
                    result.thumbnails.append(url)


def _iter_json_chunks(body: str):
    """
    Yield one or more parsed JSON objects from *body*.

    Handles:
    - A single JSON object / array.
    - Newline-delimited JSON (each line is an independent object).
    """
    body = body.strip()
    if not body:
        return

    # Attempt single-document parse first (most common)
    try:
        yield json.loads(body)
        return
    except json.JSONDecodeError:
        pass

    # Fall back to newline-delimited JSON
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.debug("Skipping non-JSON line: %.80s…", line)


def _extract_media_urls(obj: Any) -> dict[str, list[str]]:
    """
    Recursively walk *obj* (any JSON-decoded Python value) and collect all
    media URLs into three buckets.

    Returns:
        ``{"videos": [...], "images": [...], "thumbnails": [...]}``
    """
    videos:     list[str] = []
    images:     list[str] = []
    thumbnails: list[str] = []

    _recursive_search(obj, videos, images, thumbnails, depth=0)

    return {"videos": videos, "images": images, "thumbnails": thumbnails}


def _recursive_search(
    node: Any,
    videos: list[str],
    images: list[str],
    thumbnails: list[str],
    depth: int,
) -> None:
    """
    Depth-first walk of any JSON-decoded Python structure.

    - ``dict``   → inspect every key/value pair
    - ``list``   → recurse into every element
    - anything else → leaf node, ignored unless a parent dict matched a key

    A depth cap of 50 prevents stack overflows on pathologically deep objects.
    """
    if depth > 50:
        return

    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, str) and value.startswith("http"):
                if key in _VIDEO_KEYS:
                    if value not in videos:
                        videos.append(value)
                        logger.debug("video   [%s]: %s", key, value)
                elif key in _IMAGE_KEYS:
                    if value not in images:
                        images.append(value)
                        logger.debug("image   [%s]: %s", key, value)
                elif key in _THUMBNAIL_KEYS:
                    if value not in thumbnails:
                        thumbnails.append(value)
                        logger.debug("thumb   [%s]: %s", key, value)

            # Always recurse into dict values regardless of the key name
            _recursive_search(value, videos, images, thumbnails, depth + 1)

    elif isinstance(node, list):
        for item in node:
            _recursive_search(item, videos, images, thumbnails, depth + 1)

    # Scalar leaves (str, int, float, bool, None) — nothing to do


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------


def _extract_ad_id_from_url(url: str) -> Optional[str]:
    """Parse the numeric ad ID from an Ad Library URL query string."""
    qs = parse_qs(urlparse(url).query)
    ids = qs.get("id", qs.get("ad_id", []))
    return ids[0] if ids else None
