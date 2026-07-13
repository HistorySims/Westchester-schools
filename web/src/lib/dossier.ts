// Types + small helpers for the Cluster Dossier page.

import type { PaperShare } from "./brief";

export interface DossierWeek {
  week_start: string;            // YYYY-MM-DD, Monday of the ISO week
  chunk_count: number;
  count_by_paper: Record<string, number>;  // lccn → count
  mean_ocr_quality: number | null;
  centroid_x: number | null;
  centroid_y: number | null;
  top_terms: string[];
}

export interface DossierChunk {
  chunk_id: string;
  date: string;                  // YYYY-MM-DD
  paper_lccn: string;
  paper_title: string;
  page_sequence: number;
  edition: number;
  excerpt: string;               // first ~40 words of OCR text
  quality: number;               // 0..1; 1 when unscored
  x: number;                     // UMAP coords (for the comet underlay)
  y: number;
  loc_url: string;
}

export interface DossierCluster {
  id: string;
  tier: number;
  label: number;
  label_text: string | null;     // refusals already stripped server-side
  size: number;
  active_size: number;
  burstiness: number | null;
  drift_net: number | null;
  drift_cumulative: number | null;
  drift_ratio: number | null;
  shape_tag: string;
  shape_explanation: string;
  date_min: string;
  date_max: string;
  papers: PaperShare[];
}

export interface DossierResponse {
  cluster: DossierCluster;
  weeks: DossierWeek[];
  chunks: DossierChunk[];
  // Paper display order (by contribution, descending). Color
  // assignment indexes into this array so every band and card uses
  // the same color per paper.
  papers: { lccn: string; title: string }[];
}

// One consistent color per paper across all dossier bands and cards.
// Index = position in DossierResponse.papers.
export const PAPER_COLORS = [
  "#f59e0b", // amber — dominant paper
  "#38bdf8", // sky
  "#4ade80", // green
  "#f472b6", // pink
  "#c084fc", // purple
] as const;

export function paperColor(index: number): string {
  return PAPER_COLORS[index % PAPER_COLORS.length];
}

// Opacity encoding for OCR quality (the fog-of-war convention).
// Clamped so content never becomes invisible or untappable.
export function qualityOpacity(q: number): number {
  return 0.45 + 0.55 * Math.max(0, Math.min(1, q));
}

export function shortPaperName(title: string): string {
  return title.replace(/\s*\(.*?\)\s*/g, "").trim();
}

// Monday of the ISO week for a YYYY-MM-DD string. Matches the Python
// pipeline's week_start convention.
export function isoWeekStartStr(dateStr: string): string {
  const d = new Date(dateStr + "T00:00:00Z");
  const day = d.getUTCDay() || 7;
  d.setUTCDate(d.getUTCDate() - (day - 1));
  return d.toISOString().slice(0, 10);
}

export function formatWeekLabel(weekStart: string): string {
  const d = new Date(weekStart + "T00:00:00Z");
  return (
    "Week of " +
    d.toLocaleDateString("en-US", {
      month: "short",
      day: "numeric",
      year: "numeric",
      timeZone: "UTC",
    })
  );
}
