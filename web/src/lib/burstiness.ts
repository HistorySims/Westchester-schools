import type { ExplorePoints } from "./explore-data";

export interface BurstyTopic {
  cluster: number;
  size: number;
  burstiness: number;
  peakDay: number;
  peakCount: number;
  activeDays: number;
}

export function clusterArrayForTier(
  points: ExplorePoints,
  tier: number
): Int16Array {
  switch (tier) {
    case 0: return points.clusterT0;
    case 1: return points.clusterT1;
    case 2: return points.clusterT2;
    case 3: return points.clusterT3;
    default: return points.clusterT2;
  }
}

export function computeBurstyTopics(
  points: ExplorePoints,
  dateOffsets: Uint16Array,
  tier: number,
  contentFilter: Set<number>,
  minClusterSize: number = 20,
  limit: number = 8
): BurstyTopic[] {
  const clusterArr = clusterArrayForTier(points, tier);

  const counts = new Map<number, Map<number, number>>();
  for (let i = 0; i < points.count; i++) {
    if (!contentFilter.has(points.contentType[i])) continue;
    const cluster = clusterArr[i];
    if (cluster < 0) continue;
    const day = dateOffsets[i];
    let dayMap = counts.get(cluster);
    if (!dayMap) {
      dayMap = new Map();
      counts.set(cluster, dayMap);
    }
    dayMap.set(day, (dayMap.get(day) ?? 0) + 1);
  }

  const topics: BurstyTopic[] = [];
  for (const [cluster, dayMap] of counts) {
    const dayCounts = Array.from(dayMap.values());
    const total = dayCounts.reduce((a, b) => a + b, 0);
    if (total < minClusterSize) continue;

    const mean = total / dayCounts.length;
    const variance =
      dayCounts.reduce((sum, c) => sum + (c - mean) ** 2, 0) /
      dayCounts.length;
    const std = Math.sqrt(variance);
    const cv = mean > 0 ? std / mean : 0;

    let peakDay = 0;
    let peakCount = 0;
    for (const [day, count] of dayMap) {
      if (count > peakCount) {
        peakCount = count;
        peakDay = day;
      }
    }

    topics.push({
      cluster,
      size: total,
      burstiness: cv,
      peakDay,
      peakCount,
      activeDays: dayMap.size,
    });
  }

  topics.sort((a, b) => b.burstiness - a.burstiness);
  return topics.slice(0, limit);
}

export function pickRepresentativeChunks(
  points: ExplorePoints,
  dateOffsets: Uint16Array,
  chunkIds: string[],
  tier: number,
  cluster: number,
  contentFilter: Set<number>,
  n: number = 12
): string[] {
  const clusterArr = clusterArrayForTier(points, tier);
  const candidates: { id: string; day: number }[] = [];
  for (let i = 0; i < points.count; i++) {
    if (clusterArr[i] !== cluster) continue;
    if (!contentFilter.has(points.contentType[i])) continue;
    if (!chunkIds[i]) continue;
    candidates.push({ id: chunkIds[i], day: dateOffsets[i] });
  }

  if (candidates.length === 0) return [];
  if (candidates.length <= n) return candidates.map((c) => c.id);

  candidates.sort((a, b) => a.day - b.day);
  const step = candidates.length / n;
  const picked: string[] = [];
  for (let k = 0; k < n; k++) {
    const idx = Math.min(Math.floor(k * step), candidates.length - 1);
    picked.push(candidates[idx].id);
  }
  return Array.from(new Set(picked));
}
