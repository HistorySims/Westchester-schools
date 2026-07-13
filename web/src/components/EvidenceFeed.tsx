"use client";

// The dossier's evidence feed: every active chunk in chronological
// order with sticky week dividers. Each card is an index entry, not a
// quote — muted OCR excerpt, opacity by quality, prominent tap target
// to the LoC page image. Built for fast one-thumb skimming.

import { useMemo } from "react";
import type { DossierChunk } from "@/lib/dossier";
import {
  formatWeekLabel,
  isoWeekStartStr,
  paperColor,
  qualityOpacity,
  shortPaperName,
} from "@/lib/dossier";

interface Props {
  chunks: DossierChunk[];
  papers: { lccn: string; title: string }[];
  starred: Set<string>;
  onToggleStar: (chunkId: string) => void;
  registerWeekEl: (weekStart: string, el: HTMLDivElement | null) => void;
}

export function EvidenceFeed({
  chunks,
  papers,
  starred,
  onToggleStar,
  registerWeekEl,
}: Props) {
  const paperIdx = useMemo(() => {
    const m = new Map<string, number>();
    papers.forEach((p, i) => m.set(p.lccn, i));
    return m;
  }, [papers]);

  const byWeek = useMemo(() => {
    const groups: { week: string; items: DossierChunk[] }[] = [];
    let cur: { week: string; items: DossierChunk[] } | null = null;
    for (const ch of chunks) {
      const wk = isoWeekStartStr(ch.date);
      if (!cur || cur.week !== wk) {
        cur = { week: wk, items: [] };
        groups.push(cur);
      }
      cur.items.push(ch);
    }
    return groups;
  }, [chunks]);

  if (chunks.length === 0) {
    return (
      <div className="rounded border border-stone-800 bg-stone-900 p-6 text-center">
        <p className="text-sm text-stone-400">
          No active chunks in this cluster.
        </p>
        <p className="text-xs text-stone-600 mt-1">
          Every member was quarantined as unreadable OCR.
        </p>
      </div>
    );
  }

  return (
    <div>
      {byWeek.map((group) => (
        <section key={group.week}>
          <div
            ref={(el) => registerWeekEl(group.week, el)}
            data-week={group.week}
            className="sticky top-0 z-10 bg-stone-950/95 backdrop-blur-sm py-1.5 border-b border-stone-800"
          >
            <h3 className="text-[11px] uppercase tracking-wide text-stone-400">
              {formatWeekLabel(group.week)}
              <span className="text-stone-600"> · {group.items.length}</span>
            </h3>
          </div>

          <ul className="divide-y divide-stone-800/60">
            {group.items.map((ch) => {
              const isStarred = starred.has(ch.chunk_id);
              const color = paperColor(paperIdx.get(ch.paper_lccn) ?? 0);
              return (
                <li
                  key={ch.chunk_id}
                  className="py-2"
                  style={{ opacity: qualityOpacity(ch.quality) }}
                >
                  <div className="flex items-start gap-2">
                    <div className="flex-1 min-w-0">
                      <div className="flex items-baseline gap-1.5 text-[11px] text-stone-400 font-mono">
                        <span
                          className="inline-block w-2 h-2 rounded-full flex-shrink-0 self-center"
                          style={{ backgroundColor: color }}
                        />
                        <span className="text-stone-300">{ch.date}</span>
                        <span className="truncate">
                          {shortPaperName(ch.paper_title)}
                        </span>
                        <span className="flex-shrink-0">p.{ch.page_sequence}</span>
                      </div>
                      <p className="mt-0.5">
                        <span className="text-[9px] uppercase tracking-wider text-stone-600 mr-1.5">
                          OCR excerpt
                        </span>
                        <span className="text-xs text-stone-500 leading-snug">
                          {ch.excerpt}
                        </span>
                      </p>
                    </div>

                    <div className="flex flex-col items-center gap-1 flex-shrink-0">
                      <button
                        type="button"
                        onClick={() => onToggleStar(ch.chunk_id)}
                        aria-label={isStarred ? "Unstar" : "Star"}
                        className={`w-9 h-9 flex items-center justify-center rounded text-lg leading-none ${
                          isStarred
                            ? "text-amber-300"
                            : "text-stone-600 active:text-stone-400"
                        }`}
                      >
                        {isStarred ? "★" : "☆"}
                      </button>
                      <a
                        href={ch.loc_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="text-[10px] px-2 py-1.5 rounded border border-stone-700 text-amber-400 active:bg-stone-800 whitespace-nowrap"
                      >
                        LoC →
                      </a>
                    </div>
                  </div>
                </li>
              );
            })}
          </ul>
        </section>
      ))}
    </div>
  );
}
