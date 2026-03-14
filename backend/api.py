"""
api.py — FastAPI server for the Facebook Ad Library Creative Downloader.

Endpoints
---------
POST   /download            Enqueue a scrape + download job  (5 req/min per IP)
GET    /status/{job_id}     Poll job status
GET    /result/{job_id}     Retrieve downloaded filenames when complete
GET    /files/{job_id}/{filename}  Serve a downloaded file
GET    /health              Liveness probe

Production features
-------------------
* Rate limiting  — 5 POST /download requests per minute per IP (Redis-backed).
* Access logging — every request logged with method, path, status, latency, IP.
* Job cleanup    — background task deletes /tmp/jobs/* dirs older than 30 min.
* Structured errors — all error responses share a single JSON schema.
"""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, field_validator
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

try:
    from backend.utils import get_redis_connection, get_logger, AdUrlError, parse_ad_url, REDIS_URL
except ImportError:
    from utils import get_redis_connection, get_logger, AdUrlError, parse_ad_url, REDIS_URL  # type: ignore[no-redef]

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter — Redis-backed so limits are shared across API replicas
# ---------------------------------------------------------------------------

limiter = Limiter(key_func=get_remote_address, storage_uri=REDIS_URL)

# ---------------------------------------------------------------------------
# Temporary job storage cleanup
# ---------------------------------------------------------------------------

JOBS_ROOT = Path("/tmp/jobs")
JOB_TTL_SECONDS = 30 * 60   # 30 minutes
_CLEANUP_INTERVAL = 5 * 60  # run every 5 minutes


def _cleanup_expired_jobs() -> None:
    """Remove job directories whose last-modified time exceeds JOB_TTL_SECONDS."""
    if not JOBS_ROOT.exists():
        return
    cutoff = time.time() - JOB_TTL_SECONDS
    removed = 0
    for job_dir in JOBS_ROOT.iterdir():
        if not job_dir.is_dir():
            continue
        try:
            if job_dir.stat().st_mtime < cutoff:
                shutil.rmtree(job_dir)
                logger.info("Removed expired job dir: %s", job_dir.name)
                removed += 1
        except Exception as exc:
            logger.warning("Could not remove job dir %s: %s", job_dir.name, exc)
    if removed:
        logger.info("Cleanup pass complete — removed %d expired job dir(s)", removed)


async def _cleanup_loop() -> None:
    """Periodic background coroutine: sleep then clean."""
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            _cleanup_expired_jobs()
        except Exception as exc:
            logger.error("Cleanup loop error: %s", exc)


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_cleanup_loop())
    logger.info(
        "Job cleanup task started (ttl=%ds, interval=%ds)",
        JOB_TTL_SECONDS, _CLEANUP_INTERVAL,
    )
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Facebook Ad Library Downloader",
    description="Submit Ad Library URLs and download creative assets.",
    version="1.0.0",
    lifespan=lifespan,
)

app.state.limiter = limiter

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production via env config
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Request / access logging middleware
# ---------------------------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    ms = (time.time() - start) * 1000
    ip = request.client.host if request.client else "unknown"
    logger.info(
        "%s %s %d %.1fms ip=%s",
        request.method, request.url.path, response.status_code, ms, ip,
    )
    return response


