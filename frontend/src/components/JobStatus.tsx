"use client";

import { useEffect, useState } from "react";
import { getJobStatus, JobStatusResponse } from "@/lib/api";
import { Loader2, CheckCircle, XCircle, Clock } from "lucide-react";

interface Props {
  jobId: string;
}

const POLL_INTERVAL_MS = 3000;

const statusConfig: Record<
  string,
  { label: string; color: string; icon: React.ReactNode }
> = {
  queued: {
    label: "Queued",
    color: "text-yellow-600 bg-yellow-50 border-yellow-200",
    icon: <Clock className="w-4 h-4" />,
  },
  started: {
    label: "In Progress",
    color: "text-blue-600 bg-blue-50 border-blue-200",
    icon: <Loader2 className="w-4 h-4 animate-spin" />,
  },
  finished: {
    label: "Finished",
    color: "text-green-600 bg-green-50 border-green-200",
    icon: <CheckCircle className="w-4 h-4" />,
  },
  failed: {
    label: "Failed",
    color: "text-red-600 bg-red-50 border-red-200",
    icon: <XCircle className="w-4 h-4" />,
  },
};

export default function JobStatus({ jobId }: Props) {
  const [status, setStatus] = useState<JobStatusResponse | null>(null);

  useEffect(() => {
    let active = true;

    async function poll() {
      try {
        const data = await getJobStatus(jobId);
        if (!active) return;
        setStatus(data);
        if (data.status !== "finished" && data.status !== "failed") {
          setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch {
        if (active) setTimeout(poll, POLL_INTERVAL_MS * 2);
      }
    }

    poll();
    return () => { active = false; };
  }, [jobId]);

  if (!status) {
    return (
      <div className="flex items-center gap-2 text-sm text-gray-500">
        <Loader2 className="w-4 h-4 animate-spin" />
        Fetching job status…
      </div>
    );
  }

  const cfg = statusConfig[status.status] ?? statusConfig.queued;

  return (
    <section className="bg-white rounded-xl shadow p-6">
      <h2 className="text-lg font-semibold mb-3">Job Status</h2>

      <div
        className={`inline-flex items-center gap-2 text-sm font-medium border rounded-full px-3 py-1 ${cfg.color}`}
      >
        {cfg.icon}
        {cfg.label}
      </div>

      <dl className="mt-4 text-sm text-gray-600 space-y-1">
        <div className="flex gap-2">
          <dt className="font-medium w-20">Job ID</dt>
          <dd className="font-mono text-xs text-gray-500 break-all">{status.job_id}</dd>
        </div>
      </dl>

      {status.error && (
        <p className="mt-3 text-red-600 text-sm">{status.error}</p>
      )}
    </section>
  );
}
