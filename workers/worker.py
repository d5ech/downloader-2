"""
worker.py — RQ worker process for the Facebook Ad Library Downloader.

Usage
-----
Direct:
    python workers/worker.py

Docker (see docker-compose.yml):
    command: python workers/worker.py

Environment variables
---------------------
RQ_QUEUES        Comma-separated queue names in priority order.
                 Default: "high,default,low"
RQ_WORKER_NAME   Explicit worker name; auto-generated if unset.
RQ_BURST         Set to "1" / "true" to exit once all queues are empty.
RQ_REDIS_RETRIES Max Redis connection attempts on startup.  Default: 5.
REDIS_URL        Redis DSN.  Default: "redis://localhost:6379/0"

Retry strategy
--------------
Jobs are enqueued with a Retry(max=3, interval=[30,60,120]) object by
api.py.  The on_failure_callback in tasks.py prevents retries for
NonRetryableErrors (bad URLs, missing ads, etc.).

Queue priority
--------------
The worker drains queues in declaration order.  Enqueue to "high" for
jobs that need immediate processing (e.g. single-ad fetches), "default"
for normal requests, and "low" for bulk/background work.
"""

from __future__ import annotations

import os
import sys
import signal
import time
import logging
import socket

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.utils import get_logger, REDIS_URL, get_redis_connection
from rq import Queue, Worker

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_QUEUES = "high,default,low"

QUEUE_NAMES:   list[str] = [
    q.strip() for q in os.environ.get("RQ_QUEUES", _DEFAULT_QUEUES).split(",") if q.strip()
]
WORKER_NAME:   str | None = os.environ.get("RQ_WORKER_NAME") or None
BURST_MODE:    bool       = os.environ.get("RQ_BURST", "false").lower() in {"1", "true", "yes"}
MAX_CONN_RETRIES: int     = int(os.environ.get("RQ_REDIS_RETRIES", "5"))

# ---------------------------------------------------------------------------
# Redis connection with startup retry
# ---------------------------------------------------------------------------


def _connect_redis(max_retries: int = MAX_CONN_RETRIES):
    """
    Return a live Redis connection, retrying up to *max_retries* times.

    Waits use exponential back-off (2 s, 4 s, 8 s …) so the worker
    tolerates a Redis container that starts slightly after this process.

    Raises:
        RuntimeError: If Redis is still unreachable after all retries.
    """
    delay = 2
    for attempt in range(1, max_retries + 1):
        try:
            conn = get_redis_connection()
            conn.ping()
            logger.info("Redis connected (attempt %d/%d) — %s", attempt, max_retries, REDIS_URL)
            return conn
        except Exception as exc:
            if attempt == max_retries:
                raise RuntimeError(
                    f"Cannot connect to Redis after {max_retries} attempts: {exc}"
                ) from exc
            logger.warning(
                "Redis not ready (attempt %d/%d): %s — retrying in %ds",
                attempt, max_retries, exc, delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, 30)   # cap at 30 s


# ---------------------------------------------------------------------------
# Custom exception handler
# ---------------------------------------------------------------------------


def _exception_handler(job, exc_type, exc_value, traceback) -> bool:
    """
    RQ exception handler called for every job failure.

    Logs the failure with full context.  Returns ``True`` to let RQ continue
    with its own default handling (move to failed queue, schedule retry).
    Returning ``False`` would suppress default handling entirely.
    """
    logger.error(
        "Job %s failed [%s]: %s",
        job.id if job else "unknown",
        exc_type.__name__ if exc_type else "UnknownError",
        exc_value,
        exc_info=(exc_type, exc_value, traceback),
    )
    return True   # continue with RQ default handling


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------

_worker_ref: list = []   # mutable container so the signal handler can close over it


def _handle_sigterm(signum, frame) -> None:
    """
    SIGTERM → warm shutdown: finish the current job then exit.

    RQ's Worker.request_stop() sets an internal flag that prevents
    the worker from picking up new jobs after the current one finishes.
    """
    logger.info("SIGTERM received — requesting warm shutdown")
    if _worker_ref:
        try:
            _worker_ref[0].request_stop(signum, frame)
        except Exception as exc:
            logger.warning("Could not propagate SIGTERM to worker: %s", exc)


def _handle_sigint(signum, frame) -> None:
    """
    SIGINT (Ctrl+C) → immediate shutdown.

    Raises SystemExit so any ``finally`` blocks still run.
    """
    logger.info("SIGINT received — shutting down immediately")
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_sigterm)
signal.signal(signal.SIGINT,  _handle_sigint)


# ---------------------------------------------------------------------------
# Worker factory
# ---------------------------------------------------------------------------


def _build_worker(queues, conn):
    """
    Instantiate an RQ Worker with the project's custom exception handler.

    The worker name includes the hostname and PID so multiple workers can
    run on the same host without name collisions.
    """
    name = WORKER_NAME or f"worker.{socket.gethostname()}.{os.getpid()}"

    worker = Worker(
        queues=queues,
        name=name,
        connection=conn,
        exception_handlers=[_exception_handler],
    )
    logger.info(
        "Worker %r created | queues=%s pid=%d",
        name, [q.name for q in queues], os.getpid(),
    )
    return worker


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    """
    Entry point: connect to Redis, create queues, start the worker loop.

    The worker drains queues in the order declared by ``QUEUE_NAMES`` (high
    → default → low) so urgent jobs are always processed first.
    """
    conn   = _connect_redis()
    queues = [Queue(name, connection=conn) for name in QUEUE_NAMES]

    logger.info(
        "Starting worker | queues=%s burst=%s redis=%s",
        QUEUE_NAMES, BURST_MODE, REDIS_URL,
    )

    worker = _build_worker(queues, conn)
    _worker_ref.append(worker)

    try:
        worker.work(burst=BURST_MODE, with_scheduler=True)
    except SystemExit:
        logger.info("Worker exiting cleanly")
    except Exception as exc:
        logger.critical("Worker crashed: %s", exc, exc_info=True)
        sys.exit(1)
    finally:
        logger.info("Worker process stopped")


if __name__ == "__main__":
    main()
