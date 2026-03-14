"use client";

import { useState, useCallback } from "react";
import SubmitForm from "@/components/SubmitForm";
import JobStatus from "@/components/JobStatus";
import AssetGrid from "@/components/AssetGrid";
import { JobStatus as Status } from "@/lib/api";

export default function Home() {
  const [jobId, setJobId]   = useState<string | null>(null);
  const [status, setStatus] = useState<Status | null>(null);
  const [files,  setFiles]  = useState<string[]>([]);

  function handleJobCreated(id: string) {
    setJobId(id);
    setStatus("queued");
    setFiles([]);
  }

  const handleStatusChange = useCallback(
    (newStatus: Status, resultFiles?: string[]) => {
      setStatus(newStatus);
      if (resultFiles !== undefined) setFiles(resultFiles);
    },
    [],
  );

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-blue-600 text-white shadow">
        <div className="max-w-2xl mx-auto px-4 py-6">
          <h1 className="text-2xl font-bold tracking-tight">
            Facebook Ad Downloader
          </h1>
          <p className="text-blue-100 text-sm mt-1">
            Paste an Ad Library URL or bare ad ID to download its creative assets.
          </p>
        </div>
      </header>

      {/* Content */}
      <div className="max-w-2xl mx-auto px-4 py-8 space-y-5">
        {/* Step 1 — URL input */}
        <SubmitForm onJobCreated={handleJobCreated} />

        {/* Step 2 & 3 — progress indicator */}
        {jobId && (
          <JobStatus jobId={jobId} onStatusChange={handleStatusChange} />
        )}

        {/* Step 4 — download links (shown once complete) */}
        {status === "complete" && jobId && (
          <AssetGrid jobId={jobId} files={files} />
        )}
      </div>
    </main>
  );
}
