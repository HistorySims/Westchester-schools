"use client";

// The dossier's anatomy panel: three bands sharing one time axis.
//   Band 1 — the stream: weekly chunk counts stacked by paper.
//   Band 2 — the comet trail: weekly centroids in UMAP space over a
//            faint member point-cloud, colored pale→saturated by time.
//   Band 3 — the word river: per-week c-TF-IDF terms, scrubbed week
//            prominent, neighbors dimmed.
// One scrubber drives all three (plus the evidence feed via the
// parent). Color = paper throughout; opacity = OCR quality.

import { useMemo } from "react";
import type { DossierChunk, DossierWeek } from "@/lib/dossier";
import { paperColor, qualityOpacity, shortPaperName } from "@/lib/dossier";

const W = 360;
const PAD_X = 12;
const STREAM_H = 76;
const COMET_H = 156;
const TRAIL_COLOR = "#fbbf24";

interface Props {
  weeks: DossierWeek[];
  papers: { lccn: string; title: string }[];
  chunks: DossierChunk[];
  driftNet: number | null;
  driftRatio: number | null;
  weekIndex: number;
  onWeekChange: (i: number) => void;
  // Called only when the user releases the scrubber, so the parent
  // can scroll the evidence feed without yanking the anatomy off
  // screen mid-drag.
  onWeekCommit?: (i: number) => void;
}

