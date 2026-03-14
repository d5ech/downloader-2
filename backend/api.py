"""
api.py — FastAPI server for the Facebook Ad Library Creative Downloader.

Endpoints
---------
POST   /download            Enqueue a scrape + download job
GET    /status/{job_id}     Poll job status
GET    /result/{job_id}     Retrieve downloaded filenames when complete

GET    /health              Liveness probe
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, field_validator

try:
    from backend.utils import get_redis_connection, get_logger, AdUrlError, parse_ad_url
except ImportError:
    from utils import get_redis_connection, get_logger, AdUrlError, parse_ad_url  # type: ignore[no-redef]

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Facebook Ad Library Downloader",
    description="Submit Ad Library URLs and download creative assets.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # tighten in production via env config
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
            # Normalise to the canonical URL so the worker always receives https://
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
        # rq raises NoSuchJobError; older versions raise a plain Exception
        if "NoSuchJob" in type(exc).__name__ or "does not exist" in str(exc).lower():
            raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found.")


def _files_from_result(job_result: dict | None) -> list[str]:
    """
    Extract the list of successfully downloaded filenames from a job result dict.

    Only files whose ``success`` flag is truthy are included.  Returns an
    empty list when *job_result* is ``None`` or contains no assets.
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
    # Prevent path traversal: reject filenames containing separators or leading dot
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
async def post_download(body: DownloadRequest) -> DownloadResponse:
    """
    Accept a Facebook Ad Library URL, enqueue a background job, and
    return a ``job_id`` that the client can poll.

    The URL is validated and normalised to its canonical form before
    being forwarded to the worker.

    Returns **202 Accepted** immediately; the actual work is performed
    asynchronously by an RQ worker process.
    """
    from rq.job import Retry  # noqa: PLC0415
    from workers.tasks import (  # noqa: PLC0415
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
        # Retry transient failures up to MAX_RETRIES times.
        # on_failure_callback will zero retries_left for NonRetryableErrors.
        retry=Retry(max=MAX_RETRIES, interval=RETRY_INTERVALS),
        on_failure=on_failure_callback,
        on_success=on_success_callback,
    )

    logger.info("Enqueued job %s for %s (retries=%d)", job.id, body.url, MAX_RETRIES)
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
    * ``queued``   — waiting in the queue (includes RQ's *deferred* state)
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
        raise HTTPException(status_code=500, detail=error_detail)

    # api_status == "complete"
    files = _files_from_result(job.result)
    return ResultResponse(job_id=job_id, status=api_status, files=files)
