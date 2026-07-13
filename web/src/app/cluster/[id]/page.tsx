"use client";

// Cluster Dossier — the middle of the historian's funnel:
// brief → cluster → evidence → primary source.
//
// Top zone: anatomy panel (stream, comet trail, word river, scrubber).
// Bottom zone: evidence feed, every active chunk → LoC page image.
// Stars are session-local; export produces a markdown citation list.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { ClusterAnatomy } from "@/components/ClusterAnatomy";
import { EvidenceFeed } from "@/components/EvidenceFeed";
import type { DossierResponse } from "@/lib/dossier";

export default function ClusterDossierPage() {
  const params = useParams<{ id: string }>();
  const id = Array.isArray(params.id) ? params.id[0] : params.id;

  const [data, setData] = useState<DossierResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [weekIndex, setWeekIndex] = useState(0);
  const [starred, setStarred] = useState<Set<string>>(new Set());
  const [showExport, setShowExport] = useState(false);
  const [copied, setCopied] = useState(false);
  const [briefQuestion, setBriefQuestion] = useState<string | null>(null);

  const weekEls = useRef(new Map<string, HTMLDivElement>());
  // Guard so scrubber→scroll doesn't fight the scroll-spy.
  const lastScrubAt = useRef(0);

  useEffect(() => {
    // Brief question passed via ?q= when arriving from a research brief.
    const q = new URLSearchParams(window.location.search).get("q");
    if (q) setBriefQuestion(q);
  }, []);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const resp = await fetch(`/api/cluster/dossier?id=${encodeURIComponent(id)}`);
        if (!resp.ok) {
          const body = await resp.json().catch(() => ({}));
          throw new Error(body.error ?? `HTTP ${resp.status}`);
        }
        const payload = (await resp.json()) as DossierResponse;
        if (!cancelled) setData(payload);
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  const registerWeekEl = useCallback(
    (weekStart: string, el: HTMLDivElement | null) => {
      if (el) weekEls.current.set(weekStart, el);
      else weekEls.current.delete(weekStart);
    },
    [],
  );

  // Scrubber → anatomy only. Dragging the slider should never scroll
  // the page — that yanks the anatomy off screen mid-drag and you
  // can't watch the story move. Stays on the index update.
  const handleWeekChange = useCallback((i: number) => {
    setWeekIndex(i);
  }, []);

  // On release, line the feed up with the chosen week so when the
  // historian scrolls down to read, they're already on the right card.
  // The scroll-spy guard suppresses the rebound during the smooth
  // scroll.
  const handleWeekCommit = useCallback(
    (i: number) => {
      const wk = data?.weeks[i]?.week_start;
      if (!wk) return;
      lastScrubAt.current = Date.now();
      weekEls.current
        .get(wk)
        ?.scrollIntoView({ behavior: "smooth", block: "start" });
    },
    [data],
  );

  // Feed → scrubber (scroll-spy): watch week dividers; the divider
  // nearest the top of the viewport sets the scrub position, unless a
  // scrub just happened (its smooth-scroll would echo back).
  useEffect(() => {
    if (!data || data.weeks.length === 0) return;
    const weekIdxByStart = new Map(
      data.weeks.map((w, i) => [w.week_start, i]),
    );
    const observer = new IntersectionObserver(
      (entries) => {
        if (Date.now() - lastScrubAt.current < 1200) return;
        for (const entry of entries) {
          if (!entry.isIntersecting) continue;
          const wk = (entry.target as HTMLElement).dataset.week;
          const idx = wk ? weekIdxByStart.get(wk) : undefined;
          if (idx !== undefined) setWeekIndex(idx);
        }
      },
      { rootMargin: "0px 0px -75% 0px", threshold: 0 },
    );
    for (const el of weekEls.current.values()) observer.observe(el);
    return () => observer.disconnect();
  }, [data]);

  const toggleStar = useCallback((chunkId: string) => {
    setStarred((prev) => {
      const next = new Set(prev);
      if (next.has(chunkId)) next.delete(chunkId);
      else next.add(chunkId);
      return next;
    });
  }, []);

  const exportMarkdown = useMemo(() => {
    if (!data) return "";
    const lines: string[] = [];
    const label = data.cluster.label_text ?? `cluster #${data.cluster.label}`;
    lines.push(`# Herald citations — ${label}`);
    if (briefQuestion) lines.push(`Research question: ${briefQuestion}`);
    lines.push(`Generated: ${new Date().toISOString().slice(0, 10)}`);
    lines.push("");
    for (const ch of data.chunks) {
      if (!starred.has(ch.chunk_id)) continue;
      lines.push(
        `- ${ch.paper_title}, ${ch.date}, p.${ch.page_sequence}` +
          (ch.edition > 1 ? ` (ed. ${ch.edition})` : "") +
          `. ${ch.loc_url}`,
      );
    }
    return lines.join("\n") + "\n";
  }, [data, starred, briefQuestion]);

  const handleCopy = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(exportMarkdown);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      // clipboard may be unavailable; the text is visible to select manually
    }
  }, [exportMarkdown]);

  const handleDownload = useCallback(() => {
    const blob = new Blob([exportMarkdown], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "herald-citations.md";
    a.click();
    URL.revokeObjectURL(url);
  }, [exportMarkdown]);

  if (loading) {
    return (
      <Shell>
        <div className="flex items-center gap-2 text-stone-500 text-sm py-12 justify-center">
          <span className="inline-block w-4 h-4 border-2 border-stone-500 border-t-transparent rounded-full animate-spin" />
          Loading dossier…
        </div>
      </Shell>
    );
  }

  if (error || !data) {
    return (
      <Shell>
        <p className="text-sm text-red-400 py-12 text-center">
          {error ?? "Failed to load"}
        </p>
      </Shell>
    );
  }

  const { cluster } = data;
  const label = cluster.label_text ?? `(unlabeled cluster #${cluster.label})`;

  return (
    <Shell>
      {/* Header */}
      <header className="space-y-1.5 pb-3">
        <h1 className="font-serif text-lg text-stone-100 leading-snug break-words">
          {label}
        </h1>
        <div className="flex flex-wrap items-center gap-2 text-[11px]">
          <span className="px-2 py-0.5 rounded-full bg-stone-100 text-stone-900">
            {cluster.shape_tag}
          </span>
          <span className="text-stone-500 font-mono">
            {cluster.active_size} active / {cluster.size} stored
          </span>
          <span className="text-stone-500 font-mono">
            {cluster.date_min || "?"} → {cluster.date_max || "?"}
          </span>
        </div>
        <p className="text-xs text-stone-500 italic">
          {cluster.shape_explanation}
        </p>
      </header>

      {cluster.active_size === 0 ? (
        <div className="rounded border border-stone-800 bg-stone-900 p-6 text-center">
          <p className="text-sm text-stone-400">
            This cluster has no readable chunks.
          </p>
          <p className="text-xs text-stone-600 mt-1">
            Every member was quarantined as unreadable OCR. Quarantine
            mining — recovering what these pages likely covered — is a
            future phase.
          </p>
        </div>
      ) : (
        <>
          <ClusterAnatomy
            weeks={data.weeks}
            papers={data.papers}
            chunks={data.chunks}
            driftNet={cluster.drift_net}
            driftRatio={cluster.drift_ratio}
            weekIndex={Math.min(weekIndex, Math.max(0, data.weeks.length - 1))}
            onWeekChange={handleWeekChange}
            onWeekCommit={handleWeekCommit}
          />

          <div className="pt-4">
            <h2 className="text-[11px] uppercase tracking-wide text-stone-500 pb-1">
              Evidence — {data.chunks.length} chunks
            </h2>
            <EvidenceFeed
              chunks={data.chunks}
              papers={data.papers}
              starred={starred}
              onToggleStar={toggleStar}
              registerWeekEl={registerWeekEl}
            />
          </div>
        </>
      )}

      {/* Export panel */}
      {showExport && starred.size > 0 && (
        <div className="fixed inset-x-0 bottom-12 z-30 max-w-2xl mx-auto px-3">
          <div className="rounded-t border border-stone-700 bg-stone-900 p-3 space-y-2 shadow-xl">
            <div className="flex items-center justify-between">
              <h3 className="text-xs uppercase tracking-wide text-stone-400">
                Export starred citations
              </h3>
              <button
                onClick={() => setShowExport(false)}
                className="text-stone-500 text-sm px-2"
              >
                Close
              </button>
            </div>
            <pre className="text-[10px] text-stone-300 bg-stone-950 rounded p-2 max-h-44 overflow-y-auto whitespace-pre-wrap break-words">
              {exportMarkdown}
            </pre>
            <div className="flex gap-2">
              <button
                onClick={handleCopy}
                className="flex-1 py-2 rounded bg-stone-100 text-stone-900 text-xs font-medium"
              >
                {copied ? "Copied ✓" : "Copy to clipboard"}
              </button>
              <button
                onClick={handleDownload}
                className="flex-1 py-2 rounded border border-stone-600 text-stone-200 text-xs"
              >
                Download .md
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Sticky star footer */}
      {starred.size > 0 && (
        <footer className="fixed inset-x-0 bottom-0 z-30 bg-stone-900/95 backdrop-blur border-t border-stone-700">
          <div className="max-w-2xl mx-auto px-4 h-12 flex items-center justify-between">
            <span className="text-xs text-amber-300">
              ★ {starred.size} starred
            </span>
            <button
              onClick={() => setShowExport((v) => !v)}
              className="text-xs px-3 py-1.5 rounded bg-amber-400 text-stone-900 font-medium"
            >
              Export
            </button>
          </div>
        </footer>
      )}
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-full bg-stone-950 text-stone-200">
      <div className="max-w-2xl mx-auto px-3 py-3 pb-20">
        <nav className="flex items-center gap-2 pb-2 text-xs">
          <a
            href="/brief"
            className="text-stone-500 hover:text-stone-300 border border-stone-800 rounded px-2 py-1"
          >
            ← Brief
          </a>
          <a
            href="/explore"
            className="text-stone-500 hover:text-stone-300 border border-stone-800 rounded px-2 py-1"
          >
            Explore
          </a>
          <a
            href="/"
            className="text-stone-500 hover:text-stone-300 border border-stone-800 rounded px-2 py-1"
          >
            Chat
          </a>
        </nav>
        {children}
      </div>
    </div>
  );
}
