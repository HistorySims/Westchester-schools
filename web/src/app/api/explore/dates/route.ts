import type { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

interface NestedRow {
  chunk_id: string;
  chunks: {
    pages: {
      issues: {
        date_issued: string;
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

  const all: { chunkId: string; dateIssued: string }[] = [];
  const pageSize = 1000;
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .from("chunk_projections")
      .select("chunk_id, chunks!inner(pages!inner(issues!inner(date_issued)))")
      .eq("run_id", activeRun.run_id)
      .order("chunk_id")
      .range(offset, offset + pageSize - 1);

    if (error) {
      return jsonError(error.message, 500);
    }
    if (!data || data.length === 0) break;

    for (const row of data as unknown as NestedRow[]) {
      const dateIssued = row.chunks?.pages?.issues?.date_issued;
      if (dateIssued) {
        all.push({ chunkId: row.chunk_id, dateIssued });
      }
    }

    if (data.length < pageSize) break;
    offset += pageSize;
  }

  if (all.length === 0) {
    return jsonError("No date data", 404);
  }

  let minDate = "9999-12-31";
  let maxDate = "0000-01-01";
  for (const row of all) {
    if (row.dateIssued < minDate) minDate = row.dateIssued;
    if (row.dateIssued > maxDate) maxDate = row.dateIssued;
  }
  const minDateObj = new Date(minDate);
  const maxOffset = Math.floor(
    (new Date(maxDate).getTime() - minDateObj.getTime()) / (24 * 60 * 60 * 1000)
  );

  const n = all.length;
  const buf = new ArrayBuffer(8 + n * 2);
  const view = new DataView(buf);
  view.setUint32(0, n, true);
  view.setUint32(4, maxOffset, true);

  for (let i = 0; i < n; i++) {
    const d = new Date(all[i].dateIssued);
    const off = Math.floor((d.getTime() - minDateObj.getTime()) / (24 * 60 * 60 * 1000));
    view.setUint16(8 + i * 2, off, true);
  }

  return new Response(buf, {
    headers: {
      "Content-Type": "application/octet-stream",
      "Cache-Control": "public, max-age=3600",
      "X-Min-Date": minDate,
      "X-Max-Date": maxDate,
    },
  });
}
