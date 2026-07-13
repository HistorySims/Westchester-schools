"use client";

import { useEffect, useState } from "react";
import type { Citation, AskResponse } from "@/lib/types";

interface ClusterStoryProps {
  tier: number;
  label: number;
  onClose: () => void;
}

export function ClusterStory({ tier, label, onClose }: ClusterStoryProps) {
  const [text, setText] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [streaming, setStreaming] = useState(true);
  const [cacheHit, setCacheHit] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [refreshCounter, setRefreshCounter] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setText("");
    setCitations([]);
    setStreaming(true);
    setCacheHit(false);
    setError(null);

    async function run() {
      try {
        const resp = await fetch("/api/explore/cluster-story", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tier, label, refresh: refreshCounter > 0 }),
        });
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.error || `HTTP ${resp.status}`);
        }
        if (resp.headers.get("X-Cache") === "HIT") setCacheHit(true);

        const reader = resp.body?.getReader();
        if (!reader) throw new Error("No body");

        const decoder = new TextDecoder();
        let buffer = "";
        let streamed = "";
        let eventType = "";

        const processLine = (line: string) => {
          if (line.startsWith("event: ")) {
            eventType = line.slice(7).trim();
          } else if (line.startsWith("data: ")) {
            const parsed = JSON.parse(line.slice(6));
            if (eventType === "token") {
              streamed += parsed.text;
              if (!cancelled) setText(streamed);
            } else if (eventType === "done") {
              const resp = parsed as AskResponse;
              if (!cancelled) {
                setText(resp.text);
                setCitations(resp.citations);
              }
            } else if (eventType === "error") {
              throw new Error(parsed.error);
            }
          }
        };

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const lines = buffer.split("\n");
          buffer = lines.pop() ?? "";
          for (const line of lines) {
            try { processLine(line); }
            catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
        buffer += decoder.decode();
        if (buffer.trim()) {
          for (const line of buffer.split("\n")) {
            try { processLine(line); }
            catch (e) {
              if (e instanceof SyntaxError) continue;
              throw e;
            }
          }
        }
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e.message : "Failed");
      } finally {
        if (!cancelled) setStreaming(false);
      }
    }

    run();
    return () => { cancelled = true; };
  }, [tier, label, refreshCounter]);

  return (
    <div className="border-t border-stone-700 p-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide flex items-center gap-2">
          What&apos;s the Story?
          {cacheHit && (
            <span className="text-[9px] text-stone-500 font-normal normal-case">
              (cached)
            </span>
          )}
        </h3>
        <div className="flex items-center gap-3">
          {!streaming && (
            <button
              onClick={() => setRefreshCounter((c) => c + 1)}
              className="text-amber-500 hover:text-amber-400 text-xs underline"
              title="Re-run synthesis (skips cache, costs ~$0.05)"
            >
              Regenerate
            </button>
          )}
          <button
            onClick={onClose}
            className="text-stone-500 hover:text-stone-300 text-sm"
          >
            Close
          </button>
        </div>
      </div>

      {error && (
        <p className="text-xs text-red-400 mb-2">Error: {error}</p>
      )}

      <div className="text-sm text-stone-300 leading-relaxed font-serif whitespace-pre-wrap">
        {text}
        {streaming && !cacheHit && (
          <span className="text-stone-500 animate-pulse">|</span>
        )}
      </div>

      {!streaming && citations.length > 0 && (
        <div className="mt-3 pt-3 border-t border-stone-700">
          <p className="text-xs font-sans text-stone-500 mb-1">Sources</p>
          <div className="space-y-1">
            {citations
              .filter((c) => new RegExp(`\\[${c.index}\\]`).test(text))
              .map((c) => (
                <a
                  key={c.index}
                  href={c.image_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="block text-xs font-mono text-stone-400 hover:text-amber-400"
                >
                  [{c.index}] {c.paper_title}, {c.date_issued}, p.{c.page_sequence}
                </a>
              ))}
          </div>
        </div>
      )}
    </div>
  );
}
