import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

// The district roster for the filter chips.
export async function GET(req: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(req))) return rateLimitResponse();
  try {
    const { data, error } = await getSupabase()
      .from("districts")
      .select("slug, name")
      .order("slug");
    if (error) throw new Error(error.message);
    return Response.json({ districts: data ?? [] });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to load districts";
    return jsonError(message, 500);
  }
}
