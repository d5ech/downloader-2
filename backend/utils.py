"""
utils.py — Shared utilities used across the backend and workers.
"""

from __future__ import annotations

import logging
import os
import re
import sys
from functools import lru_cache
from urllib.parse import urlparse, parse_qs

# redis is imported lazily inside get_redis_connection() so that modules
# which only use parse_ad_url (or other non-Redis helpers) can be imported
# without Redis being installed in the environment.


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s — %(message)s"
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger with a consistent format."""
    logging.basicConfig(
        level=LOG_LEVEL,
        format=LOG_FORMAT,
        stream=sys.stdout,
    )
    return logging.getLogger(name)


# ---------------------------------------------------------------------------
# Redis
# ---------------------------------------------------------------------------

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


@lru_cache(maxsize=1)
def get_redis_connection():
    """
    Return a cached Redis connection.

    The connection is lazy — it won't actually connect until a command
    is issued, so this is safe to call at import time.
    """
    import redis as redis_lib  # noqa: PLC0415
    return redis_lib.from_url(REDIS_URL, decode_responses=False)


def ping_redis() -> bool:
    """Return True if Redis is reachable."""
    try:
        get_redis_connection().ping()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------


def require_env(key: str) -> str:
    """
    Return the value of *key* from the environment.
    Raises RuntimeError if the variable is not set.
    """
    value = os.environ.get(key)
    if value is None:
        raise RuntimeError(f"Required environment variable {key!r} is not set.")
    return value


def bool_env(key: str, default: bool = False) -> bool:
    """Parse a boolean environment variable (1/true/yes → True)."""
    raw = os.environ.get(key, str(default)).lower()
    return raw in {"1", "true", "yes"}


# ---------------------------------------------------------------------------
# Ad URL parsing
# ---------------------------------------------------------------------------

_AD_LIBRARY_BASE = "https://www.facebook.com/ads/library/"
# Matches a string that is entirely decimal digits (1–20 chars)
_NUMERIC_ID_RE = re.compile(r"^\d{1,20}$")


class AdUrlError(ValueError):
    """Raised when *parse_ad_url* receives an invalid input."""


def parse_ad_url(input: str) -> dict:
    """
    Parse a Facebook Ad Library URL or bare ad ID and return a canonical dict.

    Accepted inputs
    ---------------
    - Full URL:  ``https://www.facebook.com/ads/library/?id=1457638199042352``
    - Bare ID:   ``1457638199042352``

    Returns
    -------
    .. code-block:: python

        {
            "ad_id": "1457638199042352",
            "url": "https://www.facebook.com/ads/library/?id=1457638199042352",
        }

    Raises
    ------
    AdUrlError
        If the input is empty, contains no recognisable ad ID, or the ID is
        not a valid numeric string.
    """
    if not isinstance(input, str):
        raise AdUrlError(f"Input must be a string, got {type(input).__name__!r}.")

    raw = input.strip()
    if not raw:
        raise AdUrlError("Input must not be empty.")

    ad_id: str | None = None

    # ── Branch 1: looks like a URL ──────────────────────────────────────────
    if raw.startswith("http://") or raw.startswith("https://") or raw.startswith("www."):
        parsed = urlparse(raw if "://" in raw else "https://" + raw)

        # Must be a Facebook Ad Library URL
        host = parsed.netloc.lstrip("www.")
        if not host.startswith("facebook.com"):
            raise AdUrlError(
                f"URL host {parsed.netloc!r} is not facebook.com."
            )
        if not parsed.path.startswith("/ads/library"):
            raise AdUrlError(
                "URL path does not point to the Facebook Ad Library "
                f"(/ads/library); got {parsed.path!r}."
            )

        qs = parse_qs(parsed.query)
        candidates = qs.get("id", qs.get("ad_id", []))
        if not candidates:
            raise AdUrlError(
                "No 'id' or 'ad_id' parameter found in the Ad Library URL."
            )
        ad_id = candidates[0].strip()

    # ── Branch 2: bare numeric ID ───────────────────────────────────────────
    elif _NUMERIC_ID_RE.match(raw):
        ad_id = raw

    # ── Branch 3: unrecognised format ───────────────────────────────────────
    else:
        raise AdUrlError(
            f"Cannot parse {raw!r}: expected a Facebook Ad Library URL or a "
            "numeric ad ID."
        )

    # ── Validate the extracted ID ───────────────────────────────────────────
    if not _NUMERIC_ID_RE.match(ad_id):
        raise AdUrlError(
            f"Ad ID {ad_id!r} is not a valid numeric identifier."
        )

    canonical_url = f"{_AD_LIBRARY_BASE}?id={ad_id}"
    return {"ad_id": ad_id, "url": canonical_url}


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def sanitise_url(url: str) -> str:
    """
    Basic URL sanitisation: strip whitespace and enforce https.

    TODO: Add more robust validation / normalisation as needed.
    """
    url = url.strip()
    if url.startswith("http://"):
        url = "https://" + url[7:]
    return url
