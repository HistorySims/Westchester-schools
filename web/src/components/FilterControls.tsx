"use client";

import { useState } from "react";

interface FilterControlsProps {
  onFiltersChange: (filters: {
    paperLccn: string | null;
    dateFrom: string | null;
    dateTo: string | null;
  }) => void;
  disabled: boolean;
}

const PAPERS = [
  { lccn: null as string | null, label: "All papers" },
  { lccn: "sn83030213", label: "New-York Daily Tribune" },
  { lccn: "sn83030911", label: "Albany Evening Journal" },
];

export function FilterControls({ onFiltersChange, disabled }: FilterControlsProps) {
  const [expanded, setExpanded] = useState(false);
  const [paperLccn, setPaperLccn] = useState<string | null>(null);
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");

  const handleChange = (
    newPaper?: string | null,
    newFrom?: string,
    newTo?: string
  ) => {
    const p = newPaper !== undefined ? newPaper : paperLccn;
    const f = newFrom !== undefined ? newFrom : dateFrom;
    const t = newTo !== undefined ? newTo : dateTo;
    if (newPaper !== undefined) setPaperLccn(p);
    if (newFrom !== undefined) setDateFrom(f);
    if (newTo !== undefined) setDateTo(t);
    onFiltersChange({
      paperLccn: p,
      dateFrom: f || null,
      dateTo: t || null,
    });
  };

  if (!expanded) {
    const hasFilters = paperLccn || dateFrom || dateTo;
    return (
      <button
        onClick={() => setExpanded(true)}
        disabled={disabled}
        className={`text-xs px-2 py-1 rounded transition-colors
          ${hasFilters
            ? "text-amber-700 bg-amber-50 border border-amber-200"
            : "text-stone-400 hover:text-stone-600"
          }
          disabled:opacity-50`}
      >
        {hasFilters
          ? `Filtered: ${PAPERS.find((p) => p.lccn === paperLccn)?.label ?? ""}${dateFrom ? ` from ${dateFrom}` : ""}${dateTo ? ` to ${dateTo}` : ""}`
          : "Filters"}
      </button>
    );
  }

  return (
    <div className="border border-stone-200 rounded-lg p-3 space-y-2 bg-white">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-stone-600">Filters</span>
        <button
          onClick={() => setExpanded(false)}
          className="text-xs text-stone-400 hover:text-stone-600"
        >
          Close
        </button>
      </div>

      <div>
        <label className="block text-xs text-stone-500 mb-1">Paper</label>
        <select
          value={paperLccn ?? ""}
          onChange={(e) =>
            handleChange(e.target.value || null, undefined, undefined)
          }
          disabled={disabled}
          className="w-full text-sm border border-stone-200 rounded px-2 py-1.5
            text-stone-800 focus:outline-none focus:ring-1 focus:ring-amber-600
            disabled:opacity-50"
        >
          {PAPERS.map((p) => (
            <option key={p.lccn ?? "all"} value={p.lccn ?? ""}>
              {p.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex gap-2">
        <div className="flex-1">
          <label className="block text-xs text-stone-500 mb-1">From</label>
          <input
            type="date"
            value={dateFrom}
            onChange={(e) => handleChange(undefined, e.target.value, undefined)}
            min="1842-01-01"
            max="1846-12-31"
            disabled={disabled}
            className="w-full text-sm border border-stone-200 rounded px-2 py-1.5
              text-stone-800 focus:outline-none focus:ring-1 focus:ring-amber-600
              disabled:opacity-50"
          />
        </div>
        <div className="flex-1">
          <label className="block text-xs text-stone-500 mb-1">To</label>
          <input
            type="date"
            value={dateTo}
            onChange={(e) => handleChange(undefined, undefined, e.target.value)}
            min="1842-01-01"
            max="1846-12-31"
            disabled={disabled}
            className="w-full text-sm border border-stone-200 rounded px-2 py-1.5
              text-stone-800 focus:outline-none focus:ring-1 focus:ring-amber-600
              disabled:opacity-50"
          />
        </div>
      </div>

      {(paperLccn || dateFrom || dateTo) && (
        <button
          onClick={() => {
            setPaperLccn(null);
            setDateFrom("");
            setDateTo("");
            onFiltersChange({ paperLccn: null, dateFrom: null, dateTo: null });
          }}
          disabled={disabled}
          className="text-xs text-amber-700 hover:text-amber-800 disabled:opacity-50"
        >
          Clear filters
        </button>
      )}
    </div>
  );
}
