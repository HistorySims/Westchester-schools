import type { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

interface ProjectionRow {
  x: number;
  y: number;
  cluster_t0: number;
  cluster_t1: number;
  cluster_t2: number;
  cluster_t3: number;
  content_type: number;
  chunk_id: string;
}

async function fetchAllProjections(runId: string): Promise<ProjectionRow[]> {
  const supabase = getSupabase();
  const allRows: ProjectionRow[] = [];
  const pageSize = 1000;
  let offset = 0;

  while (true) {
    const { data, error } = await supabase
      .from("chunk_projections")
      .select("x, y, cluster_t0, cluster_t1, cluster_t2, cluster_t3, content_type, chunk_id")
      .eq("run_id", runId)
      .order("chunk_id")
      .range(offset, offset + pageSize - 1);

    if (error) throw new Error(error.message);
    if (!data || data.length === 0) break;

    allRows.push(...data);
    if (data.length < pageSize) break;
    offset += pageSize;
  }

  return allRows;
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
    return jsonError(
      `No active cluster run: ${runError?.message ?? "empty result"}`,
      404
    );
  }

  const runId = activeRun.run_id;

  let data: ProjectionRow[];
  try {
    data = await fetchAllProjections(runId);
  } catch (e) {
    return jsonError(
      e instanceof Error ? e.message : "Failed to fetch projections",
      500
    );
  }

  if (data.length === 0) {
    return jsonError("No projection data found", 404);
  }

  const n = data.length;
  const pointBuf = new ArrayBuffer(4 + n * 17);
  const view = new DataView(pointBuf);

  view.setUint32(0, n, true);

  for (let i = 0; i < n; i++) {
    const offset = 4 + i * 17;
    const row = data[i];
    view.setFloat32(offset, row.x, true);
    view.setFloat32(offset + 4, row.y, true);
    view.setInt16(offset + 8, row.cluster_t0, true);
    view.setInt16(offset + 10, row.cluster_t1, true);
    view.setInt16(offset + 12, row.cluster_t2, true);
    view.setInt16(offset + 14, row.cluster_t3, true);
    view.setUint8(offset + 16, row.content_type);
  }

  return new Response(pointBuf, {
    headers: {
      "Content-Type": "application/octet-stream",
      "Cache-Control": "public, max-age=3600",
      "X-Run-Id": runId,
    },
  });
}
