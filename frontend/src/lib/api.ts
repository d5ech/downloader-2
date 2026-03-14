/**
 * api.ts — Thin wrapper around the FastAPI backend.
 *
 * All requests are sent to /api/* which Next.js rewrites to the FastAPI server
 * (see next.config.js).
 */

import axios from "axios";

const client = axios.create({
  baseURL: "/api",
  headers: { "Content-Type": "application/json" },
});

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface JobResponse {
  job_id: string;
  status: string;
  message: string;
}

export interface JobStatusResponse {
  job_id: string;
  status: "queued" | "started" | "finished" | "failed";
  result: unknown | null;
  error: string | null;
}

export interface AssetMeta {
  ad_id: string;
  filename: string;
  asset_type: string;
  size_bytes: number;
  url: string;
}

export interface AssetsResponse {
  job_id: string;
  count: number;
  assets: AssetMeta[];
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

/** Submit a new Ad Library URL for processing. */
export async function submitJob(
  url: string,
  maxAssets = 20
): Promise<JobResponse> {
  const { data } = await client.post<JobResponse>("/jobs", {
    url,
    max_assets: maxAssets,
  });
  return data;
}

/** Poll the status of an existing job. */
export async function getJobStatus(jobId: string): Promise<JobStatusResponse> {
  const { data } = await client.get<JobStatusResponse>(`/jobs/${jobId}`);
  return data;
}

/** Retrieve asset metadata for a finished job. */
export async function listAssets(jobId: string): Promise<AssetsResponse> {
  const { data } = await client.get<AssetsResponse>(`/jobs/${jobId}/assets`);
  return data;
}

/** Build the direct download URL for a single asset. */
export function assetDownloadUrl(jobId: string, filename: string): string {
  return `/api/jobs/${jobId}/assets/${encodeURIComponent(filename)}`;
}
