"""
tasks.py — RQ task definitions executed by the worker pool.

Each function in this module is the entry point for a background job.
RQ workers import and execute these directly.
"""

from __future__ import annotations

import sys
import os

# Ensure the repo root is on the path so backend modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.scraper import scrape_ad_library_url
from backend.downloader import download_media_assets
from backend.utils import get_logger, sanitise_url

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def run_ad_download(url: str, job_id: str, ad_id: str | None = None, max_assets: int = 20) -> dict:
    """
    Full pipeline: scrape the Ad Library URL then download all found assets.

    This function is enqueued by api.py and executed by worker.py.

    Args:
        url:        Facebook Ad Library URL to process.
        job_id:     Unique identifier assigned by the API (used as output folder name).
        ad_id:      Optional specific ad ID to filter on.
        max_assets: Maximum number of assets to download.

    Returns:
        Result dict passed back through RQ::

            {
                "job_id": str,
                "scraped_count": int,
                "assets": [...],
                "errors": [...],
            }
    """
    url = sanitise_url(url)
    logger.info("[job:%s] Starting pipeline for %s", job_id, url)

    # --- Step 1: Scrape ---
    try:
        records = scrape_ad_library_url(url, max_assets=max_assets)
    except Exception as exc:
        logger.error("[job:%s] Scrape failed: %s", job_id, exc)
        raise

    logger.info("[job:%s] Scrape yielded %d ad records", job_id, len(records))

    # --- Step 2: Download ---
    try:
        result = download_media_assets(records, job_id=job_id)
    except Exception as exc:
        logger.error("[job:%s] Download failed: %s", job_id, exc)
        raise

    result["scraped_count"] = len(records)
    logger.info("[job:%s] Pipeline complete — %d assets saved", job_id, len(result.get("assets", [])))
    return result
