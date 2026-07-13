"use client";

import { useState, useEffect, useCallback, useMemo, useRef } from "react";
import { ExploreMap } from "@/components/ExploreMap";
import { ExploreSidebar } from "@/components/ExploreSidebar";
import { ChunkDetail } from "@/components/ChunkDetail";
import { TimeFilter } from "@/components/TimeFilter";
import { BurstyTopics } from "@/components/BurstyTopics";
import { ClusterStory } from "@/components/ClusterStory";
import { SearchBox } from "@/components/SearchBox";
import { TimelineMinimap } from "@/components/TimelineMinimap";
import {
  ExplorePoints,
  parsePointsBinary,
  parseDatesBinary,
  parseTimelineBinary,
  TimelineData,
  ChunkDetail as ChunkDetailType,
  ClusterInfo,
} from "@/lib/explore-data";
import { computeBurstyTopics, BurstyTopic } from "@/lib/burstiness";

interface DatesData {
  offsets: Uint16Array;
  maxOffset: number;
  minDate: string;
}

const DEFAULT_WINDOW = 14;

export default function ExplorePage() {
  const [points, setPoints] = useState<ExplorePoints | null>(null);
  const [chunkIds, setChunkIds] = useState<string[] | null>(null);
  const [dates, setDates] = useState<DatesData | null>(null);
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [showMinimap, setShowMinimap] = useState(true);
  const [mobileView, setMobileView] = useState<"map" | "timeline">("map");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tier, setTier] = useState(2);
  const [contentFilter, setContentFilter] = useState<Set<number>>(
    new Set([0])
  );
  const [showOutliers, setShowOutliers] = useState(false);
  const [dateRange, setDateRange] = useState<[number, number] | null>(null);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);
  const [selectedChunk, setSelectedChunk] = useState<ChunkDetailType | null>(
    null
  );
  const [loadingChunk, setLoadingChunk] = useState(false);
  const [focusedCluster, setFocusedCluster] = useState<number | null>(null);
  const [storyCluster, setStoryCluster] = useState<{ tier: number; label: number } | null>(null);
  const [clusterInfo, setClusterInfo] = useState<Map<number, ClusterInfo>>(
    new Map()
  );
  const [searchMatches, setSearchMatches] = useState<Set<number> | null>(null);
  const [searchLoading, setSearchLoading] = useState(false);
  const storyRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (storyCluster && storyRef.current) {
      storyRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  }, [storyCluster]);

  useEffect(() => {
    async function load() {
      try {
        const resp = await fetch("/api/explore/points");
        if (!resp.ok) {
          const err = await resp.json();
          throw new Error(err.error || `HTTP ${resp.status}`);
        }
        const buf = await resp.arrayBuffer();
        setPoints(parsePointsBinary(buf));
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  useEffect(() => {
    fetch("/api/explore/chunk-ids")
      .then((r) => r.json())
      .then((ids) => setChunkIds(ids))
      .catch(() => {});
  }, []);

  useEffect(() => {
    fetch(`/api/explore/clusters?tier=${tier}`)
      .then((r) => r.json())
      .then((data: ClusterInfo[]) => {
        const map = new Map<number, ClusterInfo>();
        for (const c of data) map.set(c.label, c);
        setClusterInfo(map);
      })
      .catch(() => setClusterInfo(new Map()));
  }, [tier]);

  useEffect(() => {
    async function loadDates() {
      try {
        const resp = await fetch("/api/explore/dates");
        if (!resp.ok) return;
        const minDate = resp.headers.get("X-Min-Date") ?? "1845-06-01";
        const buf = await resp.arrayBuffer();
        const parsed = parseDatesBinary(buf);
        setDates({
          offsets: parsed.offsets,
          maxOffset: parsed.maxOffset,
          minDate,
        });
        setDateRange([0, parsed.maxOffset]);
      } catch {
        // Time filter just won't appear
      }
    }
    loadDates();
  }, []);

  useEffect(() => {
    async function loadTimeline() {
      try {
        const resp = await fetch("/api/explore/timeline");
        if (!resp.ok) return;
        const buf = await resp.arrayBuffer();
        setTimeline(parseTimelineBinary(buf));
      } catch {
        // Minimap just won't appear
      }
    }
    loadTimeline();
  }, []);

  const handlePointClick = useCallback(
    async (index: number) => {
      if (!chunkIds || !chunkIds[index]) return;
      setSelectedIndex(index);
      setLoadingChunk(true);
      try {
        const resp = await fetch(`/api/explore/chunk?id=${chunkIds[index]}`);
        if (resp.ok) {
          setSelectedChunk(await resp.json());
        }
      } finally {
        setLoadingChunk(false);
      }
    },
    [chunkIds]
  );

  const selectedLabel = useMemo(() => {
    if (!selectedChunk) return null;
    const paperShort = selectedChunk.paper_title
      .replace(/\s*\(.*?\)\s*/g, "")
      .replace(/\b\d{4}-\d{4}\b/g, "")
      .trim();
    return `${selectedChunk.date_issued} · ${paperShort}`;
  }, [selectedChunk]);

  const burstyTopics = useMemo<BurstyTopic[]>(() => {
    if (!points || !dates) return [];
    return computeBurstyTopics(
      points,
      dates.offsets,
      tier,
      contentFilter,
      20,
      8
    );
  }, [points, dates, tier, contentFilter]);

  const handleSearch = useCallback(
    async (q: string) => {
      if (!chunkIds) return;
      setSearchLoading(true);
      try {
        const resp = await fetch(`/api/explore/search?q=${encodeURIComponent(q)}`);
        if (!resp.ok) {
          setSearchMatches(new Set());
          return;
        }
        const data = (await resp.json()) as { chunk_ids: string[] };
        const matchSet = new Set(data.chunk_ids);
        const indices = new Set<number>();
        for (let i = 0; i < chunkIds.length; i++) {
          if (matchSet.has(chunkIds[i])) indices.add(i);
        }
        setSearchMatches(indices);
      } catch {
        setSearchMatches(new Set());
      } finally {
        setSearchLoading(false);
      }
    },
    [chunkIds]
  );

  const handleClearSearch = useCallback(() => {
    setSearchMatches(null);
  }, []);

  const handleAskTopic = useCallback(
    (topic: BurstyTopic) => {
      setStoryCluster({ tier, label: topic.cluster });
    },
    [tier]
  );

  const handleAskCurrentChunkCluster = useCallback(() => {
    if (!points || selectedIndex === null) return;
    const clusterArr =
      tier === 0 ? points.clusterT0 :
      tier === 1 ? points.clusterT1 :
      tier === 2 ? points.clusterT2 :
      points.clusterT3;
    const clusterLabel = clusterArr[selectedIndex];
    if (clusterLabel < 0) return;
    setFocusedCluster(clusterLabel);
    setStoryCluster({ tier, label: clusterLabel });
  }, [points, selectedIndex, tier]);

  const handleTopicClick = useCallback(
    (topic: BurstyTopic) => {
      if (!dates) return;
      setFocusedCluster(topic.cluster);
      const width =
        dateRange && dateRange[1] - dateRange[0] < dates.maxOffset
          ? dateRange[1] - dateRange[0]
          : DEFAULT_WINDOW;
      const half = Math.floor(width / 2);
      let start = topic.peakDay - half;
      let end = start + width;
      if (start < 0) {
        start = 0;
        end = width;
      }
      if (end > dates.maxOffset) {
        end = dates.maxOffset;
        start = Math.max(0, end - width);
      }
      setDateRange([start, end]);
    },
    [dates, dateRange]
  );

  const stats = useMemo(() => {
    if (!points) return { total: 0, visible: 0, outliers: 0 };
    let outliers = 0;
    let visible = 0;
    const clusterArr =
      tier === 0
        ? points.clusterT0
        : tier === 1
        ? points.clusterT1
        : tier === 2
        ? points.clusterT2
        : points.clusterT3;
    for (let i = 0; i < points.count; i++) {
      const isOutlier = clusterArr[i] < 0;
      if (isOutlier) outliers++;
      if (!contentFilter.has(points.contentType[i])) continue;
      if (!showOutliers && isOutlier) continue;
      if (dateRange && dates) {
        const off = dates.offsets[i];
        if (off < dateRange[0] || off > dateRange[1]) continue;
      }
      visible++;
    }
    return { total: points.count, visible, outliers };
  }, [points, tier, contentFilter, showOutliers, dateRange, dates]);

  if (loading) {
    return (
      <div className="h-full flex items-center justify-center bg-stone-900">
        <div className="flex items-center gap-3 text-stone-400">
          <span className="inline-block w-5 h-5 border-2 border-stone-400 border-t-transparent rounded-full animate-spin" />
          Loading corpus map...
        </div>
      </div>
    );
  }

  if (error || !points) {
    return (
      <div className="h-full flex items-center justify-center bg-stone-900">
        <div className="text-center px-6">
          <p className="text-stone-400 text-sm mb-2">
            {error || "No cluster data available"}
          </p>
          <a
            href="/"
            className="text-amber-500 hover:text-amber-400 text-sm underline"
          >
            Back to search
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full flex flex-col md:flex-row bg-stone-900">
      {/* Mobile-only view switcher — desktop shows everything at once */}
      {timeline && (
        <div className="md:hidden flex border-b border-stone-800 bg-stone-900">
          <button
            onClick={() => setMobileView("map")}
            className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
              mobileView === "map"
                ? "bg-stone-800 text-stone-100 border-b-2 border-amber-600"
                : "text-stone-500"
            }`}
          >
            Cluster Map
          </button>
          <button
            onClick={() => setMobileView("timeline")}
            className={`flex-1 px-3 py-2 text-xs font-medium transition-colors ${
              mobileView === "timeline"
                ? "bg-stone-800 text-stone-100 border-b-2 border-amber-600"
                : "text-stone-500"
            }`}
          >
            Timeline
          </button>
        </div>
      )}
      <div
        className={`flex-1 relative min-h-[50vh] md:min-h-0 ${
          mobileView === "timeline" ? "hidden md:block" : ""
        }`}
      >
        <ExploreMap
          points={points}
          tier={tier}
          contentFilter={contentFilter}
          showOutliers={showOutliers}
          dateOffsets={dates?.offsets ?? null}
          dateRange={dateRange}
          selectedIndex={selectedIndex}
          selectedLabel={selectedLabel}
          focusedCluster={focusedCluster}
          searchMatches={searchMatches}
          onPointClick={handlePointClick}
        />
        <div className="absolute top-3 left-3 flex gap-1">
          <a
            href="/"
            className="text-xs text-stone-400 hover:text-stone-200 bg-stone-800/80 px-2 py-1 rounded"
          >
            Chat
          </a>
          <a
            href="/brief"
            className="text-xs text-stone-400 hover:text-stone-200 bg-stone-800/80 px-2 py-1 rounded"
          >
            Brief
          </a>
        </div>
        <div className="absolute top-3 right-3 text-xs text-stone-500 bg-stone-800/80 px-2 py-1 rounded">
          {stats.visible.toLocaleString()} / {stats.total.toLocaleString()} chunks
        </div>
        <div className="absolute bottom-3 left-3 text-xs text-stone-500 bg-stone-800/80 px-2 py-1 rounded">
          Pinch / scroll to zoom · drag to pan · tap a dot
        </div>
        {timeline && (
          <button
            onClick={() => setShowMinimap((v) => !v)}
            className="hidden md:block absolute bottom-3 right-3 text-xs text-stone-400 hover:text-stone-200 bg-stone-800/80 px-2 py-1 rounded"
            title="Toggle the chronological minimap on the right"
          >
            {showMinimap ? "Hide timeline" : "Show timeline"}
          </button>
        )}
      </div>

      {timeline && (showMinimap || mobileView === "timeline") && (
        <div
          className={`border-stone-700 bg-stone-950 ${
            mobileView === "timeline"
              ? "flex-1 min-h-[50vh] md:hidden"
              : "hidden md:block md:w-[140px] lg:w-[180px] md:border-l"
          }`}
        >
          <TimelineMinimap
            timeline={timeline}
            chunkIds={chunkIds}
            tier={tier}
            searchMatches={searchMatches}
            contentFilter={contentFilter}
            minDate={dates?.minDate ?? "1845-06-01"}
            onChunkClick={handlePointClick}
          />
        </div>
      )}

      <div className="w-full md:w-72 lg:w-80 border-t md:border-t-0 md:border-l border-stone-700 bg-stone-900 overflow-y-auto">
        <ExploreSidebar
          tier={tier}
          onTierChange={(t) => {
            setTier(t);
            setFocusedCluster(null);
          }}
          contentFilter={contentFilter}
          onContentFilterChange={setContentFilter}
          showOutliers={showOutliers}
          onShowOutliersChange={setShowOutliers}
          outlierCount={stats.outliers}
          totalCount={stats.total}
          visibleCount={stats.visible}
        />
        {dates && dateRange && (
          <div className="px-4 pb-4">
            <TimeFilter
              minDate={dates.minDate}
              range={dateRange}
              maxOffset={dates.maxOffset}
              onChange={setDateRange}
            />
          </div>
        )}
        <div className="px-4 pb-4">
          <SearchBox
            matchCount={searchMatches?.size ?? null}
            loading={searchLoading}
            onSearch={handleSearch}
            onClear={handleClearSearch}
          />
        </div>
        {dates && burstyTopics.length > 0 && (
          <div className="px-4 pb-4">
            <BurstyTopics
              topics={burstyTopics}
              minDate={dates.minDate}
              focusedCluster={focusedCluster}
              clusterLabels={
                new Map(
                  Array.from(clusterInfo.entries()).map(([k, v]) => [
                    k,
                    v.label_text,
                  ])
                )
              }
              onTopicClick={handleTopicClick}
              onAskClick={handleAskTopic}
            />
          </div>
        )}
        {storyCluster && (
          <div ref={storyRef}>
            <ClusterStory
              tier={storyCluster.tier}
              label={storyCluster.label}
              onClose={() => setStoryCluster(null)}
            />
          </div>
        )}
        {(selectedChunk || loadingChunk) && (
          <ChunkDetail
            chunk={selectedChunk}
            loading={loadingChunk}
            tier={tier}
            clusterLabel={
              selectedChunk && selectedChunk.cluster_labels.length > tier
                ? clusterInfo.get(selectedChunk.cluster_labels[tier])
                    ?.label_text ?? null
                : null
            }
            dossierHref={
              selectedChunk && selectedChunk.cluster_labels.length > tier
                ? (() => {
                    const info = clusterInfo.get(
                      selectedChunk.cluster_labels[tier]
                    );
                    return info?.id ? `/cluster/${info.id}` : null;
                  })()
                : null
            }
            onAskClusterStory={handleAskCurrentChunkCluster}
            onClose={() => {
              setSelectedChunk(null);
              setSelectedIndex(null);
            }}
          />
        )}
      </div>
    </div>
  );
}
