"""
worker.py — RQ worker entry point.

Start one or more of these processes to consume jobs from the Redis queue:

    python workers/worker.py

In production, run via the docker-compose service definition or a process
manager such as supervisord / systemd.
"""

from __future__ import annotations

import os
import sys
import signal
import logging

# Allow backend imports when the worker is started from the repo root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rq import Worker, Queue, Connection
from redis import Redis

from backend.utils import get_redis_connection, get_logger, REDIS_URL

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

QUEUES = os.environ.get("RQ_QUEUES", "default").split(",")
WORKER_NAME = os.environ.get("RQ_WORKER_NAME", None)  # auto-generated if None
BURST_MODE = os.environ.get("RQ_BURST", "false").lower() in {"1", "true", "yes"}

# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_worker_instance: Worker | None = None


def _handle_sigterm(signum, frame):
    """Allow the current job to finish before exiting."""
    logger.info("SIGTERM received — requesting warm shutdown")
    if _worker_instance:
        _worker_instance.request_stop(signum, frame)


signal.signal(signal.SIGTERM, _handle_sigterm)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    conn = get_redis_connection()
    queues = [Queue(name, connection=conn) for name in QUEUES]

    logger.info(
        "Starting RQ worker | queues=%s burst=%s redis=%s",
        QUEUES, BURST_MODE, REDIS_URL,
    )

    global _worker_instance
    with Connection(conn):
        _worker_instance = Worker(
            queues=queues,
            name=WORKER_NAME,
            connection=conn,
        )
        _worker_instance.work(burst=BURST_MODE, with_scheduler=True)


if __name__ == "__main__":
    main()