# ---------------------------------------------------------------------------
# Structured error schema + helpers
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Uniform error envelope returned by all error responses."""
    error: str              # machine-readable slug
    detail: str | None = None
    job_id: str | None = None


def _json_error(
    error: str,
    detail: str | None = None,
    job_id: str | None = None,
    status: int = 400,
) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content=ErrorResponse(error=error, detail=detail, job_id=job_id).model_dump(),
    )


# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------


@app.exception_handler(RateLimitExceeded)
async def rate_limit_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    ip = request.client.host if request.client else "unknown"
    logger.warning("Rate limit hit: ip=%s path=%s", ip, request.url.path)
    retry_after = getattr(exc, "retry_after", None)
    detail = (
        f"5 requests per minute per IP. Retry-After: {retry_after}s"
        if retry_after
        else "5 requests per minute per IP."
    )
    return _json_error("rate_limit_exceeded", detail, status=429)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    msgs = "; ".join(
        f"{'→'.join(str(loc) for loc in e['loc'])}: {e['msg']}"
        for e in exc.errors()
    )
    logger.warning("Validation error: %s %s — %s", request.method, request.url.path, msgs)
    return _json_error("validation_error", msgs, status=422)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    _slugs = {
        400: "bad_request",
        202: "job_pending",
        404: "not_found",
        429: "rate_limit_exceeded",
        500: "internal_error",
    }
    slug = _slugs.get(exc.status_code, "http_error")
    return _json_error(slug, str(exc.detail) if exc.detail else None, status=exc.status_code)


@app.exception_handler(Exception)
async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "Unhandled exception: %s %s — %s",
        request.method, request.url.path, exc, exc_info=True,
    )
    return _json_error("internal_error", "An unexpected error occurred.", status=500)


# ---------------------------------------------------------------------------
# RQ status → API status mapping
#
#   RQ statuses  : queued | started | finished | failed | deferred | stopped
#   API statuses : queued | running | complete | error
# ---------------------------------------------------------------------------

_RQ_TO_API_STATUS: dict[str, str] = {
    "queued":   "queued",
    "deferred": "queued",   # waiting on a dependency
    "started":  "running",
    "finished": "complete",
    "failed":   "error",
    "stopped":  "error",
    "canceled": "error",
}


def _map_status(rq_status: str) -> str:
    """Translate an RQ job-status string to the public API vocabulary."""
    return _RQ_TO_API_STATUS.get(rq_status, "error")


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


class DownloadRequest(BaseModel):
    """Body accepted by POST /download."""

    url: str

    @field_validator("url")
    @classmethod
    def validate_ad_url(cls, v: str) -> str:
        """Reject anything that is not a valid Facebook Ad Library URL or bare ID."""
        try:
            parsed = parse_ad_url(v)
            return parsed["url"]
        except AdUrlError as exc:
            raise ValueError(str(exc)) from exc


class DownloadResponse(BaseModel):
    """Response body for POST /download."""

    job_id: str
    status: str   # always "queued" on success


class StatusResponse(BaseModel):
    """Response body for GET /status/{job_id}."""

    job_id: str
    status: str   # queued | running | complete | error


class ResultResponse(BaseModel):
    """Response body for GET /result/{job_id}."""

    job_id: str
    status: str
    files: list[str]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _get_queue():
    """Return the default RQ Queue backed by Redis."""
    from rq import Queue
    return Queue(connection=get_redis_connection())


def _fetch_job(job_id: str):
    """
    Fetch an RQ Job by ID.

    Raises:
        HTTPException(404): if the job does not exist in Redis.
    """
    from rq.job import Job
    from rq.exceptions import NoSuchJobError

    try:
        return Job.fetch(job_id, connection=get_redis_connection())
    except (NoSuchJobError, Exception) as exc:
        if "NoSuchJob" in type(exc).__name__ or "does not exist" in str(exc).lower():
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")


def _files_from_result(job_result: dict | None) -> list[str]:
    """
    Extract the list of successfully downloaded filenames from a job result dict.

    Only files whose ``success`` flag is truthy are included.
    """
    if not job_result or not isinstance(job_result, dict):
        return []
    return [
        asset["filename"]
        for asset in job_result.get("assets", [])
        if asset.get("success") and asset.get("filename")
    ]


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get(
    "/files/{job_id}/{filename}",
    tags=["jobs"],
    summary="Download a file produced by a completed job",
)
async def get_file(job_id: str, filename: str) -> FileResponse:
    """
    Serve a single file from the job output directory.

    Files are stored at ``/tmp/jobs/{job_id}/{filename}`` by the worker.
    Raises **404** if the job directory or file does not exist.
    """
    if "/" in filename or "\\" in filename or filename.startswith("."):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = Path(f"/tmp/jobs/{job_id}/{filename}")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found.")

    return FileResponse(path, filename=filename)


@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    """Liveness probe — returns 200 when the API process is alive."""
    return {"status": "ok"}


@app.post(
    "/download",
    response_model=DownloadResponse,
    status_code=202,
    tags=["jobs"],
    summary="Enqueue a scrape + download job",
)
@limiter.limit("5/minute")
async def post_download(request: Request, body: DownloadRequest) -> DownloadResponse:
    """
    Accept a Facebook Ad Library URL, enqueue a background job, and
    return a ``job_id`` that the client can poll.

    Rate-limited to **5 requests per minute per IP**.

    Returns **202 Accepted** immediately; the actual work is performed
    asynchronously by an RQ worker process.
    """
    from rq.job import Retry
    from workers.tasks import (
        on_failure_callback,
        on_success_callback,
        MAX_RETRIES,
        RETRY_INTERVALS,
    )

    job_id = str(uuid.uuid4())

    q   = _get_queue()
    job = q.enqueue(
        "workers.tasks.run_ad_download",
        kwargs={"url": body.url, "job_id": job_id},
        job_id=job_id,
        job_timeout="10m",
        result_ttl=3_600,       # keep result in Redis for 1 hour
        failure_ttl=86_400,     # keep failure info for 24 hours
        retry=Retry(max=MAX_RETRIES, interval=RETRY_INTERVALS),
        on_failure=on_failure_callback,
        on_success=on_success_callback,
    )

    ip = request.client.host if request.client else "unknown"
    logger.info("Enqueued job %s url=%s ip=%s retries=%d", job.id, body.url, ip, MAX_RETRIES)
    return DownloadResponse(job_id=job.id, status="queued")


@app.get(
    "/status/{job_id}",
    response_model=StatusResponse,
    tags=["jobs"],
    summary="Poll job status",
)
async def get_status(job_id: str) -> StatusResponse:
    """
    Return the current status of a job.

    Status values
    -------------
    * ``queued``   — waiting in the queue
    * ``running``  — a worker is actively processing the job
    * ``complete`` — finished successfully; results are available
    * ``error``    — failed, stopped, or cancelled

    Raises **404** if *job_id* is unknown.
    """
    job        = _fetch_job(job_id)
    rq_status  = job.get_status()
    api_status = _map_status(rq_status.value if hasattr(rq_status, "value") else str(rq_status))

    return StatusResponse(job_id=job_id, status=api_status)


@app.get(
    "/result/{job_id}",
    response_model=ResultResponse,
    tags=["jobs"],
    summary="Retrieve downloaded filenames",
)
async def get_result(job_id: str) -> ResultResponse:
    """
    Return the list of filenames produced by a completed job.

    Raises
    ------
    404 : Job not found.
    202 : Job is still queued or running (retry later).
    500 : Job finished in an error state.
    """
    job        = _fetch_job(job_id)
    rq_status  = job.get_status()
    status_str = rq_status.value if hasattr(rq_status, "value") else str(rq_status)
    api_status = _map_status(status_str)

    if api_status in ("queued", "running"):
        raise HTTPException(
            status_code=202,
            detail=f"Job is {api_status}. Retry after a moment.",
        )

    if api_status == "error":
        error_detail = str(job.exc_info).strip() if job.exc_info else "Job failed."
        logger.error("Job %s result requested in error state: %s", job_id, error_detail)
        raise HTTPException(status_code=500, detail=error_detail)

    files = _files_from_result(job.result)
    logger.info("Job %s result served — %d file(s)", job_id, len(files))
    return ResultResponse(job_id=job_id, status=api_status, files=files)
