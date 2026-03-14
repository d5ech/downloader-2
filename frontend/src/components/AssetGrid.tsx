"use client";

import { useEffect, useState } from "react";
import { listAssets, assetDownloadUrl, AssetMeta } from "@/lib/api";
import { Download, Image as ImageIcon, Video } from "lucide-react";

interface Props {
  jobId: string;
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function AssetCard({ asset, jobId }: { asset: AssetMeta; jobId: string }) {
  const isVideo = asset.asset_type === "video";
  const downloadUrl = assetDownloadUrl(jobId, asset.filename);

  return (
    <div className="bg-white border border-gray-200 rounded-lg overflow-hidden shadow-sm hover:shadow-md transition-shadow">
      {/* Preview placeholder */}
      <div className="h-36 bg-gray-100 flex items-center justify-center">
        {isVideo ? (
          <Video className="w-10 h-10 text-gray-300" />
        ) : (
          <ImageIcon className="w-10 h-10 text-gray-300" />
        )}
      </div>

      {/* Meta */}
      <div className="p-3">
        <p className="text-xs font-mono text-gray-500 truncate">{asset.filename}</p>
        <p className="text-xs text-gray-400 mt-0.5">
          {asset.asset_type} · {formatBytes(asset.size_bytes)}
        </p>

        {/* Download button */}
        <a
          href={downloadUrl}
          download={asset.filename}
          className="mt-2 flex items-center justify-center gap-1 w-full text-xs bg-blue-600 hover:bg-blue-700 text-white rounded px-2 py-1.5 transition-colors"
        >
          <Download className="w-3.5 h-3.5" />
          Download
        </a>
      </div>
    </div>
  );
}

export default function AssetGrid({ jobId }: Props) {
  const [assets, setAssets] = useState<AssetMeta[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;

    async function fetchAssets() {
      try {
        const data = await listAssets(jobId);
        if (active) setAssets(data.assets);
      } catch (err: unknown) {
        // 202 means not ready yet — retry shortly
        const status = (err as { response?: { status?: number } })?.response?.status;
        if (status === 202) {
          setTimeout(fetchAssets, 3000);
        } else {
          if (active) setError("Could not load assets.");
        }
      }
    }

    fetchAssets();
    return () => { active = false; };
  }, [jobId]);

  if (error) {
    return <p className="text-red-600 text-sm">{error}</p>;
  }

  if (!assets) return null;

  if (assets.length === 0) {
    return (
      <section className="bg-white rounded-xl shadow p-6 text-sm text-gray-500">
        No assets were found for this job.
      </section>
    );
  }

  return (
    <section className="bg-white rounded-xl shadow p-6">
      <h2 className="text-lg font-semibold mb-4">
        Downloaded Assets ({assets.length})
      </h2>
      <div className="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 gap-4">
        {assets.map((asset) => (
          <AssetCard key={asset.filename} asset={asset} jobId={jobId} />
        ))}
      </div>
    </section>
  );
}
