import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

export async function GET(request: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(request))) {
    return rateLimitResponse();
  }

  const tier = parseInt(request.nextUrl.searchParams.get("tier") ?? "0", 10);
  if (tier < 0 || tier > 3 || isNaN(tier)) {
    return jsonError("tier must be 0-3", 400);
  }

  const supabase = getSupabase();

  const { data: activeRun, error: runError } = await supabase
    .from("active_cluster_run")
    .select("run_id")
    .single();

  if (runError || !activeRun) {
    return jsonError("No active cluster run", 404);
  }

  const { data, error } = await supabase
    .from("clusters")
    .select("id, label, size, date_min, date_max, parent_id, label_text")
    .eq("run_id", activeRun.run_id)
    .eq("tier", tier)
    .order("label");

  if (error) {
    return jsonError(error.message, 500);
  }

  return Response.json(data ?? [], {
    headers: { "Cache-Control": "public, max-age=3600" },
  });
}
