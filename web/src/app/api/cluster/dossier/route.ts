// GET /api/cluster/dossier?id=<cluster uuid>
//
// Single payload for the Cluster Dossier page: cluster metadata
// (label, metrics, shape tag, papers), the cluster_weeks rows, and
// the full active chunk list. Clusters are small (n≈15-90 active),
// so no pagination — one response.
//
// All data from status='active' AND content_type=0 chunks, consistent
// with the quarantine. Zero-active clusters return an empty chunks
// array — the page renders an empty state, never a broken page.

import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  deriveShapeTag,
  isRefusalLabel,
  percentileRank,
} from "@/lib/brief";
import { isoWeekStartStr } from "@/lib/dossier";
import type {
  DossierChunk,
  DossierResponse,
  DossierWeek,
} from "@/lib/dossier";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

const EXCERPT_WORDS = 40;

interface ClusterDbRow {
  id: string;
  run_id: string;
  tier: number;
  label: number;
  size: number;
  label_text: string | null;
  date_min: string | null;
  date_max: string | null;
  drift_cumulative: number | null;
  drift_net: number | null;
  drift_weeks: number | null;
  active_size: number | null;
  burstiness: number | null;
  active_date_min: string | null;
  active_date_max: string | null;
}

interface MemberRow {
  chunk_id: string;
  x: number;
  y: number;
  chunks: {
    status: string | null;
    content: string;
    quality_score: number | null;
    pages: {
      sequence: number;
      image_url: string;
      issues: {
        date_issued: string;
        edition: number;
        papers: { lccn: string; title: string } | null;
      } | null;
    } | null;
  } | null;
}

interface WeekDbRow {
  week_start: string;
  chunk_count: number;
  count_by_paper: Record<string, number> | null;
  mean_ocr_quality: number | null;
  centroid_x: number | null;
  centroid_y: number | null;
  top_terms: string[] | null;
}

export async function GET(req: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(req))) {
    return rateLimitResponse();
  }

  const id = req.nextUrl.searchParams.get("id");
  if (!id) return jsonError("Missing id parameter", 400);

  try {
    return await buildDossier(id);
  } catch (err) {
    console.error("dossier failed:", err);
    const message = err instanceof Error ? err.message : "Internal error";
    return jsonError(message, 500);
  }
}

