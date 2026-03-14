"use client";

import { useState } from "react";
import SubmitForm from "@/components/SubmitForm";
import JobStatus from "@/components/JobStatus";
import AssetGrid from "@/components/AssetGrid";

export default function Home() {
  const [jobId, setJobId] = useState<string | null>(null);

  return (
    <main className="min-h-screen bg-gray-50">
      {/* Header */}
      <header className="bg-blue-600 text-white shadow">
        <div className="max-w-4xl mx-auto px-4 py-6">
          <h1 className="text-2xl font-bold">Facebook Ad Library Downloader</h1>
          <p className="text-blue-100 text-sm mt-1">
            Submit an Ad Library URL to extract and download creative assets.
          </p>
        </div>
      </header>

      {/* Body */}
      <div className="max-w-4xl mx-auto px-4 py-8 space-y-8">
        {/* URL submission */}
        <SubmitForm onJobCreated={(id) => setJobId(id)} />

        {/* Live status + asset list */}
        {jobId && (
          <>
            <JobStatus jobId={jobId} />
            <AssetGrid jobId={jobId} />
          </>
        )}
      </div>
    </main>
  );
}