export function ClusterAnatomy({
  weeks,
  papers,
  chunks,
  driftNet,
  driftRatio,
  weekIndex,
  onWeekChange,
  onWeekCommit,
}: Props) {
  const n = weeks.length;
  const xAt = (i: number) =>
    n <= 1 ? W / 2 : PAD_X + (i * (W - 2 * PAD_X)) / (n - 1);

  const maxCount = useMemo(
    () => Math.max(1, ...weeks.map((w) => w.chunk_count)),
    [weeks],
  );

  // Stream geometry: silhouette-centered stack. For each week, the
  // stream's half-height scales with count; paper layers split it by
  // their share that week.
  const streamLayers = useMemo(() => {
    const center = STREAM_H / 2;
    const maxHalf = STREAM_H / 2 - 6;
    // Per week: array of [yTop, yBottom] per paper.
    const perWeek = weeks.map((w) => {
      const total = w.chunk_count;
      const half = (total / maxCount) * maxHalf;
      const top = center - half;
      const height = 2 * half;
      let cum = 0;
      return papers.map((p) => {
        const cnt = w.count_by_paper[p.lccn] ?? 0;
        const y0 = top + (total > 0 ? (cum / total) * height : 0);
        cum += cnt;
        const y1 = top + (total > 0 ? (cum / total) * height : 0);
        return [y0, y1] as [number, number];
      });
    });
    return perWeek;
  }, [weeks, papers, maxCount]);

  // Comet underlay: normalize member UMAP coords into the band rect.
  const comet = useMemo(() => {
    const xs = chunks.map((c) => c.x);
    const ys = chunks.map((c) => c.y);
    const minX = Math.min(...xs, ...weeks.map((w) => w.centroid_x ?? Infinity));
    const maxX = Math.max(...xs, ...weeks.map((w) => w.centroid_x ?? -Infinity));
    const minY = Math.min(...ys, ...weeks.map((w) => w.centroid_y ?? Infinity));
    const maxY = Math.max(...ys, ...weeks.map((w) => w.centroid_y ?? -Infinity));
    const spanX = Math.max(1e-6, maxX - minX);
    const spanY = Math.max(1e-6, maxY - minY);
    const pad = 14;
    const px = (x: number) => pad + ((x - minX) / spanX) * (W - 2 * pad);
    // Flip Y so the orientation matches the explore map (SVG y grows down).
    const py = (y: number) =>
      COMET_H - pad - ((y - minY) / spanY) * (COMET_H - 2 * pad);
    return { px, py };
  }, [chunks, weeks]);

  const paperIdx = useMemo(() => {
    const m = new Map<string, number>();
    papers.forEach((p, i) => m.set(p.lccn, i));
    return m;
  }, [papers]);

  const current = weeks[weekIndex];
  const prev = weeks[weekIndex - 1];
  const next = weeks[weekIndex + 1];

  if (n === 0) return null;

  return (
    <div className="space-y-2">
      {/* Paper legend */}
      <div className="flex flex-wrap gap-3 text-[11px] text-stone-400">
        {papers.map((p, i) => (
          <span key={p.lccn} className="inline-flex items-center gap-1.5">
            <span
              className="inline-block w-2.5 h-2.5 rounded-full"
              style={{ backgroundColor: paperColor(i) }}
            />
            {shortPaperName(p.title)}
          </span>
        ))}
        <span className="text-stone-600">· opacity = OCR quality</span>
      </div>

      {/* Band 1 — the stream */}
      <svg
        viewBox={`0 0 ${W} ${STREAM_H}`}
        className="w-full block rounded bg-stone-900"
        preserveAspectRatio="none"
      >
        {n === 1 ? (
          papers.map((p, pi) => {
            const [y0, y1] = streamLayers[0][pi];
            const q = weeks[0].mean_ocr_quality ?? 1;
            return (
              <rect
                key={p.lccn}
                x={W / 2 - 10}
                width={20}
                y={y0}
                height={Math.max(0.5, y1 - y0)}
                fill={paperColor(pi)}
                opacity={qualityOpacity(q)}
              />
            );
          })
        ) : (
          weeks.slice(0, -1).map((w, i) => {
            const qa = w.mean_ocr_quality ?? 1;
            const qb = weeks[i + 1].mean_ocr_quality ?? 1;
            const segOpacity = qualityOpacity((qa + qb) / 2);
            return papers.map((p, pi) => {
              const [a0, a1] = streamLayers[i][pi];
              const [b0, b1] = streamLayers[i + 1][pi];
              if (a1 - a0 < 0.01 && b1 - b0 < 0.01) return null;
              const x0 = xAt(i);
              const x1 = xAt(i + 1);
              return (
                <polygon
                  key={`${p.lccn}-${i}`}
                  points={`${x0},${a0} ${x1},${b0} ${x1},${b1} ${x0},${a1}`}
                  fill={paperColor(pi)}
                  opacity={segOpacity}
                />
              );
            });
          })
        )}
        {/* Scrub cursor */}
        <line
          x1={xAt(weekIndex)}
          x2={xAt(weekIndex)}
          y1={2}
          y2={STREAM_H - 2}
          stroke="#fafaf9"
          strokeWidth={1}
          opacity={0.8}
        />
      </svg>

      {/* Band 2 — the comet trail */}
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${COMET_H}`}
          className="w-full block rounded bg-stone-900"
        >
          {/* Member point-cloud underlay */}
          {chunks.map((c) => (
            <circle
              key={c.chunk_id}
              cx={comet.px(c.x)}
              cy={comet.py(c.y)}
              r={1.6}
              fill={paperColor(paperIdx.get(c.paper_lccn) ?? 0)}
              opacity={0.12 + 0.18 * c.quality}
            />
          ))}
          {/* Weekly centroid trail up to the scrubbed week, pale → saturated */}
          {weeks.slice(0, Math.max(1, weekIndex + 1)).map((w, i, arr) => {
            if (i === 0) return null;
            const a = arr[i - 1];
            if (
              a.centroid_x === null || a.centroid_y === null ||
              w.centroid_x === null || w.centroid_y === null
            ) {
              return null;
            }
            const t = arr.length > 1 ? i / (arr.length - 1) : 1;
            return (
              <line
                key={w.week_start}
                x1={comet.px(a.centroid_x)}
                y1={comet.py(a.centroid_y)}
                x2={comet.px(w.centroid_x)}
                y2={comet.py(w.centroid_y)}
                stroke={TRAIL_COLOR}
                strokeWidth={2}
                strokeLinecap="round"
                opacity={0.15 + 0.8 * t}
              />
            );
          })}
          {/* Marker at the scrubbed week */}
          {current?.centroid_x !== null &&
            current?.centroid_y !== null &&
            current && (
              <circle
                cx={comet.px(current.centroid_x!)}
                cy={comet.py(current.centroid_y!)}
                r={4.5}
                fill={TRAIL_COLOR}
                stroke="#fafaf9"
                strokeWidth={1.2}
              />
            )}
        </svg>
        {/* Honest numbers beside the suggestive picture */}
        <div className="absolute top-1.5 right-2 text-right text-[10px] font-mono text-stone-400 bg-stone-900/80 rounded px-1.5 py-0.5">
          <div>net drift {driftNet !== null ? driftNet.toFixed(3) : "—"}</div>
          <div>direction {driftRatio !== null ? driftRatio.toFixed(2) : "—"}</div>
        </div>
      </div>

      {/* Band 3 — the word river */}
      <div className="rounded bg-stone-900 px-3 py-2 min-h-[64px]">
        {current?.top_terms?.length ? (
          <div className="flex items-baseline gap-2 flex-wrap">
            {prev?.top_terms?.slice(0, 2).map((t) => (
              <span key={`p-${t}`} className="text-[10px] text-stone-600">
                {t}
              </span>
            ))}
            {current.top_terms.map((t) => (
              <span
                key={t}
                className="text-sm font-serif text-amber-300"
              >
                {t}
              </span>
            ))}
            {next?.top_terms?.slice(0, 2).map((t) => (
              <span key={`n-${t}`} className="text-[10px] text-stone-600">
                {t}
              </span>
            ))}
          </div>
        ) : (
          <p className="text-[11px] text-stone-600 italic">
            No distinguishing terms for this week yet — run the
            cluster-weeks pipeline to populate the word river.
          </p>
        )}
      </div>

      {/* The scrubber */}
      <div className="space-y-1">
        <input
          type="range"
          min={0}
          max={n - 1}
          step={1}
          value={weekIndex}
          onChange={(e) => onWeekChange(parseInt(e.target.value, 10))}
          onPointerUp={(e) =>
            onWeekCommit?.(parseInt((e.target as HTMLInputElement).value, 10))
          }
          onTouchEnd={(e) =>
            onWeekCommit?.(parseInt((e.target as HTMLInputElement).value, 10))
          }
          onKeyUp={(e) =>
            onWeekCommit?.(parseInt((e.target as HTMLInputElement).value, 10))
          }
          className="w-full accent-amber-400 touch-none h-8"
          aria-label="Scrub week"
        />
        <div className="flex justify-between text-[10px] font-mono text-stone-500">
          <span>{weeks[0].week_start}</span>
          <span className="text-amber-300">
            {current?.week_start} · {current?.chunk_count} chunks
          </span>
          <span>{weeks[n - 1].week_start}</span>
        </div>
      </div>
    </div>
  );
}
