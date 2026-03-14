/**
 * api.ts — Typed wrapper around the FastAPI backend.
 *
 * Requests go to /api/* which next.config.js proxies to the FastAPI server.
 *
 * Backend endpoints used:
 *   POST /download          → { job_id, status: "queued" }
 *   GET  /status/{job_id}   → { job_id, status: "queued"|"running"|"complete"|"error" }
 *   GET  /result/{job_id}   → { job_id, status, files: string[] }
 *   GET  /files/{job_id}/{filename}  → file download
 */

import axios from "axios";

const client = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type JobStatus = "queued" | "running" | "complete" | "error";

export interface DownloadResponse {
  job_id: string;
  status: string;
}

export interface StatusResponse {
  job_id: string;
  status: JobStatus;
}

export interface ResultResponse {
  job_id: string;
  status: string;
  files: string[];
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

/** Enqueue a new download job for the given Ad Library URL or bare ad ID. */
export async function submitJob(url: string): Promise<DownloadResponse> {
  const { data } = await client.post<DownloadResponse>("/download", { url });
  return data;
}

/** Poll the current status of a job. */
export async function getJobStatus(jobId: string): Promise<StatusResponse> {
  const { data } = await client.get<StatusResponse>(`/status/${jobId}`);
  return data;
}

/**
 * Retrieve the list of downloaded filenames for a completed job.
 * Throws with status 202 if the job is still running.
 */
export async function getJobResult(jobId: string): Promise<ResultResponse> {
  const { data } = await client.get<ResultResponse>(`/result/${jobId}`);
  return data;
}

/** Direct download URL for a single file produced by a job. */
export function fileDownloadUrl(jobId: string, filename: string): string {
  return `/api/files/${jobId}/${encodeURIComponent(filename)}`;
}
