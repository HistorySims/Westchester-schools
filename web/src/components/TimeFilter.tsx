"use client";

import { useMemo } from "react";

interface TimeFilterProps {
  minDate: string;
  range: [number, number];
  maxOffset: number;
  onChange: (range: [number, number]) => void;
}

const WIDTHS = [
  { value: 7, label: "1 wk" },
  { value: 14, label: "2 wk" },
  { value: 30, label: "1 mo" },
  { value: null, label: "All" },
];

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
  });
}

function offsetToDate(minDate: string, offset: number): string {
  const d = new Date(minDate);
  d.setUTCDate(d.getUTCDate() + offset);
  return d.toISOString().slice(0, 10);
}

export function TimeFilter({
  minDate,
  range,
  maxOffset,
  onChange,
}: TimeFilterProps) {
  const currentWidth = range[1] - range[0];
  const center = Math.round((range[0] + range[1]) / 2);

  const activeWidthBtn = useMemo(() => {
    if (currentWidth >= maxOffset) return null;
    for (const w of WIDTHS) {
      if (w.value !== null && Math.abs(currentWidth - w.value) <= 1) return w.value;
    }
    return undefined;
  }, [currentWidth, maxOffset]);

  const setWidth = (newWidth: number | null) => {
    if (newWidth === null) {
      onChange([0, maxOffset]);
      return;
    }
    const half = Math.floor(newWidth / 2);
    let start = center - half;
    let end = start + newWidth;
    if (start < 0) {
      start = 0;
      end = Math.min(maxOffset, newWidth);
    }
    if (end > maxOffset) {
      end = maxOffset;
      start = Math.max(0, end - newWidth);
    }
    onChange([start, end]);
  };

  const moveWindow = (newCenter: number) => {
    if (currentWidth >= maxOffset) {
      onChange([0, maxOffset]);
      return;
    }
    const half = Math.floor(currentWidth / 2);
    let start = newCenter - half;
    let end = start + currentWidth;
    if (start < 0) {
      start = 0;
      end = currentWidth;
    }
    if (end > maxOffset) {
      end = maxOffset;
      start = end - currentWidth;
    }
    onChange([start, end]);
  };

  const startDate = offsetToDate(minDate, range[0]);
  const endDate = offsetToDate(minDate, range[1]);
  const windowMode = currentWidth < maxOffset;

  return (
    <div>
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide">
          Date Window
        </h3>
        {windowMode && (
          <button
            onClick={() => setWidth(null)}
            className="text-xs text-stone-500 hover:text-stone-300 underline"
          >
            All
          </button>
        )}
      </div>

      <div className="text-xs text-stone-300 font-mono mb-2 text-center">
        {formatDate(startDate)} &mdash; {formatDate(endDate)}
      </div>

      <div className="flex rounded-md border border-stone-700 overflow-hidden mb-3">
        {WIDTHS.map((w) => (
          <button
            key={w.label}
            onClick={() => setWidth(w.value)}
            className={`flex-1 px-1 py-1 text-xs font-medium transition-colors ${
              activeWidthBtn === w.value
                ? "bg-amber-800 text-amber-50"
                : "bg-stone-900 text-stone-400 hover:bg-stone-800 hover:text-stone-200"
            }`}
          >
            {w.label}
          </button>
        ))}
      </div>

      {windowMode && (
        <div className="px-1">
          <label className="text-[10px] text-stone-500 uppercase">Slide window</label>
          <input
            type="range"
            min={0}
            max={maxOffset}
            value={center}
            onChange={(e) => moveWindow(parseInt(e.target.value, 10))}
            className="w-full accent-amber-600"
          />
        </div>
      )}
    </div>
  );
}
