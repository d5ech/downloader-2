"""
downloader.py — HTTP asset downloader.

Responsibilities:
  • Accept a list of AdRecord objects (from scraper.py).
  • Download each asset URL to a local directory using the requests library.
  • Return a manifest of successfully saved files.
"""

from __future__ import annotations

import hashlib
import mimetypes
import os
from pathlib import Path
from typing import Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from scraper import AdRecord, AdAsset
from utils import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DOWNLOAD_ROOT = Path(os.environ.get("DOWNLOAD_ROOT", "media/downloads"))
CHUNK_SIZE = 1024 * 256          # 256 KB streaming chunks
REQUEST_TIMEOUT = (10, 60)       # (connect, read) seconds
MAX_RETRIES = 3
ALLOWED_MIME_TYPES = {
    "image/jpeg", "image/png", "image/gif", "image/webp",
    "video/mp4", "video/webm", "video/quicktime",
}

# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------


def download_media_assets(
    records: list[AdRecord],
    job_id: str,
) -> dict:
    """
    Download all assets from *records* into a job-specific subdirectory.

    Args:
        records:  Ad records produced by scraper.scrape_ad_library_url().
        job_id:   Unique identifier for this download batch (used as folder name).

    Returns:
        A result dict::

            {
                "job_id": str,
                "assets": [
                    {
                        "ad_id": str,
                        "filename": str,
                        "asset_type": str,
                        "size_bytes": int,
                        "url": str,
                    },
                    ...
                ],
                "errors": [{"url": str, "error": str}, ...],
            }
    """
    dest_dir = DOWNLOAD_ROOT / job_id
    dest_dir.mkdir(parents=True, exist_ok=True)

    session = _build_session()
    downloaded: list[dict] = []
    errors: list[dict] = []

    for record in records:
        for asset in record.assets:
            try:
                file_info = _download_asset(session, asset, record.ad_id, dest_dir)
                if file_info:
                    downloaded.append(file_info)
            except Exception as exc:
                logger.error("Failed to download %s: %s", asset.url, exc)
                errors.append({"url": asset.url, "error": str(exc)})

    logger.info(
        "Download complete — %d succeeded, %d failed",
        len(downloaded), len(errors),
    )
    return {"job_id": job_id, "assets": downloaded, "errors": errors}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_session() -> requests.Session:
    """Return a requests.Session with retry logic and a realistic UA header."""
    session = requests.Session()
    retry = Retry(
        total=MAX_RETRIES,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=["GET", "HEAD"],
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Referer": "https://www.facebook.com/",
        }
    )
    return session


def _download_asset(
    session: requests.Session,
    asset: AdAsset,
    ad_id: str,
    dest_dir: Path,
) -> Optional[dict]:
    """
    Stream a single asset to disk.

    Returns file metadata dict on success, or None if the asset should be skipped.
    Raises on unrecoverable errors.
    """
    logger.debug("Downloading %s", asset.url)
    response = session.get(asset.url, timeout=REQUEST_TIMEOUT, stream=True)
    response.raise_for_status()

    content_type = response.headers.get("Content-Type", "").split(";")[0].strip()

    if content_type not in ALLOWED_MIME_TYPES:
        logger.warning("Skipping unsupported MIME type %r for %s", content_type, asset.url)
        return None

    extension = mimetypes.guess_extension(content_type) or ".bin"
    # Normalise a couple of common quirks from the stdlib
    extension = {"jpeg": ".jpg", "jpe": ".jpg"}.get(extension.lstrip("."), extension)

    filename = _safe_filename(ad_id, asset.url, extension)
    dest_path = dest_dir / filename

    size_bytes = _stream_to_disk(response, dest_path)

    return {
        "ad_id": ad_id,
        "filename": filename,
        "asset_type": asset.asset_type,
        "size_bytes": size_bytes,
        "url": asset.url,
    }


def _stream_to_disk(response: requests.Response, dest: Path) -> int:
    """Write the response body to *dest* in chunks; return total bytes written."""
    total = 0
    with dest.open("wb") as fh:
        for chunk in response.iter_content(chunk_size=CHUNK_SIZE):
            if chunk:
                fh.write(chunk)
                total += len(chunk)
    return total


def _safe_filename(ad_id: str, url: str, extension: str) -> str:
    """
    Derive a collision-resistant, filesystem-safe filename from the ad ID and URL.
    Format: <ad_id>_<url_hash><extension>   e.g. 123456_a3f9b2.jpg
    """
    url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
    safe_id = re.sub(r"[^\w-]", "_", ad_id)[:32]
    return f"{safe_id}_{url_hash}{extension}"


# Late import to avoid circular issues in unit tests
import re  # noqa: E402
