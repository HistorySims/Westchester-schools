import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

// Latest topic-map snapshot for the /explore page. The heavy JSON lives in the
// cluster_maps table (written by `herald-cluster run --publish`); return the
// most recent one. Cached briefly at the edge — a new map appears roughly
// monthly, so staleness is fine.
export async function GET(req: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(req))) return rateLimitResponse();
  try {
    const { data, error } = await getSupabase()
      .from("cluster_maps")
      .select("data, generated_at, n_points, n_clusters")
      .order("created_at", { ascending: false })
      .limit(1)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!data) {
      return Response.json({ map: null }, { headers: { "Cache-Control": "no-store" } });
    }
    return Response.json(
      { map: data.data, generated_at: data.generated_at },
      { headers: { "Cache-Control": "public, s-maxage=300, stale-while-revalidate=3600" } }
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to load map";
    return jsonError(message, 500);
  }
}
