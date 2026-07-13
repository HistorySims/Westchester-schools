// Types, tuning constants, and helpers for the Research Brief feature.
//
// Phase 1 scope: question intake + translation, cluster matching by
// centroid+FTS, geometry cards from data we already store. Out of
// scope: quarantine mining, entity extraction, c-TF-IDF terms,
// proximity queries, fragment mining.

export interface TranslationOutput {
  period_terms: string[];
  likely_entities: string[];
  candidate_date_ranges: string[];
  search_phrases: string[];
  restated_question: string;
}

export interface ParentEntry {
  tier: number;
  label: number;
  label_text: string | null;
}

export interface PaperShare {
  lccn: string;
  title: string;
  count: number;
  share: number;
}

export interface WeeklyCount {
  // ISO week start (YYYY-MM-DD, Monday of the ISO week)
  week: string;
  count: number;
}

export interface ClusterCard {
  cluster_id: string;        // clusters.id uuid — dossier route param
  tier: number;
  label: number;
  label_text: string | null;
  size: number;            // stored cluster size (all member chunks)
  active_size: number;     // active + content_type=0 contributing to the card
  date_min: string;
  date_max: string;
  peak_week: string | null;
  peak_count: number;
  burstiness: number;      // CV of weekly counts; null-safe → 0
  drift_net: number | null;
  drift_cumulative: number | null;
  drift_ratio: number | null;
  weeks: number;
  papers: PaperShare[];
  weekly_counts: WeeklyCount[];
  shape_tag: string;
  shape_explanation: string;
  parent_chain: ParentEntry[];
  relevance: number;       // 0..1, weighted blend below
  semantic_sim: number;    // 0..1
  fts_hits: number;        // raw count of FTS matches that fell in this cluster
}

export interface BriefResponse {
  translation: TranslationOutput;
  orientation: string;
  cards: ClusterCard[];
  next_queries: string[];
  confidence_low: boolean;
  confidence_message: string | null;
  generated_at: string;
}

// -------- Tuning constants (Phase 1 defaults; tweak as we learn) ---------

export const TOP_N_FINE = 8;
export const SEMANTIC_WEIGHT = 0.6;
export const FTS_WEIGHT = 0.4;

// Hard minimum below which clusters are dropped from the brief entirely
// — a finding aid that surfaces noise has failed at its job. Cluster
// at relevance < this never appears as a card. Above MIN_RELEVANCE but
// below WEAK_RELEVANCE we still surface, but the orientation must
// flag low confidence.
export const MIN_RELEVANCE_THRESHOLD = 0.55;
export const WEAK_RELEVANCE_THRESHOLD = 0.65;

// Shape thresholds — PERCENTILE-based against the corpus distribution
// of each metric over eligible (active_size > 0) fine clusters. A
// cluster in the top decile of burstiness should never read as
// "moderate". Absolute thresholds had that exact failure mode.
export const BURSTINESS_P_HIGH = 0.85;
export const BURSTINESS_P_LOW = 0.25;
export const RATIO_P_HIGH = 0.70;
export const CUM_P_HIGH = 0.70;

// Span / duration thresholds for shape tagging.
// HEARTBEAT_MIN_WEEK_FRACTION: a "heartbeat" cluster must be present
// across at least this fraction of corpus weeks. Short-span low-burst
// clusters get the "Brief mention" tag instead.
export const HEARTBEAT_MIN_WEEK_FRACTION = 0.4;
export const SHORT_SPAN_WEEKS = 3;

// Cap how many phrases we feed FTS/embed (token + latency budget).
export const MAX_FTS_PHRASES = 8;
export const MAX_EMBED_PHRASES = 6;

// Refusal / unreadable label detection. Haiku occasionally returns
// "I cannot reliably identify..." instead of a clean topic label for
// OCR-garbage clusters. Those strings leaked into the breadcrumb UI.
// Drop them at the API boundary AND defensively in the UI.
const REFUSAL_PATTERNS: RegExp[] = [
  /^i\s+cannot\b/i,
  /^i'?m\s+unable\b/i,
  /^unable\s+to\b/i,
  /\bocr[- ]?(damaged|corrupted|errors?)\b/i,
  /\bseverely\s+corrupted\b/i,
  /\bcannot\s+reliably\b/i,
  /\bunintelligible\b/i,
  /\bno\s+(clear|shared|coherent)\b/i,
  /^unclear\b/i,
  /\bdo\s+not\s+contain\b/i,
  /\bappears?\s+to\s+be\b.*\b(corrupted|damaged|garbled)\b/i,
];

export function isRefusalLabel(label: string | null | undefined): boolean {
  if (!label) return true;
  const t = label.trim();
  if (t.length === 0) return true;
  if (t.length > 200) return true; // refusals tend to be paragraphs
  return REFUSAL_PATTERNS.some((p) => p.test(t));
}

// -------- Helpers ---------------------------------------------------------

