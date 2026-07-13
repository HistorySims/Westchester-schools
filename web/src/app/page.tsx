"use client";

import { useState, useCallback, useEffect } from "react";
import { ChatPane, type ScopeInfo } from "@/components/ChatPane";
import { PageViewer } from "@/components/PageViewer";
import type { Citation } from "@/lib/types";

export default function Home() {
  const [viewerImageUrl, setViewerImageUrl] = useState<string | null>(null);
  const [viewerResourceUrl, setViewerResourceUrl] = useState<string | null>(null);
  const [activeCitationIndex, setActiveCitationIndex] = useState<number | null>(
    null
  );
  const [viewerMeta, setViewerMeta] = useState<{
    paper: string;
    date: string;
    page: number;
  } | null>(null);
  const [showViewer, setShowViewer] = useState(false);
  const [scope, setScope] = useState<ScopeInfo | null>(null);

  const handleCitationClick = useCallback((citation: Citation) => {
    setViewerImageUrl(citation.image_url);
    setViewerResourceUrl(citation.resource_url);
    setActiveCitationIndex(citation.index);
    setViewerMeta({
      paper: citation.paper_title,
      date: citation.date_issued,
      page: citation.page_sequence,
    });
    setShowViewer(true);
  }, []);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const params = new URLSearchParams(window.location.search);
    const tierStr = params.get("scope_tier");
    const labelStr = params.get("scope_label");
    if (tierStr === null || labelStr === null) return;
    const tier = parseInt(tierStr, 10);
    const label = parseInt(labelStr, 10);
    if (isNaN(tier) || isNaN(label)) return;

    fetch(`/api/explore/clusters?tier=${tier}`)
      .then((r) => r.json())
      .then((data: { label: number; size: number; label_text: string | null }[]) => {
        const match = data.find((c) => c.label === label);
        if (match) {
          setScope({
            tier,
            label,
            labelText: match.label_text,
            size: match.size,
          });
        }
      })
      .catch(() => {});
  }, []);

  const handleClearScope = useCallback(() => {
    setScope(null);
    if (typeof window !== "undefined") {
      window.history.replaceState({}, "", window.location.pathname);
    }
  }, []);

  return (
    <div className="h-full flex flex-col md:flex-row">
      <div
        className={`${
          showViewer
            ? "hidden md:flex md:h-full md:w-1/2 lg:w-[45%]"
            : "h-full w-full"
        } flex-shrink-0 border-r border-stone-200 flex flex-col`}
      >
        <ChatPane
          onCitationClick={handleCitationClick}
          activeCitationIndex={activeCitationIndex}
          scope={scope}
          onClearScope={handleClearScope}
        />
      </div>

      {showViewer && (
        <div className="flex-1 flex flex-col bg-[#1a1a1a] min-h-0 h-full">
          {viewerMeta && (
            <div className="px-3 py-2 bg-stone-900 border-b border-stone-700 flex items-center justify-between flex-shrink-0">
              <div className="text-xs text-stone-400 font-mono truncate mr-2">
                {viewerMeta.paper} &middot; {viewerMeta.date} &middot; p.{viewerMeta.page}
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <button
                  onClick={() => {
                    setShowViewer(false);
                    setActiveCitationIndex(null);
                  }}
                  className="md:hidden text-stone-400 hover:text-stone-200 text-xs
                    border border-stone-600 rounded px-2 py-1"
                >
                  Back to chat
                </button>
                <button
                  onClick={() => {
                    setShowViewer(false);
                    setActiveCitationIndex(null);
                  }}
                  className="hidden md:block text-stone-500 hover:text-stone-300 text-sm"
                  title="Close viewer"
                >
                  Close
                </button>
              </div>
            </div>
          )}
          <div className="flex-1 min-h-0">
            <PageViewer
              imageUrl={viewerImageUrl}
              resourceUrl={viewerResourceUrl}
            />
          </div>
        </div>
      )}
    </div>
  );
}
