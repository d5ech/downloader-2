"use client";

import { useEffect, useRef, useState } from "react";
import { getJobStatus, getJobResult, JobStatus as Status } from "@/lib/api";
import { Loader2, CheckCircle, XCircle, Clock } from "lucide-react";

interface Props {
  jobId: string;
  /** Called whenever the status changes. On "complete", files are also passed. */
  onStatusChange: (status: Status, files?: string[]) => void;
}

const POLL_MS = 3_000;

const STATUS_CFG: Record<
  Status,
  { label: string; color: string; icon: React.ReactNode }
> = {
  queued: {
    label: "Queued",
    color: "text-yellow-600 bg-yellow-50 border-yellow-200",
    icon: <Clock className="w-4 h-4" />,
  },
  running: {
    label: "Processing…",
    color: "text-blue-600 bg-blue-50 border-blue-200",
    icon: <Loader2 className="w-4 h-4 animate-spin" />,
  },
  complete: {
    label: "Complete",
    color: "text-green-600 bg-green-50 border-green-200",
    icon: <CheckCircle className="w-4 h-4" />,
  },
  error: {
    label: "Failed",
    color: "text-red-600 bg-red-50 border-red-200",
    icon: <XCircle className="w-4 h-4" />,
  },
};

export default function JobStatus({ jobId, onStatusChange }: Props) {
  const [status, setStatus] = useState<Status>("queued");
  // Keep a ref so the polling closure always sees the latest callback
  const callbackRef = useRef(onStatusChange);
  callbackRef.current = onStatusChange;

  useEffect(() => {
    let active = true;

    async function poll() {
      try {
        const data = await getJobStatus(jobId);
        if (!active) return;

        setStatus(data.status);

        if (data.status === "complete") {
          // Fetch the file list and forward it to the parent
          try {
            const result = await getJobResult(jobId);
            if (active) callbackRef.current("complete", result.files);
          } catch {
            if (active) callbackRef.current("complete", []);
          }
          return; // stop polling
        }

        callbackRef.current(data.status);

        if (data.status !== "error") {
          setTimeout(poll, POLL_MS);
        }
      } catch {
        // Network hiccup — back off and retry
        if (active) setTimeout(poll, POLL_MS * 2);
      }
    }

    poll();
    return () => {
      active = false;
    };
  }, [jobId]);

  const cfg = STATUS_CFG[status];

  return (
    <section className="bg-white rounded-xl shadow p-4">
      <div className="flex items-center gap-3">
        <span
          className={`inline-flex items-center gap-2 text-sm font-medium border rounded-full px-3 py-1 ${cfg.color}`}
        >
          {cfg.icon}
          {cfg.label}
        </span>
        <span className="text-xs font-mono text-gray-400 truncate">{jobId}</span>
      </div>
    </section>
  );
}
