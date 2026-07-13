"use client";

import { useMemo, useCallback } from "react";
import DeckGL from "@deck.gl/react";
import { ScatterplotLayer, TextLayer } from "@deck.gl/layers";
import { OrthographicView } from "@deck.gl/core";
import type { ExplorePoints } from "@/lib/explore-data";
import { clusterColor } from "@/lib/explore-data";

interface ExploreMapProps {
  points: ExplorePoints;
  tier: number;
  contentFilter: Set<number>;
  showOutliers: boolean;
  dateOffsets: Uint16Array | null;
  dateRange: [number, number] | null;
  selectedIndex: number | null;
  selectedLabel: string | null;
  focusedCluster: number | null;
  searchMatches: Set<number> | null;
  onPointClick: (index: number) => void;
}

const INITIAL_VIEW_STATE = {
  target: [0.5, 0.5, 0] as [number, number, number],
  zoom: 8,
  minZoom: 4,
  maxZoom: 18,
};

export function ExploreMap({
  points,
  tier,
  contentFilter,
  showOutliers,
  dateOffsets,
  dateRange,
  selectedIndex,
  selectedLabel,
  focusedCluster,
  searchMatches,
  onPointClick,
}: ExploreMapProps) {
  const clusterArrayForTier = useMemo(() => {
    switch (tier) {
      case 0: return points.clusterT0;
      case 1: return points.clusterT1;
      case 2: return points.clusterT2;
      case 3: return points.clusterT3;
      default: return points.clusterT2;
    }
  }, [points, tier]);

  const filteredIndices = useMemo(() => {
    const indices: number[] = [];
    for (let i = 0; i < points.count; i++) {
      if (!contentFilter.has(points.contentType[i])) continue;
      if (!showOutliers && clusterArrayForTier[i] < 0) continue;
      if (dateRange && dateOffsets) {
        const off = dateOffsets[i];
        if (off < dateRange[0] || off > dateRange[1]) continue;
      }
      indices.push(i);
    }
    return indices;
  }, [points, contentFilter, showOutliers, clusterArrayForTier, dateRange, dateOffsets]);

  const clusterMateIndices = useMemo(() => {
    const targetLabel =
      focusedCluster ??
      (selectedIndex !== null ? clusterArrayForTier[selectedIndex] : null);
    if (targetLabel === null || targetLabel < 0) return [];
    const mates: number[] = [];
    for (const i of filteredIndices) {
      if (i === selectedIndex) continue;
      if (clusterArrayForTier[i] === targetLabel) mates.push(i);
    }
    return mates;
  }, [selectedIndex, focusedCluster, clusterArrayForTier, filteredIndices]);

  const handleClick = useCallback(
    (info: { index: number }) => {
      if (info.index >= 0 && info.index < filteredIndices.length) {
        onPointClick(filteredIndices[info.index]);
      }
    },
    [onPointClick, filteredIndices]
  );

  const dotLayer = useMemo(
    () =>
      new ScatterplotLayer({
        id: "chunks",
        data: { length: filteredIndices.length },
        getPosition: (_: unknown, { index }: { index: number }) => {
          const i = filteredIndices[index];
          return [points.x[i], points.y[i], 0];
        },
        getFillColor: (_: unknown, { index }: { index: number }) => {
          const i = filteredIndices[index];
          const label = clusterArrayForTier[i];
          const [r, g, b] = clusterColor(label);
          const alpha = label < 0 ? 100 : 200;
          return [r, g, b, alpha];
        },
        getRadius: (_: unknown, { index }: { index: number }) => {
          const i = filteredIndices[index];
          return clusterArrayForTier[i] < 0 ? 0.5 : 1;
        },
        radiusMinPixels: 1.5,
        radiusMaxPixels: 8,
        pickable: true,
        onClick: handleClick,
        updateTriggers: {
          getPosition: [filteredIndices],
          getFillColor: [tier, filteredIndices],
          getRadius: [tier, filteredIndices],
        },
      }),
    [points, tier, filteredIndices, clusterArrayForTier, handleClick]
  );

  const matesLineColor: [number, number, number, number] =
    focusedCluster !== null ? [255, 255, 255, 220] : [255, 255, 255, 110];

  const matesLineWidth = focusedCluster !== null ? 1.2 : 0.5;
  const matesRadius = focusedCluster !== null ? 2 : 1.6;

  const clusterMatesLayer = useMemo(
    () =>
      new ScatterplotLayer({
        id: "cluster-mates",
        data: { length: clusterMateIndices.length },
        getPosition: (_: unknown, { index }: { index: number }) => {
          const i = clusterMateIndices[index];
          return [points.x[i], points.y[i], 0];
        },
        getFillColor: [0, 0, 0, 0],
        getLineColor: matesLineColor,
        getRadius: matesRadius,
        getLineWidth: matesLineWidth,
        stroked: true,
        filled: false,
        radiusMinPixels: 4,
        radiusMaxPixels: 9,
        lineWidthMinPixels: 1,
        lineWidthMaxPixels: 2,
        pickable: false,
        updateTriggers: {
          getPosition: [clusterMateIndices],
          getLineColor: [matesLineColor],
          getRadius: [matesRadius],
          getLineWidth: [matesLineWidth],
        },
      }),
    [points, clusterMateIndices, matesLineColor, matesLineWidth, matesRadius]
  );

  const searchMatchIndices = useMemo(() => {
    if (!searchMatches || searchMatches.size === 0) return [];
    const arr: number[] = [];
    for (const i of filteredIndices) {
      if (searchMatches.has(i)) arr.push(i);
    }
    return arr;
  }, [searchMatches, filteredIndices]);

  const searchLayer = useMemo(
    () =>
      new ScatterplotLayer({
        id: "search-matches",
        data: { length: searchMatchIndices.length },
        getPosition: (_: unknown, { index }: { index: number }) => {
          const i = searchMatchIndices[index];
          return [points.x[i], points.y[i], 0];
        },
        getFillColor: [0, 0, 0, 0],
        getLineColor: [255, 215, 0, 240],
        getRadius: 2.2,
        getLineWidth: 1.5,
        stroked: true,
        filled: false,
        radiusMinPixels: 5,
        radiusMaxPixels: 12,
        lineWidthMinPixels: 1.5,
        lineWidthMaxPixels: 2.5,
        pickable: false,
        updateTriggers: { getPosition: [searchMatchIndices] },
      }),
    [points, searchMatchIndices]
  );

  const highlightData = useMemo(() => {
    if (selectedIndex === null) return [];
    return [{
      position: [points.x[selectedIndex], points.y[selectedIndex]] as [number, number],
      label: selectedLabel ?? "",
    }];
  }, [selectedIndex, selectedLabel, points]);

  const selectedRingLayer = useMemo(
    () =>
      new ScatterplotLayer({
        id: "selected-highlight",
        data: highlightData,
        getPosition: (d) => [...d.position, 0] as [number, number, number],
        getFillColor: [0, 0, 0, 0],
        getLineColor: [255, 255, 255, 255],
        getRadius: 3,
        getLineWidth: 2,
        stroked: true,
        filled: false,
        radiusMinPixels: 11,
        radiusMaxPixels: 18,
        lineWidthMinPixels: 2.5,
        lineWidthMaxPixels: 4,
        pickable: false,
      }),
    [highlightData]
  );

  const labelLayer = useMemo(
    () =>
      new TextLayer({
        id: "selected-label",
        data: highlightData,
        getPosition: (d) => [...d.position, 0] as [number, number, number],
        getText: (d) => d.label,
        getSize: 13,
        getColor: [255, 255, 255, 255],
        getPixelOffset: [20, -2],
        getTextAnchor: "start",
        getAlignmentBaseline: "center",
        fontFamily: "ui-monospace, monospace",
        fontWeight: 500,
        background: true,
        backgroundPadding: [4, 2, 4, 2],
        getBackgroundColor: [20, 20, 20, 220],
        pickable: false,
      }),
    [highlightData]
  );

  const views = useMemo(
    () => new OrthographicView({ id: "ortho", flipY: true }),
    []
  );

  return (
    <DeckGL
      views={views}
      initialViewState={INITIAL_VIEW_STATE}
      controller={true}
      layers={[dotLayer, clusterMatesLayer, searchLayer, selectedRingLayer, labelLayer]}
      style={{ position: "absolute", inset: "0" }}
      getCursor={({ isDragging, isHovering }) =>
        isDragging ? "grabbing" : isHovering ? "pointer" : "grab"
      }
    />
  );
}
