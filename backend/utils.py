"""
utils.py — Shared utilities used across the backend and workers.
"""

from __future__ import annotations

import logging
import os
import sys
from functools import lru_cache

import redis as redis_lib


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
def get_redis_connection() -> redis_lib.Redis:
    """
    Return a cached Redis connection.

    The connection is lazy — it won't actually connect until a command
    is issued, so this is safe to call at import time.
    """
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
