"""
FastAPI backend — Facebook Ad Library Creative Downloader
Exposes REST endpoints for job submission, status polling, and media retrieval.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, HttpUrl
import redis
from rq import Queue

from utils import get_redis_connection, get_logger
from downloader import download_media_assets
from scraper import scrape_ad_library_url

logger = get_logger(__name__)

app = FastAPI(
    title="Facebook Ad Library Downloader",
    description="Submit Ad Library URLs and download creative assets.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SubmitRequest(BaseModel):
    url: HttpUrl
    # Optional filters the user can pass
    ad_id: str | None = None
    max_assets: int = 20


class JobResponse(BaseModel):
    job_id: str
    status: str
    message: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str          # queued | started | finished | failed
    result: Any | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_queue() -> Queue:
    """Return the default RQ queue backed by Redis."""
    conn = get_redis_connection()
    return Queue(connection=conn)


def _get_job(job_id: str):
    """Fetch an RQ Job object; raises 404 if not found."""
    from rq.job import Job
    conn = get_redis_connection()
    try:
        return Job.fetch(job_id, connection=conn)
    except Exception:
        raise HTTPException(status_code=404, detail=f"Job {job_id!r} not found.")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health", tags=["meta"])
async def health_check() -> dict:
    """Liveness probe."""
    return {"status": "ok"}


@app.post("/jobs", response_model=JobResponse, tags=["jobs"])
async def submit_job(payload: SubmitRequest) -> JobResponse:
    """
    Accept a Facebook Ad Library URL and enqueue a scrape + download job.

    The heavy lifting is delegated to the RQ worker pool so this endpoint
    returns immediately with a job ID that the client can poll.
    """
    q = _get_queue()
    job = q.enqueue(
        "workers.tasks.run_ad_download",   # dotted path resolved by the worker
        kwargs={
            "url": str(payload.url),
            "ad_id": payload.ad_id,
            "max_assets": payload.max_assets,
        },
        job_id=str(uuid.uuid4()),
        job_timeout="10m",
        result_ttl=3600,
    )
    logger.info("Enqueued job %s for URL %s", job.id, payload.url)
    return JobResponse(job_id=job.id, status="queued", message="Job enqueued successfully.")


@app.get("/jobs/{job_id}", response_model=JobStatusResponse, tags=["jobs"])
async def get_job_status(job_id: str) -> JobStatusResponse:
    """Poll the status of an existing download job."""
    job = _get_job(job_id)
    return JobStatusResponse(
        job_id=job.id,
        status=job.get_status().value,
        result=job.result if job.is_finished else None,
        error=str(job.exc_info) if job.is_failed else None,
    )


@app.get("/jobs/{job_id}/assets", tags=["jobs"])
async def list_job_assets(job_id: str) -> dict:
    """
    Return metadata (filenames, types, sizes) for all assets downloaded
    as part of *job_id*.
    """
    job = _get_job(job_id)
    if not job.is_finished:
        raise HTTPException(status_code=202, detail="Job not finished yet.")

    assets: list[dict] = job.result.get("assets", []) if job.result else []
    return {"job_id": job_id, "count": len(assets), "assets": assets}


@app.get("/jobs/{job_id}/assets/{filename}", tags=["jobs"])
async def download_asset(job_id: str, filename: str) -> FileResponse:
    """
    Stream a single downloaded asset back to the caller.
    The file must already exist on the server from a completed job.
    """
    import os
    from pathlib import Path

    job = _get_job(job_id)
    if not job.is_finished:
        raise HTTPException(status_code=202, detail="Job not finished yet.")

    asset_dir = Path("media") / "downloads" / job_id
    file_path = (asset_dir / filename).resolve()

    # Guard against path traversal
    if not str(file_path).startswith(str(asset_dir.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename.")

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Asset not found.")

    return FileResponse(path=str(file_path), filename=filename)


@app.delete("/jobs/{job_id}", tags=["jobs"])
async def cancel_job(job_id: str) -> dict:
    """Cancel a queued or started job."""
    job = _get_job(job_id)
    job.cancel()
    logger.info("Cancelled job %s", job_id)
    return {"job_id": job_id, "status": "cancelled"}
