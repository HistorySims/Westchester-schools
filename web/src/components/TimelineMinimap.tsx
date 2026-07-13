"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { TimelineData } from "@/lib/explore-data";
import { clusterColor } from "@/lib/explore-data";

interface TimelineMinimapProps {
  timeline: TimelineData;
  chunkIds: string[] | null;
  tier: number;
  searchMatches: Set<number> | null;
  contentFilter: Set<number>;
  minDate: string;
  onChunkHover?: (chunkIndex: number | null) => void;
  onChunkClick?: (chunkIndex: number) => void;
}

const GUTTER_PX = 14;        // left axis area for date labels
const COLUMN_PAD = 2;
const MIN_CHUNK_HEIGHT = 1;
const MAX_CHUNK_HEIGHT = 16;
const DEFAULT_CHUNK_HEIGHT = 3;
const DAY_PAD_PX = 0;        // optional padding between date rows

const MORSE_SEGMENTS = 7;
const SEARCH_CAP_FRAC = 0.10;

// Inertia / momentum constants
const WHEEL_GAIN = 1.6;             // amplify trackpad wheel
const VELOCITY_SAMPLE_MS = 80;      // window for averaging touch velocity
const FLING_FRICTION = 0.94;        // per-frame velocity decay (≈60fps)
const FLING_MIN_SPEED = 0.05;       // px/ms — below this we stop the rAF
const FLING_MAX_SPEED = 4.5;        // px/ms cap so a furious flick doesn't shoot to the end

// Deterministic pseudo-random noise table so the Morse pattern is
// stable across redraws but varies per (chunk, segment).
const NOISE_LEN = 1024;
const noiseArr = new Float32Array(NOISE_LEN);
for (let i = 0; i < NOISE_LEN; i++) {
  // Cheap LCG-derived noise — Math.random is fine here since we only
  // generate this once at module load and bake it in.
  noiseArr[i] = Math.random();
}

interface DayLayout {
  date: number;             // dateOffset
  startY: number;           // top of this date's row
  rowHeight: number;        // maxChunksAnyPaperThisDay * chunkHeight
  byPaper: Map<number, number[]>;  // paperIdx → sorted list of global chunk indices
}

