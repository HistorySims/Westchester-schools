import { NextRequest } from "next/server";
import { searchChunks } from "@/lib/schools-retrieval";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

// Full-text search over the corpus — "every mention of X". Cheap (Postgres
// FTS), so a generous rate bucket.
export async function GET(req: NextRequest) {
  if (!checkRateLimit("search", clientIp(req))) return rateLimitResponse();

  const url = new URL(req.url);
  const q = url.searchParams.get("q")?.trim();
  if (!q) return jsonError("Missing 'q' query parameter", 400);

  const districtsParam = url.searchParams.get("districts");
  const districts = districtsParam ? districtsParam.split(",").filter(Boolean) : null;
  const limit = Math.min(Number(url.searchParams.get("limit") ?? 60) || 60, 200);

  try {
    const hits = await searchChunks(q, limit, districts);
    return Response.json({ query: q, count: hits.length, hits });
  } catch (err) {
    const message = err instanceof Error ? err.message : "Search failed";
    return jsonError(message, 500);
  }
}