async function buildDossier(id: string): Promise<Response> {
  const supabase = getSupabase();

  // ---- Cluster row -------------------------------------------------------
  const { data: cluster, error: clusterErr } = await supabase
    .from("clusters")
    .select(
      "id, run_id, tier, label, size, label_text, date_min, date_max, " +
        "drift_cumulative, drift_net, drift_weeks, " +
        "active_size, burstiness, active_date_min, active_date_max",
    )
    .eq("id", id)
    .single();
  if (clusterErr || !cluster) {
    return jsonError("Cluster not found", 404);
  }
  const c = cluster as unknown as ClusterDbRow;

  // ---- Member chunks (active + content only) -----------------------------
  const tierCol = `cluster_t${c.tier}` as
    | "cluster_t0" | "cluster_t1" | "cluster_t2" | "cluster_t3";
  const members: MemberRow[] = [];
  const pageSize = 1000;
  let offset = 0;
  while (true) {
    const { data, error } = await supabase
      .from("chunk_projections")
      .select(
        "chunk_id, x, y, chunks!inner(status, content, quality_score, " +
          "pages!inner(sequence, image_url, issues!inner(date_issued, edition, papers!inner(lccn, title))))",
      )
      .eq("run_id", c.run_id)
      .eq(tierCol, c.label)
      .eq("content_type", 0)
      .range(offset, offset + pageSize - 1);
    if (error) throw new Error(`load members: ${error.message}`);
    if (!data || data.length === 0) break;
    for (const row of data as unknown as MemberRow[]) {
      if (row.chunks?.status !== "active") continue;
      members.push(row);
    }
    if (data.length < pageSize) break;
    offset += pageSize;
  }

  const chunks: DossierChunk[] = [];
  for (const m of members) {
    const ch = m.chunks!;
    const issue = ch.pages?.issues;
    const paper = issue?.papers;
    if (!ch.pages || !issue || !paper) continue;
    chunks.push({
      chunk_id: m.chunk_id,
      date: issue.date_issued,
      paper_lccn: paper.lccn,
      paper_title: paper.title,
      page_sequence: ch.pages.sequence,
      edition: issue.edition,
      excerpt: firstWords(ch.content, EXCERPT_WORDS),
      quality: ch.quality_score ?? 1.0,
      x: m.x,
      y: m.y,
      loc_url: locPageUrl(paper.lccn, issue.date_issued, issue.edition, ch.pages.sequence),
    });
  }
  chunks.sort(
    (a, b) =>
      a.date.localeCompare(b.date) ||
      a.paper_lccn.localeCompare(b.paper_lccn) ||
      a.page_sequence - b.page_sequence,
  );

  // ---- Paper order (by contribution) --------------------------------------
  const paperCounts = new Map<string, { title: string; count: number }>();
  for (const ch of chunks) {
    const cur = paperCounts.get(ch.paper_lccn);
    if (cur) cur.count += 1;
    else paperCounts.set(ch.paper_lccn, { title: ch.paper_title, count: 1 });
  }
  const papersOrdered = Array.from(paperCounts.entries())
    .sort((a, b) => b[1].count - a[1].count)
    .map(([lccn, v]) => ({ lccn, title: v.title }));
  const paperShares = Array.from(paperCounts.entries())
    .map(([lccn, v]) => ({
      lccn,
      title: v.title,
      count: v.count,
      share: chunks.length > 0 ? v.count / chunks.length : 0,
    }))
    .sort((a, b) => b.count - a.count);

  // ---- Weeks: prefer cluster_weeks, fall back to deriving from chunks -----
  let weeks: DossierWeek[] = [];
  const { data: weekData, error: weekErr } = await supabase
    .from("cluster_weeks")
    .select(
      "week_start, chunk_count, count_by_paper, mean_ocr_quality, centroid_x, centroid_y, top_terms",
    )
    .eq("cluster_id", c.id)
    .order("week_start");
  if (!weekErr && weekData && weekData.length > 0) {
    weeks = (weekData as unknown as WeekDbRow[]).map((w) => ({
      week_start: w.week_start,
      chunk_count: w.chunk_count,
      count_by_paper: w.count_by_paper ?? {},
      mean_ocr_quality: w.mean_ocr_quality,
      centroid_x: w.centroid_x,
      centroid_y: w.centroid_y,
      top_terms: Array.isArray(w.top_terms) ? w.top_terms : [],
    }));
  } else {
    // Pre-migration / pre-script fallback: counts, paper mix, quality,
    // and UMAP centroids all derive from the chunk list we already
    // loaded. Only top_terms needs the Python pass (c-TF-IDF over full
    // content) — empty until cluster_weeks.py has run.
    weeks = deriveWeeksFromChunks(chunks);
  }

  // ---- Shape tag (percentile vs same-tier clusters in this run) -----------
  const shape = await computeShape(supabase, c);

  const driftRatio =
    c.drift_cumulative && c.drift_cumulative > 1e-9
      ? Math.min(1, (c.drift_net ?? 0) / c.drift_cumulative)
      : null;

  const response: DossierResponse = {
    cluster: {
      id: c.id,
      tier: c.tier,
      label: c.label,
      label_text: isRefusalLabel(c.label_text) ? null : c.label_text,
      size: c.size,
      active_size: chunks.length,
      burstiness: c.burstiness,
      drift_net: c.drift_net,
      drift_cumulative: c.drift_cumulative,
      drift_ratio: driftRatio,
      shape_tag: shape.tag,
      shape_explanation: shape.explanation,
      date_min: c.active_date_min ?? c.date_min ?? "",
      date_max: c.active_date_max ?? c.date_max ?? "",
      papers: paperShares,
    },
    weeks,
    chunks,
    papers: papersOrdered,
  };

  return Response.json(response, {
    headers: { "Cache-Control": "public, max-age=600" },
  });
}