export function isoWeekStart(d: Date): string {
  // Monday of the ISO week as YYYY-MM-DD.
  const day = d.getUTCDay() || 7; // Sun=0 → 7
  const monday = new Date(d);
  monday.setUTCDate(d.getUTCDate() - (day - 1));
  return monday.toISOString().slice(0, 10);
}

export interface ShapeInputs {
  burstiness_pct: number;     // 0..1 percentile rank in corpus
  ratio_pct: number;          // 0..1
  cum_pct: number;            // 0..1
  weeks: number;              // ISO weeks with ≥1 active chunk
  corpus_weeks: number;       // total ISO weeks spanned by corpus
}

export function deriveShapeTag(
  inputs: ShapeInputs,
): { tag: string; explanation: string } {
  const { burstiness_pct, ratio_pct, cum_pct, weeks, corpus_weeks } = inputs;
  const week_fraction = corpus_weeks > 0 ? weeks / corpus_weeks : 0;
  const spans_most_of_corpus = week_fraction >= HEARTBEAT_MIN_WEEK_FRACTION;
  const short_span = weeks <= SHORT_SPAN_WEEKS;

  // High burst, top of corpus distribution.
  if (burstiness_pct >= BURSTINESS_P_HIGH) {
    if (ratio_pct >= RATIO_P_HIGH) {
      return {
        tag: "Directional evolving story",
        explanation:
          "Coverage spikes and the centroid moves in a coherent direction over time — framing or focus is shifting.",
      };
    }
    if (short_span) {
      return {
        tag: "Spike-and-decay",
        explanation:
          "One concentrated burst, then drops off. Single event, single framing.",
      };
    }
    return {
      tag: "High-burst event",
      explanation:
        "Concentrated, event-driven coverage — bursty even by corpus standards but spans more than a few weeks.",
    };
  }

  // Low burst — distinguish duration. A "heartbeat" must span most of
  // the corpus; otherwise it's a brief mention that happened to be flat.
  if (burstiness_pct < BURSTINESS_P_LOW) {
    if (spans_most_of_corpus) {
      return {
        tag: "Heartbeat",
        explanation:
          "Steady background coverage spanning most of the corpus — recurring content with no bursts.",
      };
    }
    return {
      tag: "Brief mention",
      explanation:
        "Short, low-volume appearance — a few items concentrated in a small window, then gone.",
    };
  }

  // Churn: a recurring slot with high week-to-week variance but no
  // net displacement (e.g. police court, market reports).
  if (cum_pct >= CUM_P_HIGH && ratio_pct < BURSTINESS_P_LOW) {
    return {
      tag: "Churn",
      explanation:
        "High week-to-week variance with no net displacement — a recurring slot whose specific contents rotate.",
    };
  }

  return {
    tag: "Topical thread",
    explanation:
      "Moderate burst, moderate movement — a recurring topic that develops gradually.",
  };
}

// Percentile rank of value `v` in a sorted-ascending distribution
// `dist`. Returns the fraction of values strictly less than v, so
// the max value gets ≈1 and the min ≈0. Stable on duplicate values.
export function percentileRank(v: number, dist_sorted: number[]): number {
  if (dist_sorted.length === 0) return 0;
  let lo = 0;
  let hi = dist_sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (dist_sorted[mid] < v) lo = mid + 1;
    else hi = mid;
  }
  return lo / dist_sorted.length;
}

export function combineRelevance(
  semantic_sim_norm: number,
  fts_hits_norm: number,
): number {
  return SEMANTIC_WEIGHT * semantic_sim_norm + FTS_WEIGHT * fts_hits_norm;
}

// Cosine SIMILARITY (not distance). Defensive on length mismatch.
export function cosineSimilarity(a: number[], b: number[]): number {
  if (a.length !== b.length) return 0;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  if (denom === 0) return 0;
  return dot / denom;
}

export function meanVector(vecs: number[][]): number[] {
  if (vecs.length === 0) return [];
  const dim = vecs[0].length;
  const out = new Array<number>(dim).fill(0);
  for (const v of vecs) {
    for (let i = 0; i < dim; i++) out[i] += v[i];
  }
  for (let i = 0; i < dim; i++) out[i] /= vecs.length;
  return out;
}

export function parseCentroid(c: number[] | string | null): number[] | null {
  if (Array.isArray(c)) return c;
  if (typeof c !== "string") return null;
  try {
    const trimmed = c.trim();
    if (trimmed.startsWith("[")) return JSON.parse(trimmed) as number[];
    return null;
  } catch {
    return null;
  }
}

// Coefficient-of-variation of weekly counts (same shape as the
// explore burstiness metric).
export function burstinessFromCounts(counts: number[]): number {
  if (counts.length === 0) return 0;
  const total = counts.reduce((a, b) => a + b, 0);
  if (total === 0) return 0;
  const mean = total / counts.length;
  const variance =
    counts.reduce((s, c) => s + (c - mean) ** 2, 0) / counts.length;
  const std = Math.sqrt(variance);
  return mean > 0 ? std / mean : 0;
}
