"use client";

import type { Citation } from "@/lib/types";

interface CitationLinkProps {
  citation: Citation;
  isActive: boolean;
  onClick: (citation: Citation) => void;
}

export function CitationLink({ citation, isActive, onClick }: CitationLinkProps) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation();
        onClick(citation);
      }}
      className={`inline-flex items-center justify-center min-w-[1.75em] h-[1.75em] px-1
        text-xs font-mono rounded align-super cursor-pointer transition-colors
        touch-manipulation
        ${isActive
          ? "bg-amber-700 text-amber-50"
          : "bg-amber-100 text-amber-800 hover:bg-amber-200 active:bg-amber-300"
        }`}
      title={`${citation.paper_title}, ${citation.date_issued}, p.${citation.page_sequence}`}
      aria-label={`View source: ${citation.paper_title}, ${citation.date_issued}, page ${citation.page_sequence}`}
    >
      {citation.index}
    </button>
  );
}