function firstWords(text: string, n: number): string {
  const words = text.trim().split(/\s+/);
  const slice = words.slice(0, n).join(" ");
  return words.length > n ? slice + " …" : slice;
}

function locPageUrl(
  lccn: string,
  dateIssued: string,
  edition: number,
  sequence: number,
): string {
  // The current loc.gov viewer takes plain /resource/ URLs. The legacy
  // chroniclingamerica words= highlight parameter has no documented
  // equivalent on the new viewer (and LOC blocks automated checks from
  // this environment), so we ship plain page links.
  return `https://www.loc.gov/resource/${lccn}/${dateIssued}/ed-${edition}/seq-${sequence}/`;
}

function deriveWeeksFromChunks(chunks: DossierChunk[]): DossierWeek[] {
  const byWeek = new Map<string, DossierChunk[]>();
  for (const ch of chunks) {
    const wk = isoWeekStartStr(ch.date);
    const list = byWeek.get(wk);
    if (list) list.push(ch);
    else byWeek.set(wk, [ch]);
  }
  return Array.from(byWeek.entries())
    .sort((a, b) => a[0].localeCompare(b[0]))
    .map(([week_start, list]) => {
      const byPaper: Record<string, number> = {};
      let qSum = 0;
      let xSum = 0;
      let ySum = 0;
      for (const ch of list) {
        byPaper[ch.paper_lccn] = (byPaper[ch.paper_lccn] ?? 0) + 1;
        qSum += ch.quality;
        xSum += ch.x;
        ySum += ch.y;
      }
      return {
        week_start,
        chunk_count: list.length,
        count_by_paper: byPaper,
        mean_ocr_quality: qSum / list.length,
        centroid_x: xSum / list.length,
        centroid_y: ySum / list.length,
        top_terms: [],
      };
    });
}

async function computeShape(
  supabase: ReturnType<typeof getSupabase>,
  c: ClusterDbRow,
): Promise<{ tag: string; explanation: string }> {
  // Percentile distributions over same-tier, same-run clusters with
  // active members — the same population the brief's shape tags use.
  const { data, error } = await supabase
    .from("clusters")
    .select("burstiness, drift_cumulative, drift_net, drift_weeks, active_size")
    .eq("run_id", c.run_id)
    .eq("tier", c.tier);
  const burstDist: number[] = [];
  const ratioDist: number[] = [];
  const cumDist: number[] = [];
  let corpusWeeks = 0;
  if (!error && data) {
    for (const row of data as {
      burstiness: number | null;
      drift_cumulative: number | null;
      drift_net: number | null;
      drift_weeks: number | null;
      active_size: number | null;
    }[]) {
      if (row.active_size !== null && row.active_size <= 0) continue;
      if (row.burstiness !== null) burstDist.push(row.burstiness);
      if (
        row.drift_cumulative !== null &&
        row.drift_cumulative > 1e-9 &&
        row.drift_net !== null
      ) {
        ratioDist.push(Math.min(1, row.drift_net / row.drift_cumulative));
        cumDist.push(row.drift_cumulative);
      }
      if (row.drift_weeks && row.drift_weeks > corpusWeeks) {
        corpusWeeks = row.drift_weeks;
      }
    }
  }
  burstDist.sort((a, b) => a - b);
  ratioDist.sort((a, b) => a - b);
  cumDist.sort((a, b) => a - b);

  const ratio =
    c.drift_cumulative && c.drift_cumulative > 1e-9
      ? Math.min(1, (c.drift_net ?? 0) / c.drift_cumulative)
      : null;
  return deriveShapeTag({
    burstiness_pct: percentileRank(c.burstiness ?? 0, burstDist),
    ratio_pct: ratio !== null ? percentileRank(ratio, ratioDist) : 0,
    cum_pct:
      c.drift_cumulative !== null
        ? percentileRank(c.drift_cumulative, cumDist)
        : 0,
    weeks: c.drift_weeks ?? 0,
    corpus_weeks: corpusWeeks,
  });
}
