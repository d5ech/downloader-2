"use client";

import { Download, Film, Image as ImageIcon, FileIcon } from "lucide-react";
import { fileDownloadUrl } from "@/lib/api";

interface Props {
  jobId: string;
  files: string[];
}

function FileTypeIcon({ filename }: { filename: string }) {
  if (/\.(mp4|webm|mov|avi)$/i.test(filename))
    return <Film className="w-4 h-4 text-blue-500 shrink-0" />;
  if (/\.(jpe?g|png|webp|gif)$/i.test(filename))
    return <ImageIcon className="w-4 h-4 text-green-500 shrink-0" />;
  return <FileIcon className="w-4 h-4 text-gray-400 shrink-0" />;
}

export default function AssetGrid({ jobId, files }: Props) {
  if (files.length === 0) {
    return (
      <section className="bg-white rounded-xl shadow p-6 text-sm text-gray-500">
        No downloadable files were found for this job.
      </section>
    );
  }

  return (
    <section className="bg-white rounded-xl shadow p-6">
      <h2 className="text-base font-semibold mb-3">
        Downloads&nbsp;
        <span className="text-gray-400 font-normal">({files.length})</span>
      </h2>

      <ul className="space-y-2">
        {files.map((filename) => (
          <li
            key={filename}
            className="flex items-center justify-between gap-3 rounded-lg border border-gray-100 px-3 py-2 hover:bg-gray-50 transition-colors"
          >
            <div className="flex items-center gap-2 min-w-0">
              <FileTypeIcon filename={filename} />
              <span className="text-sm font-mono truncate text-gray-700">
                {filename}
              </span>
            </div>

            <a
              href={fileDownloadUrl(jobId, filename)}
              download={filename}
              className="flex items-center gap-1 shrink-0 text-xs bg-blue-600 hover:bg-blue-700 text-white rounded px-3 py-1.5 transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              Download
            </a>
          </li>
        ))}
      </ul>
    </section>
  );
}
