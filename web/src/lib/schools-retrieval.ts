// Panel retrieval over the schools corpus — the TS port of
// herald.schools_retrieval. Both legs rank *per district* (in the RPCs), RRF
// fuses them within each district, an optional Voyage rerank sharpens the
// pool, and a per-document cap spreads the slate across sources. Districts
// that produced nothing are returned explicitly — absence is part of the
// answer.

import { getSupabase } from "./supabase";
import { embedQuery, rerank } from "./voyage";

const RRF_K = 60;
export const DEFAULT_POOL = 12; // candidates per district per leg
export const DEFAULT_PER_DISTRICT = 4; // evidence chunks per district, final
export const DEFAULT_MAX_PER_DOC = 2; // cap chunks from any one document

export interface EvidenceChunk {
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  section_path: string;
  heading: string | null;
  content: string;
  source_url: string;
  score: number;
  rerank_score?: number;
}

export interface Panel {
  question: string;
  by_district: Record<string, EvidenceChunk[]>;
  empty_districts: string[];
}

export interface RetrieveOptions {
  districts?: string[] | null;
  docType?: string | null;
  dateFrom?: string | null;
  dateTo?: string | null;
  perDistrict?: number;
  pool?: number;
  maxPerDoc?: number;
}

interface LegRow {
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  section_path: string;
  heading: string | null;
  content: string;
  source_url: string;
  score: number;
}

function toChunk(r: LegRow): EvidenceChunk {
  return { ...r, score: 0 };
}

async function panelSemantic(
  queryEmbedding: number[],
  pool: number,
  o: RetrieveOptions
): Promise<EvidenceChunk[]> {
  const { data, error } = await getSupabase().rpc("match_school_chunks_semantic", {
    query_embedding: JSON.stringify(queryEmbedding),
    per_district: pool,
    filter_districts: o.districts ?? null,
    filter_doc_type: o.docType ?? null,
    filter_date_from: o.dateFrom ?? null,
    filter_date_to: o.dateTo ?? null,
  });
  if (error) throw new Error(`Semantic search failed: ${error.message}`);
  return ((data ?? []) as LegRow[]).map(toChunk);
}

async function panelFts(
  question: string,
  pool: number,
  o: RetrieveOptions
): Promise<EvidenceChunk[]> {
  const { data, error } = await getSupabase().rpc("match_school_chunks_fts", {
    query: question,
    per_district: pool,
    filter_districts: o.districts ?? null,
    filter_doc_type: o.docType ?? null,
    filter_date_from: o.dateFrom ?? null,
    filter_date_to: o.dateTo ?? null,
  });
  if (error) throw new Error(`FTS search failed: ${error.message}`);
  return ((data ?? []) as LegRow[]).map(toChunk);
}

// RRF within each district: each leg arrives grouped per district in rank
// order (the SQL contract), so a chunk's rank is its position in its
// district's list. A chunk in both legs sums both terms.
function rrfFusePerDistrict(
  semantic: EvidenceChunk[],
  fts: EvidenceChunk[],
  keep: number
): Record<string, EvidenceChunk[]> {
  const scores = new Map<string, number>();
  const best = new Map<string, EvidenceChunk>();

  for (const leg of [semantic, fts]) {
    const perDistrictRank = new Map<string, number>();
    for (const c of leg) {
      const rank = (perDistrictRank.get(c.district) ?? 0) + 1;
      perDistrictRank.set(c.district, rank);
      scores.set(c.chunk_id, (scores.get(c.chunk_id) ?? 0) + 1 / (RRF_K + rank));
      if (!best.has(c.chunk_id)) best.set(c.chunk_id, c);
    }
  }

  const byDistrict: Record<string, EvidenceChunk[]> = {};
  for (const [cid, chunk] of best) {
    chunk.score = scores.get(cid) ?? 0;
    (byDistrict[chunk.district] ??= []).push(chunk);
  }
  for (const slug of Object.keys(byDistrict)) {
    byDistrict[slug] = byDistrict[slug]
      .sort((a, b) => b.score - a.score)
      .slice(0, keep);
  }
  return byDistrict;
}

// Up to `limit` chunks in rank order, at most `maxPerDoc` per document
// (keyed by source_url); backfill overflow if diverse docs run out.
function capPerDocument(
  rows: EvidenceChunk[],
  limit: number,
  maxPerDoc: number
): EvidenceChunk[] {
  const kept: EvidenceChunk[] = [];
  const overflow: EvidenceChunk[] = [];
  const perDoc = new Map<string, number>();
  for (const c of rows) {
    const n = perDoc.get(c.source_url) ?? 0;
    if (n < maxPerDoc) {
      perDoc.set(c.source_url, n + 1);
      kept.push(c);
      if (kept.length >= limit) return kept;
    } else {
      overflow.push(c);
    }
  }
  return kept.concat(overflow).slice(0, limit);
}

async function listDistrictSlugs(): Promise<string[]> {
  const { data, error } = await getSupabase()
    .from("districts")
    .select("slug")
    .order("slug");
  if (error) throw new Error(`District list failed: ${error.message}`);
  return (data ?? []).map((r: { slug: string }) => r.slug);
}

export async function retrievePanel(
  question: string,
  options: RetrieveOptions = {}
): Promise<Panel> {
  const pool = options.pool ?? DEFAULT_POOL;
  const perDistrict = options.perDistrict ?? DEFAULT_PER_DISTRICT;
  const maxPerDoc = options.maxPerDoc ?? DEFAULT_MAX_PER_DOC;

  const queryEmbedding = await embedQuery(question);
  const [sem, fts] = await Promise.all([
    panelSemantic(queryEmbedding, pool, options),
    panelFts(question, pool, options),
  ]);

  let fused = rrfFusePerDistrict(sem, fts, pool);

  // One pooled rerank call across every district's candidates.
  const pooled = Object.values(fused).flat();
  if (pooled.length > 0) {
    const results = await rerank(question, pooled.map((c) => c.content), pooled.length);
    for (const r of results) pooled[r.index].rerank_score = r.relevance_score;
    for (const slug of Object.keys(fused)) {
      fused[slug] = fused[slug].sort(
        (a, b) => (b.rerank_score ?? 0) - (a.rerank_score ?? 0)
      );
    }
  }

  const byDistrict: Record<string, EvidenceChunk[]> = {};
  for (const [slug, rows] of Object.entries(fused)) {
    if (rows.length) byDistrict[slug] = capPerDocument(rows, perDistrict, maxPerDoc);
  }

  let known = await listDistrictSlugs();
  if (options.districts && options.districts.length) {
    known = known.filter((s) => options.districts!.includes(s));
  }
  const empty = known.filter((s) => !(s in byDistrict));

  return { question, by_district: byDistrict, empty_districts: empty };
}

export interface SearchHit {
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  section_path: string;
  heading: string | null;
  content: string;
  source_url: string;
  rank: number;
}

export async function searchChunks(
  query: string,
  matchCount = 60,
  districts: string[] | null = null
): Promise<SearchHit[]> {
  const { data, error } = await getSupabase().rpc("search_school_chunks", {
    query,
    match_count: matchCount,
    filter_districts: districts,
  });
  if (error) throw new Error(`Search failed: ${error.message}`);
  return (data ?? []) as SearchHit[];
}
