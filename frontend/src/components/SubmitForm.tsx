"use client";

import { useState } from "react";
import { submitJob } from "@/lib/api";
import { Loader2, Download } from "lucide-react";

interface Props {
  onJobCreated: (jobId: string) => void;
}

export default function SubmitForm({ onJobCreated }: Props) {
  const [url, setUrl] = useState("");
  const [maxAssets, setMaxAssets] = useState(20);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);

    try {
      const job = await submitJob(url.trim(), maxAssets);
      onJobCreated(job.job_id);
    } catch (err: unknown) {
      const message =
        err instanceof Error ? err.message : "Failed to submit job.";
      setError(message);
    } finally {
      setLoading(false);
    }
  }

  return (
    <section className="bg-white rounded-xl shadow p-6">
      <h2 className="text-lg font-semibold mb-4">Submit Ad Library URL</h2>
      <form onSubmit={handleSubmit} className="space-y-4">
        {/* URL input */}
        <div>
          <label
            htmlFor="ad-url"
            className="block text-sm font-medium text-gray-700 mb-1"
          >
            Facebook Ad Library URL
          </label>
          <input
            id="ad-url"
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://www.facebook.com/ads/library/?..."
            className="w-full border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {/* Max assets */}
        <div className="flex items-center gap-3">
          <label
            htmlFor="max-assets"
            className="text-sm font-medium text-gray-700 whitespace-nowrap"
          >
            Max assets
          </label>
          <input
            id="max-assets"
            type="number"
            min={1}
            max={100}
            value={maxAssets}
            onChange={(e) => setMaxAssets(Number(e.target.value))}
            className="w-20 border border-gray-300 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
        </div>

        {/* Error */}
        {error && (
          <p className="text-red-600 text-sm">{error}</p>
        )}

        {/* Submit */}
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
          {loading ? "Submitting…" : "Download Assets"}
        </button>
      </form>
    </section>
  );
}
