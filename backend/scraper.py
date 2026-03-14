"""
scraper.py — Browser automation layer using Playwright.

Responsibilities:
  • Launch a headless Chromium browser via Playwright.
  • Navigate to a Facebook Ad Library URL.
  • Extract ad metadata and direct media URLs (images / videos).
  • Return structured data consumed by downloader.py.

All functions are *synchronous* so they can be called directly from RQ workers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse, parse_qs

from playwright.sync_api import sync_playwright, Page, Browser, BrowserContext

from utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class AdAsset:
    """A single downloadable creative asset (image or video)."""

    asset_type: str          # "image" | "video" | "unknown"
    url: str
    alt_text: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None


@dataclass
class AdRecord:
    """Metadata and assets for a single ad."""

    ad_id: str
    page_name: str = ""
    ad_text: str = ""
    cta_text: str = ""
    assets: list[AdAsset] = field(default_factory=list)
    raw_url: str = ""


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def scrape_ad_library_url(url: str, max_assets: int = 20) -> list[AdRecord]:
    """
    Entry point for scraping a Facebook Ad Library URL.

    Args:
        url:        The Ad Library search or ad-detail URL.
        max_assets: Maximum number of ad records to extract.

    Returns:
        A list of :class:`AdRecord` objects with populated asset URLs.
    """
    logger.info("Starting scrape for URL: %s", url)

    with sync_playwright() as pw:
        browser = _launch_browser(pw)
        context = _create_context(browser)
        page = context.new_page()

        try:
            records = _scrape(page, url, max_assets)
        finally:
            page.close()
            context.close()
            browser.close()

    logger.info("Scrape complete — %d ad records extracted", len(records))
    return records


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _launch_browser(pw) -> Browser:
    """Launch a stealth-configured headless Chromium instance."""
    return pw.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--disable-dev-shm-usage",
        ],
    )


def _create_context(browser: Browser) -> BrowserContext:
    """Create a browser context with a realistic user-agent."""
    return browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 900},
        locale="en-US",
    )


def _scrape(page: Page, url: str, max_assets: int) -> list[AdRecord]:
    """
    Navigate to *url* and extract ad records.

    TODO: Replace placeholder logic with real DOM selectors once the
          target page structure has been mapped.
    """
    records: list[AdRecord] = []

    # --- Navigate ---
    logger.debug("Navigating to %s", url)
    page.goto(url, wait_until="domcontentloaded", timeout=30_000)
    _dismiss_cookie_banner(page)

    # --- Detect page type ---
    if _is_search_results_page(url):
        records = _scrape_search_results(page, max_assets)
    else:
        record = _scrape_single_ad(page, url)
        if record:
            records.append(record)

    return records


def _dismiss_cookie_banner(page: Page) -> None:
    """Attempt to dismiss a cookie consent overlay if present."""
    try:
        page.click('[data-cookiebanner="accept_button"]', timeout=3_000)
    except Exception:
        pass  # Banner not present — carry on


def _is_search_results_page(url: str) -> bool:
    """Heuristic: search pages contain 'q=' or '/ads/library' without an ad_id."""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    return "q" in qs or "ad_id" not in qs


def _scrape_search_results(page: Page, max_assets: int) -> list[AdRecord]:
    """
    Scrape multiple ad cards from a search-results page.

    TODO: Implement real selectors.
          Suggested approach:
            1. Wait for ad-card containers to load.
            2. Iterate cards up to max_assets.
            3. For each card call _extract_card_data().
    """
    logger.debug("Scraping search-results page (placeholder)")
    # Placeholder — returns empty list until selectors are implemented
    return []


def _scrape_single_ad(page: Page, url: str) -> Optional[AdRecord]:
    """
    Scrape a single ad-detail page.

    TODO: Implement real selectors.
          Suggested approach:
            1. Wait for '[data-ad-id]' or equivalent container.
            2. Extract page name, body text, CTA.
            3. Collect image <img> src and video <source> src attributes.
    """
    logger.debug("Scraping single-ad page (placeholder)")
    ad_id = _extract_ad_id_from_url(url)
    return AdRecord(ad_id=ad_id or "unknown", raw_url=url)


def _extract_ad_id_from_url(url: str) -> Optional[str]:
    """Parse 'ad_id' from an Ad Library URL query string."""
    qs = parse_qs(urlparse(url).query)
    ids = qs.get("id", qs.get("ad_id", []))
    return ids[0] if ids else None


def _extract_card_data(card_element) -> Optional[AdRecord]:
    """
    Convert a Playwright element handle for a single ad card into an AdRecord.

    TODO: Implement once real selectors are known.
    """
    raise NotImplementedError("_extract_card_data not yet implemented")
