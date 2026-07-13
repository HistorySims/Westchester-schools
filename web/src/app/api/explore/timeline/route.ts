import type { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

/**
 * Binary payload for the TimelineMinimap.
 *
 * Format:
 *   [4 bytes] uint32  chunk_count
 *   [4 bytes] uint32  papers_section_byte_length
 *   [N bytes] papers section: each line "<lccn>\t<title>\n" (UTF-8)
 *   [chunk_count × 14 bytes] chunks in chunk_id order (parallel to
 *     /api/explore/chunk-ids):
 *       [2] paper_idx (uint16)
 *       [2] date_offset (uint16, days since min_date)
 *       [2] cluster_t0 (int16)
 *       [2] cluster_t1 (int16)
 *       [2] cluster_t2 (int16)
 *       [2] cluster_t3 (int16)
 *       [1] quality_u8  (0..255, scaled quality_score; 255 if unscored)
 *       [1] content_type (uint8)
 *
 * Response headers:
 *   X-Min-Date / X-Max-Date  (YYYY-MM-DD)
 */

interface ProjRow {
  chunk_id: string;
  cluster_t0: number;
  cluster_t1: number;
  cluster_t2: number;
  cluster_t3: number;
  content_type: number;
  chunks: {
    quality_score: number | null;
    pages: {
      issues: {
        date_issued: string;
        papers: { lccn: string; title: string } | null;
      } | null;
    } | null;
  } | null;
}

export async function GET(req: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(req))) {
    return rateLimitResponse();
  }

  const supabase = getSupabase();

  const { data: activeRun, error: runError } = await supabase
    .from("active_cluster_run")
    .select("run_id")
    .single();
  if (runError || !activeRun) {
    return jsonError("No active cluster run", 404);
  }

  const all: ProjRow[] = [];
  const pageSize = 1000;
  let offset = 0;
  while (true) {
    const { data, error } = await supabase
      .from("chunk_projections")
      .select(
        "chunk_id, cluster_t0, cluster_t1, cluster_t2, cluster_t3, content_type, " +
          "chunks!inner(quality_score, pages!inner(issues!inner(date_issued, papers!inner(lccn, title))))"
      )
      .eq("run_id", activeRun.run_id)
      .order("chunk_id")
      .range(offset, offset + pageSize - 1);

    if (error) {
      return jsonError(error.message, 500);
    }
    if (!data || data.length === 0) break;
    all.push(...(data as unknown as ProjRow[]));
    if (data.length < pageSize) break;
    offset += pageSize;
  }

  if (all.length === 0) {
    return jsonError("No timeline data", 404);
  }

  // Build papers ordering: stable by first-appearance
  const paperIdxByLccn = new Map<string, number>();
  const papers: { lccn: string; title: string }[] = [];
  let minDateStr = "9999-12-31";
  let maxDateStr = "0000-01-01";
  for (const row of all) {
    const papers_ = row.chunks?.pages?.issues?.papers;
    const d = row.chunks?.pages?.issues?.date_issued;
    if (papers_ && !paperIdxByLccn.has(papers_.lccn)) {
      paperIdxByLccn.set(papers_.lccn, papers.length);
      papers.push({ lccn: papers_.lccn, title: papers_.title });
    }
    if (d) {
      if (d < minDateStr) minDateStr = d;
      if (d > maxDateStr) maxDateStr = d;
    }
  }
  const minDate = new Date(minDateStr);
  const MS_PER_DAY = 24 * 60 * 60 * 1000;

  const papersBuf = new TextEncoder().encode(
    papers.map((p) => `${p.lccn}\t${p.title}`).join("\n") + "\n"
  );

  const n = all.length;
  const headerLen = 4 + 4 + papersBuf.byteLength;
  const buf = new ArrayBuffer(headerLen + n * 14);
  const view = new DataView(buf);
  new Uint8Array(buf, 4 + 4, papersBuf.byteLength).set(papersBuf);
  view.setUint32(0, n, true);
  view.setUint32(4, papersBuf.byteLength, true);

  let chunkOffset = headerLen;
  for (const row of all) {
    const papers_ = row.chunks?.pages?.issues?.papers;
    const d = row.chunks?.pages?.issues?.date_issued;
    const paperIdx = papers_ ? paperIdxByLccn.get(papers_.lccn) ?? 0 : 0;
    const dateOffset = d
      ? Math.max(
          0,
          Math.floor((new Date(d).getTime() - minDate.getTime()) / MS_PER_DAY)
        )
      : 0;
    const qRaw = row.chunks?.quality_score ?? 1.0;
    const q = Math.max(0, Math.min(255, Math.round(qRaw * 255)));

    view.setUint16(chunkOffset, paperIdx, true);
    view.setUint16(chunkOffset + 2, dateOffset, true);
    view.setInt16(chunkOffset + 4, row.cluster_t0, true);
    view.setInt16(chunkOffset + 6, row.cluster_t1, true);
    view.setInt16(chunkOffset + 8, row.cluster_t2, true);
    view.setInt16(chunkOffset + 10, row.cluster_t3, true);
    view.setUint8(chunkOffset + 12, q);
    view.setUint8(chunkOffset + 13, row.content_type);

    chunkOffset += 14;
  }

  return new Response(buf, {
    headers: {
      "Content-Type": "application/octet-stream",
      "Cache-Control": "public, max-age=3600",
      "X-Min-Date": minDateStr,
      "X-Max-Date": maxDateStr,
    },
  });
}
