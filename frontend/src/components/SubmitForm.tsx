"use client";

import { useState } from "react";
import { submitJob } from "@/lib/api";
import { Loader2, Download } from "lucide-react";

interface Props {
  onJobCreated: (jobId: string) => void;
}

export default function SubmitForm({ onJobCreated }: Props) {
  const [url, setUrl] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const job = await submitJob(url.trim());
      onJobCreated(job.job_id);
      setUrl("");
    } catch (err: unknown) {
      // The API returns { error, detail } for all error responses.
      // Prefer the human-readable `detail` string; fall back to the `error`
      // slug (reformatted) so the user always sees something meaningful.
      const data = (err as { response?: { data?: Record<string, unknown> } })
        ?.response?.data;
      const detail =
        typeof data?.detail === "string" && data.detail
          ? data.detail
          : typeof data?.error === "string" && data.error
          ? data.error.replace(/_/g, " ")
          : null;
      setError(detail ?? "Failed to submit. Check the URL and try again.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="bg-white rounded-xl shadow p-6">
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label
            htmlFor="ad-url"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Ad Library URL or ID
          </label>
          <input
            id="ad-url"
            type="text"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.facebook.com/ads/library/?id=… or 1234567890"
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {error && <p className="text-sm text-red-600">{error}</p>}

        <button
          type="submit"
          disabled={loading}
          className="flex items-center gap-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-60 text-white font-medium px-5 py-2 rounded-lg text-sm transition-colors"
        >
          {loading ? (
            <Loader2 className="w-4 h-4 animate-spin" />
          ) : (
            <Download className="w-4 h-4" />
          )}
          {loading ? "Submitting…" : "Download"}
        </button>
      </form>
    </section>
  );
}
