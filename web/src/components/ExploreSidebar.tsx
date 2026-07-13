"use client";

import { contentTypeLabel } from "@/lib/explore-data";

interface ExploreSidebarProps {
  tier: number;
  onTierChange: (tier: number) => void;
  contentFilter: Set<number>;
  onContentFilterChange: (filter: Set<number>) => void;
  showOutliers: boolean;
  onShowOutliersChange: (show: boolean) => void;
  outlierCount?: number;
  totalCount: number;
  visibleCount: number;
}

const TIERS = [
  { value: 0, label: "Fine", desc: "Natural HDBSCAN clusters" },
  { value: 1, label: "Medium", desc: "~50 topic groups" },
  { value: 2, label: "Broad", desc: "~15 major themes" },
  { value: 3, label: "Macro", desc: "~5 broad categories" },
];

const CONTENT_TYPES = [0, 1, 2, 3];

export function ExploreSidebar({
  tier,
  onTierChange,
  contentFilter,
  onContentFilterChange,
  showOutliers,
  onShowOutliersChange,
  outlierCount,
  totalCount,
  visibleCount,
}: ExploreSidebarProps) {
  const toggleContentType = (t: number) => {
    const next = new Set(contentFilter);
    if (next.has(t)) {
      next.delete(t);
    } else {
      next.add(t);
    }
    onContentFilterChange(next);
  };

  return (
    <div className="p-4 space-y-5">
      <div>
        <h2 className="text-sm font-semibold text-stone-300 mb-1">
          Explore the Corpus
        </h2>
        <p className="text-xs text-stone-500 mb-2">
          Each dot is a chunk of newspaper text. Color = cluster.
        </p>
        <p className="text-xs text-amber-500/90">
          Tap a dot to see the chunk text & source.
        </p>
        <p className="text-xs text-stone-500 mt-1">
          Showing {visibleCount.toLocaleString()} of {totalCount.toLocaleString()}.
        </p>
      </div>

      <div>
        <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide mb-2">
          Cluster Level
        </h3>
        <div className="space-y-1">
          {TIERS.map((t) => (
            <button
              key={t.value}
              onClick={() => onTierChange(t.value)}
              className={`w-full text-left px-3 py-1.5 rounded text-sm transition-colors ${
                tier === t.value
                  ? "bg-amber-800 text-amber-50"
                  : "text-stone-400 hover:bg-stone-800 hover:text-stone-200"
              }`}
            >
              <span className="font-medium">{t.label}</span>
              <span className="text-xs ml-2 opacity-70">{t.desc}</span>
            </button>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide mb-2">
          Content Filter
        </h3>
        <div className="space-y-1">
          {CONTENT_TYPES.map((t) => (
            <label
              key={t}
              className="flex items-center gap-2 px-3 py-1 cursor-pointer text-sm text-stone-400 hover:text-stone-200"
            >
              <input
                type="checkbox"
                checked={contentFilter.has(t)}
                onChange={() => toggleContentType(t)}
                className="rounded border-stone-600 bg-stone-800 text-amber-600 focus:ring-amber-600"
              />
              {contentTypeLabel(t)}
            </label>
          ))}
        </div>
      </div>

      <div>
        <h3 className="text-xs font-medium text-stone-400 uppercase tracking-wide mb-2">
          Outliers
        </h3>
        <label className="flex items-center gap-2 px-3 py-1 cursor-pointer text-sm text-stone-400 hover:text-stone-200">
          <input
            type="checkbox"
            checked={showOutliers}
            onChange={() => onShowOutliersChange(!showOutliers)}
            className="rounded border-stone-600 bg-stone-800 text-amber-600 focus:ring-amber-600"
          />
          Show outliers (gray)
          {outlierCount !== undefined && (
            <span className="text-xs text-stone-500 ml-1">
              ({outlierCount.toLocaleString()})
            </span>
          )}
        </label>
        <p className="text-xs text-stone-500 px-3 mt-1">
          Chunks that didn&apos;t fit any cluster — often junk OCR.
        </p>
      </div>
    </div>
  );
}