export function TimelineMinimap({
  timeline,
  chunkIds,
  tier,
  searchMatches,
  contentFilter,
  minDate,
  onChunkHover,
  onChunkClick,
}: TimelineMinimapProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);

  const [chunkHeight, setChunkHeight] = useState(DEFAULT_CHUNK_HEIGHT);
  const [scrollY, setScrollY] = useState(0);
  const [canvasSize, setCanvasSize] = useState({ w: 0, h: 0 });
  const [hoveredGlobalIdx, setHoveredGlobalIdx] = useState<number | null>(null);
  const [tooltipPos, setTooltipPos] = useState<{ x: number; y: number } | null>(
    null
  );

  const clusterArr = useMemo(() => {
    switch (tier) {
      case 0: return timeline.clusterT0;
      case 1: return timeline.clusterT1;
      case 2: return timeline.clusterT2;
      case 3: return timeline.clusterT3;
      default: return timeline.clusterT2;
    }
  }, [timeline, tier]);

  // Group chunks by (date, paper). For each date compute the maximum
  // chunk count across all papers — that's how tall the row needs to
  // be so chunks have consistent height regardless of which paper
  // they belong to, and dates remain aligned across columns.
  const layout = useMemo(() => {
    // First pass: bucket by date → paper → [globalIdx]
    const byDate = new Map<number, Map<number, number[]>>();
    let maxDate = 0;
    for (let i = 0; i < timeline.count; i++) {
      if (!contentFilter.has(timeline.contentType[i])) continue;
      const d = timeline.dateOffset[i];
      const p = timeline.paperIdx[i];
      if (d > maxDate) maxDate = d;
      let dm = byDate.get(d);
      if (!dm) {
        dm = new Map();
        byDate.set(d, dm);
      }
      let list = dm.get(p);
      if (!list) {
        list = [];
        dm.set(p, list);
      }
      list.push(i);
    }
    // Sort each paper's bucket so rendering is stable
    for (const dm of byDate.values()) {
      for (const list of dm.values()) list.sort((a, b) => a - b);
    }

    const dates = Array.from(byDate.keys()).sort((a, b) => a - b);
    const days: DayLayout[] = [];
    let cursorY = 0;
    for (const d of dates) {
      const byPaper = byDate.get(d)!;
      let maxN = 0;
      for (const list of byPaper.values()) {
        if (list.length > maxN) maxN = list.length;
      }
      const rowHeight = maxN * chunkHeight + DAY_PAD_PX;
      days.push({ date: d, startY: cursorY, rowHeight, byPaper });
      cursorY += rowHeight;
    }
    return { days, totalHeight: cursorY, maxDate };
  }, [timeline, contentFilter, chunkHeight]);

  // Resize the canvas
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const resize = () => {
      const rect = container.getBoundingClientRect();
      setCanvasSize({ w: Math.max(1, rect.width), h: Math.max(1, rect.height) });
    };
    resize();
    const ro = new ResizeObserver(resize);
    ro.observe(container);
    window.addEventListener("resize", resize);
    return () => {
      ro.disconnect();
      window.removeEventListener("resize", resize);
    };
  }, []);

  const maxScroll = Math.max(0, layout.totalHeight - canvasSize.h);

  useEffect(() => {
    setScrollY((y) => Math.max(0, Math.min(y, maxScroll)));
  }, [maxScroll]);

  // Binary search the day at a virtual Y position
  const findDayAtY = useCallback(
    (yVirtual: number): number => {
      const days = layout.days;
      if (days.length === 0 || yVirtual < 0) return -1;
      let lo = 0;
      let hi = days.length - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const start = days[mid].startY;
        const end = start + days[mid].rowHeight;
        if (yVirtual < start) hi = mid - 1;
        else if (yVirtual >= end) lo = mid + 1;
        else return mid;
      }
      // Past the last day
      return Math.min(lo, days.length - 1);
    },
    [layout]
  );

  // Fling/inertia state. Velocity is px/ms (positive = scrolling down).
  const flingRafRef = useRef<number | null>(null);
  const flingVelocityRef = useRef<number>(0);
  const maxScrollRef = useRef<number>(0);
  maxScrollRef.current = maxScroll;

  const stopFling = useCallback(() => {
    if (flingRafRef.current !== null) {
      cancelAnimationFrame(flingRafRef.current);
      flingRafRef.current = null;
    }
    flingVelocityRef.current = 0;
  }, []);

  const startFling = useCallback((initialV: number) => {
    if (Math.abs(initialV) < FLING_MIN_SPEED) return;
    const capped = Math.max(-FLING_MAX_SPEED, Math.min(FLING_MAX_SPEED, initialV));
    flingVelocityRef.current = capped;
    let last = performance.now();
    const tick = (now: number) => {
      const dt = Math.max(1, now - last);
      last = now;
      // 60fps-relative decay so trackpad jitter doesn't change feel
      const decay = Math.pow(FLING_FRICTION, dt / 16.67);
      flingVelocityRef.current *= decay;
      const v = flingVelocityRef.current;
      setScrollY((y) => {
        const next = y + v * dt;
        if (next <= 0 || next >= maxScrollRef.current) {
          flingVelocityRef.current = 0;
        }
        return Math.max(0, Math.min(maxScrollRef.current, next));
      });
      if (Math.abs(flingVelocityRef.current) < FLING_MIN_SPEED) {
        flingRafRef.current = null;
        return;
      }
      flingRafRef.current = requestAnimationFrame(tick);
    };
    flingRafRef.current = requestAnimationFrame(tick);
  }, []);

  useEffect(() => () => stopFling(), [stopFling]);

  // Wheel: pan / zoom with cursor focus preserved
  const onWheel = useCallback(
    (e: React.WheelEvent<HTMLCanvasElement>) => {
      e.preventDefault();
      stopFling();
      if (e.ctrlKey || e.metaKey) {
        const canvas = canvasRef.current;
        if (!canvas) return;
        const rect = canvas.getBoundingClientRect();
        const cursorY = e.clientY - rect.top;
        const targetVirtualY = scrollY + cursorY;
        // What fraction of the total height was the cursor at?
        const fraction =
          layout.totalHeight > 0 ? targetVirtualY / layout.totalHeight : 0;
        const dir = e.deltaY < 0 ? 1.15 : 1 / 1.15;
        const next = Math.max(
          MIN_CHUNK_HEIGHT,
          Math.min(MAX_CHUNK_HEIGHT, chunkHeight * dir)
        );
        setChunkHeight(next);
        // After zoom, position cursor over the same fraction of corpus
        // (we don't know newTotalHeight yet because the memo hasn't
        // refired; estimate by scaling)
        const scale = next / chunkHeight;
        const newTotal = layout.totalHeight * scale;
        setScrollY(Math.max(0, fraction * newTotal - cursorY));
      } else {
        setScrollY((y) =>
          Math.max(0, Math.min(maxScroll, y + e.deltaY * WHEEL_GAIN))
        );
      }
    },
    [chunkHeight, scrollY, maxScroll, layout, stopFling]
  );

  // Mouse drag — listeners on window so we don't lose track on canvas exit.
  // Tracks recent velocity samples so releasing the mouse mid-flick continues
  // the scroll under inertia, matching the touch behaviour.
  const dragState = useRef<{
    startY: number;
    startScroll: number;
    samples: { t: number; y: number }[];
  } | null>(null);
  const onMouseDown = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      stopFling();
      dragState.current = {
        startY: e.clientY,
        startScroll: scrollY,
        samples: [{ t: performance.now(), y: e.clientY }],
      };
      const onMove = (ev: MouseEvent) => {
        const ds = dragState.current;
        if (!ds) return;
        const dy = ds.startY - ev.clientY;
        setScrollY(
          Math.max(0, Math.min(maxScrollRef.current, ds.startScroll + dy))
        );
        const now = performance.now();
        ds.samples.push({ t: now, y: ev.clientY });
        const cutoff = now - VELOCITY_SAMPLE_MS;
        while (ds.samples.length > 2 && ds.samples[0].t < cutoff) {
          ds.samples.shift();
        }
      };
      const onUp = () => {
        const ds = dragState.current;
        dragState.current = null;
        window.removeEventListener("mousemove", onMove);
        window.removeEventListener("mouseup", onUp);
        if (ds && ds.samples.length >= 2) {
          const first = ds.samples[0];
          const last = ds.samples[ds.samples.length - 1];
          const dt = last.t - first.t;
          if (dt > 0) {
            // Velocity in screen-Y px/ms; scroll moves opposite (drag down → scroll up)
            const screenV = (last.y - first.y) / dt;
            startFling(-screenV);
          }
        }
      };
      window.addEventListener("mousemove", onMove);
      window.addEventListener("mouseup", onUp);
    },
    [scrollY, stopFling, startFling]
  );

  // Hit-test the chunk at (x, y) on the canvas
  const indexAt = useCallback(
    (x: number, y: number): number | null => {
      if (canvasSize.w <= 0 || timeline.papers.length === 0) return null;
      if (x < GUTTER_PX) return null;
      const usableW = canvasSize.w - GUTTER_PX;
      const colW = usableW / timeline.papers.length;
      const colIdx = Math.floor((x - GUTTER_PX) / colW);
      if (colIdx < 0 || colIdx >= timeline.papers.length) return null;

      const yVirtual = y + scrollY;
      const dayIdx = findDayAtY(yVirtual);
      if (dayIdx < 0) return null;
      const day = layout.days[dayIdx];
      const yInDay = yVirtual - day.startY;
      const chunkInDay = Math.floor(yInDay / chunkHeight);
      const paperChunks = day.byPaper.get(colIdx);
      if (!paperChunks || chunkInDay < 0 || chunkInDay >= paperChunks.length) {
        return null;
      }
      return paperChunks[chunkInDay];
    },
    [canvasSize.w, layout, timeline.papers.length, scrollY, chunkHeight, findDayAtY]
  );

  const onMouseMove = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left;
      const y = e.clientY - rect.top;
      const idx = indexAt(x, y);
      if (idx !== hoveredGlobalIdx) {
        setHoveredGlobalIdx(idx);
        onChunkHover?.(idx);
      }
      setTooltipPos(idx === null ? null : { x: e.clientX, y: e.clientY });
    },
    [indexAt, hoveredGlobalIdx, onChunkHover]
  );

  const onMouseLeave = useCallback(() => {
    if (hoveredGlobalIdx !== null) {
      setHoveredGlobalIdx(null);
      onChunkHover?.(null);
    }
    setTooltipPos(null);
  }, [hoveredGlobalIdx, onChunkHover]);

  const onClick = useCallback(
    (e: React.MouseEvent<HTMLCanvasElement>) => {
      const canvas = canvasRef.current;
      if (!canvas || !onChunkClick) return;
      const rect = canvas.getBoundingClientRect();
      const idx = indexAt(e.clientX - rect.left, e.clientY - rect.top);
      if (idx !== null) onChunkClick(idx);
    },
    [indexAt, onChunkClick]
  );

  // Touch support
  const touchState = useRef<{
    startScroll: number;
    startY: number;
    startX: number;
    moved: boolean;
    pinchStartDist: number | null;
    pinchStartChunkHeight: number;
    samples: { t: number; y: number }[];
  } | null>(null);

  const onTouchStart = useCallback(
    (e: React.TouchEvent<HTMLCanvasElement>) => {
      stopFling();
      const now = performance.now();
      if (e.touches.length === 1) {
        const t = e.touches[0];
        touchState.current = {
          startScroll: scrollY,
          startY: t.clientY,
          startX: t.clientX,
          moved: false,
          pinchStartDist: null,
          pinchStartChunkHeight: chunkHeight,
          samples: [{ t: now, y: t.clientY }],
        };
      } else if (e.touches.length === 2) {
        const [a, b] = [e.touches[0], e.touches[1]];
        const dx = a.clientX - b.clientX;
        const dy = a.clientY - b.clientY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        touchState.current = {
          startScroll: scrollY,
          startY: (a.clientY + b.clientY) / 2,
          startX: (a.clientX + b.clientX) / 2,
          moved: true,
          pinchStartDist: dist,
          pinchStartChunkHeight: chunkHeight,
          samples: [],
        };
      }
    },
    [scrollY, chunkHeight, stopFling]
  );

  const onTouchMove = useCallback(
    (e: React.TouchEvent<HTMLCanvasElement>) => {
      const state = touchState.current;
      if (!state) return;
      e.preventDefault();

      if (e.touches.length === 2 && state.pinchStartDist) {
        const [a, b] = [e.touches[0], e.touches[1]];
        const dx = a.clientX - b.clientX;
        const dy = a.clientY - b.clientY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const next = Math.max(
          MIN_CHUNK_HEIGHT,
          Math.min(
            MAX_CHUNK_HEIGHT,
            state.pinchStartChunkHeight * (dist / state.pinchStartDist)
          )
        );
        setChunkHeight(next);
      } else if (e.touches.length === 1) {
        const t = e.touches[0];
        const dy = state.startY - t.clientY;
        const dx = state.startX - t.clientX;
        if (Math.abs(dy) > 4 || Math.abs(dx) > 4) state.moved = true;
        setScrollY(
          Math.max(0, Math.min(maxScroll, state.startScroll + dy))
        );
        const now = performance.now();
        state.samples.push({ t: now, y: t.clientY });
        const cutoff = now - VELOCITY_SAMPLE_MS;
        while (state.samples.length > 2 && state.samples[0].t < cutoff) {
          state.samples.shift();
        }
      }
    },
    [maxScroll]
  );

  const onTouchEnd = useCallback(
    (e: React.TouchEvent<HTMLCanvasElement>) => {
      const state = touchState.current;
      if (!state) return;
      if (!state.moved && e.changedTouches.length === 1 && onChunkClick) {
        const t = e.changedTouches[0];
        const canvas = canvasRef.current;
        if (canvas) {
          const rect = canvas.getBoundingClientRect();
          const idx = indexAt(t.clientX - rect.left, t.clientY - rect.top);
          if (idx !== null) onChunkClick(idx);
        }
      } else if (state.moved && state.samples.length >= 2) {
        // Toss inertia from the last ~80ms of touch movement.
        const first = state.samples[0];
        const last = state.samples[state.samples.length - 1];
        const dt = last.t - first.t;
        if (dt > 0) {
          const screenV = (last.y - first.y) / dt;
          startFling(-screenV);
        }
      }
      touchState.current = null;
    },
    [indexAt, onChunkClick, startFling]
  );

  // ---------- Draw ----------
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || canvasSize.w === 0 || canvasSize.h === 0) return;

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.floor(canvasSize.w * dpr);
    canvas.height = Math.floor(canvasSize.h * dpr);
    canvas.style.width = `${canvasSize.w}px`;
    canvas.style.height = `${canvasSize.h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    let rafId: number | null = null;
    const draw = () => {
      rafId = null;
      ctx.clearRect(0, 0, canvasSize.w, canvasSize.h);
      ctx.fillStyle = "#0f0f0f";
      ctx.fillRect(0, 0, canvasSize.w, canvasSize.h);

      const days = layout.days;
      if (days.length === 0) return;

      const usableW = canvasSize.w - GUTTER_PX;
      const nPapers = Math.max(1, timeline.papers.length);
      const colW = usableW / nPapers;

      // Column dividers
      ctx.fillStyle = "#1a1a1a";
      for (let c = 1; c < timeline.papers.length; c++) {
        const x = GUTTER_PX + c * colW;
        ctx.fillRect(x - 0.5, 0, 1, canvasSize.h);
      }

      // Visible date range via binary search
      const firstVisibleVirtual = scrollY;
      const lastVisibleVirtual = scrollY + canvasSize.h;
      // Find first day intersecting the viewport
      let firstDay = 0;
      let lo = 0;
      let hi = days.length - 1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        const end = days[mid].startY + days[mid].rowHeight;
        if (end < firstVisibleVirtual) lo = mid + 1;
        else hi = mid - 1;
      }
      firstDay = Math.max(0, lo);

      const baseDate = new Date(minDate);
      const MS = 24 * 60 * 60 * 1000;

      let lastLabelY = -1000;
      const LABEL_MIN_GAP = 36;

      for (let dIdx = firstDay; dIdx < days.length; dIdx++) {
        const day = days[dIdx];
        if (day.startY > lastVisibleVirtual) break;

        const yTop = day.startY - scrollY;

        // Date axis label on the left gutter (sparse, not every day)
        if (yTop > lastLabelY + LABEL_MIN_GAP && day.rowHeight > 0) {
          ctx.fillStyle = "#666";
          ctx.font = "9px ui-monospace, monospace";
          const date = new Date(baseDate.getTime() + day.date * MS);
          const label =
            date.toUTCString().slice(8, 11) + " " + date.toUTCString().slice(5, 7);
          ctx.fillText(label, 1, yTop + 8);
          lastLabelY = yTop;
        }

        // Each paper column for this day
        for (let p = 0; p < nPapers; p++) {
          const chunks = day.byPaper.get(p);
          if (!chunks || chunks.length === 0) continue;
          const colXStart = GUTTER_PX + p * colW + COLUMN_PAD;
          const colXEnd = GUTTER_PX + (p + 1) * colW - COLUMN_PAD;
          const w = Math.max(2, colXEnd - colXStart);
          for (let k = 0; k < chunks.length; k++) {
            const y = yTop + k * chunkHeight;
            if (y + chunkHeight < 0 || y > canvasSize.h) continue;
            const i = chunks[k];
            drawChunk(
              ctx,
              colXStart,
              y,
              w,
              chunkHeight,
              i,
              clusterArr[i],
              timeline.quality[i] / 255,
              searchMatches?.has(i) ?? false
            );
          }
        }
      }

      // Hover ring
      if (hoveredGlobalIdx !== null) {
        const idx = hoveredGlobalIdx;
        const d = timeline.dateOffset[idx];
        const p = timeline.paperIdx[idx];
        // find day in layout (linear search OK; the layout array is sorted by date)
        let foundDay: DayLayout | null = null;
        // small binary search by date
        let lo2 = 0;
        let hi2 = days.length - 1;
        while (lo2 <= hi2) {
          const mid = (lo2 + hi2) >> 1;
          if (days[mid].date === d) {
            foundDay = days[mid];
            break;
          }
          if (days[mid].date < d) lo2 = mid + 1;
          else hi2 = mid - 1;
        }
        if (foundDay) {
          const list = foundDay.byPaper.get(p);
          if (list) {
            const k = list.indexOf(idx);
            if (k >= 0) {
              const y = foundDay.startY + k * chunkHeight - scrollY;
              const colXStart = GUTTER_PX + p * colW;
              const colXEnd = GUTTER_PX + (p + 1) * colW;
              ctx.strokeStyle = "rgba(255,255,255,0.9)";
              ctx.lineWidth = 1;
              ctx.strokeRect(
                colXStart + 0.5,
                y - 0.5,
                colXEnd - colXStart - 1,
                Math.max(2, chunkHeight) + 1
              );
            }
          }
        }
      }
    };

    rafId = requestAnimationFrame(draw);
    return () => {
      if (rafId !== null) cancelAnimationFrame(rafId);
    };
  }, [
    canvasSize,
    layout,
    clusterArr,
    timeline,
    searchMatches,
    chunkHeight,
    scrollY,
    hoveredGlobalIdx,
    minDate,
  ]);

  const hoveredChunkInfo = useMemo(() => {
    if (hoveredGlobalIdx === null) return null;
    const idx = hoveredGlobalIdx;
    const paperIdx = timeline.paperIdx[idx];
    const paper = timeline.papers[paperIdx];
    const d = new Date(minDate);
    d.setUTCDate(d.getUTCDate() + timeline.dateOffset[idx]);
    return {
      paper: paper?.title ?? paper?.lccn ?? "—",
      date: d.toISOString().slice(0, 10),
      quality: timeline.quality[idx] / 255,
      cluster: clusterArr[idx],
      chunkId: chunkIds?.[idx] ?? null,
    };
  }, [hoveredGlobalIdx, timeline, chunkIds, clusterArr, minDate]);

  return (
    <div className="relative h-full w-full bg-stone-950" ref={containerRef}>
      {/* Column headers */}
      <div
        className="absolute top-0 left-0 right-0 z-10 flex pointer-events-none"
        style={{ paddingLeft: GUTTER_PX }}
      >
        {timeline.papers.map((p) => (
          <div
            key={p.lccn}
            className="flex-1 px-1 py-0.5 text-[9px] text-stone-500 truncate"
            title={p.title}
          >
            {p.title.replace(/\s*\(.*?\)\s*/g, "").trim()}
          </div>
        ))}
      </div>

      <canvas
        ref={canvasRef}
        onWheel={onWheel}
        onMouseDown={onMouseDown}
        onMouseMove={onMouseMove}
        onMouseLeave={onMouseLeave}
        onClick={onClick}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
        onTouchCancel={onTouchEnd}
        className="block cursor-crosshair select-none"
        style={{ touchAction: "none" }}
      />

      {tooltipPos && hoveredChunkInfo && (
        <div
          className="fixed z-50 pointer-events-none bg-stone-900 border border-stone-700 text-stone-200 text-xs rounded shadow-lg px-2 py-1"
          style={{
            left: tooltipPos.x + 12,
            top: tooltipPos.y + 12,
            maxWidth: 260,
          }}
        >
          <div className="font-mono text-[10px] text-stone-400">
            {hoveredChunkInfo.date}
          </div>
          <div className="break-words">{hoveredChunkInfo.paper}</div>
          <div className="text-stone-500 text-[10px]">
            cluster #{hoveredChunkInfo.cluster} · OCR{" "}
            {Math.round(hoveredChunkInfo.quality * 100)}%
          </div>
        </div>
      )}

      <div className="absolute bottom-1 left-1 text-[9px] text-stone-600 pointer-events-none">
        pinch zoom · drag pan · tap a bar
      </div>
    </div>
  );
}

// ---------------------- drawing primitives ----------------------

function drawChunk(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  chunkIdx: number,
  clusterLabel: number,
  quality: number,
  isHit: boolean,
) {
  const [r, g, b] = clusterColor(clusterLabel);
  const baseColor = `rgb(${r}, ${g}, ${b})`;
  const drawH = Math.max(1, h);

  // 15% solid prefix, 70% middle morse pattern, 15% solid suffix.
  const prefixEnd = x + w * 0.15;
  const suffixStart = x + w * 0.85;
  const middleW = suffixStart - prefixEnd;

  ctx.fillStyle = baseColor;
  ctx.fillRect(x, y, prefixEnd - x, drawH);
  ctx.fillRect(suffixStart, y, x + w - suffixStart, drawH);

  // Middle: Morse-code-ish pattern. More gaps = worse OCR.
  if (middleW > 0) {
    const segW = middleW / MORSE_SEGMENTS;
    for (let s = 0; s < MORSE_SEGMENTS; s++) {
      const segStart = prefixEnd + s * segW;
      const presenceRoll = noiseArr[(chunkIdx * 11 + s) % NOISE_LEN];
      if (presenceRoll >= quality) continue;
      // Dot vs dash
      const isDash = noiseArr[(chunkIdx * 13 + s) % NOISE_LEN] > 0.45;
      const segActualW = isDash ? segW * 0.85 : segW * 0.4;
      const offset = (segW - segActualW) / 2;
      ctx.fillRect(segStart + offset, y, segActualW, drawH);
    }
  }

  if (isHit) {
    ctx.fillStyle = "#FFA500";
    // Gold caps at the very ends — 10% wide each. They overlay the
    // cluster-colored solid prefix/suffix.
    const capW = Math.max(1.5, w * SEARCH_CAP_FRAC);
    ctx.fillRect(x, y, capW, drawH);
    ctx.fillRect(x + w - capW, y, capW, drawH);

    // Small inward-pointing arrows at the inner edges of the caps.
    if (h >= 2 && w >= 12) {
      const arrowH = Math.min(drawH, 6);
      const arrowYTop = y + (drawH - arrowH) / 2;
      const arrowYBot = arrowYTop + arrowH;
      const arrowYMid = (arrowYTop + arrowYBot) / 2;
      const arrowLen = Math.min(capW * 0.8, 3);
      // Right-pointing arrow at the end of the left cap
      ctx.beginPath();
      ctx.moveTo(x + capW, arrowYTop);
      ctx.lineTo(x + capW + arrowLen, arrowYMid);
      ctx.lineTo(x + capW, arrowYBot);
      ctx.closePath();
      ctx.fill();
      // Left-pointing arrow at the start of the right cap
      ctx.beginPath();
      ctx.moveTo(x + w - capW, arrowYTop);
      ctx.lineTo(x + w - capW - arrowLen, arrowYMid);
      ctx.lineTo(x + w - capW, arrowYBot);
      ctx.closePath();
      ctx.fill();
    }
  }
}
