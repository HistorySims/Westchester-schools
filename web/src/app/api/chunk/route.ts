import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

// One chunk's detail for click-to-read on the topic map: the passage text and
// its source document/date. Keyed by chunk id (the map exports `cid` per point).
export async function GET(req: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(req))) return rateLimitResponse();

  const id = new URL(req.url).searchParams.get("id")?.trim();
  if (!id) return jsonError("Missing 'id' query parameter", 400);

  try {
    const { data, error } = await getSupabase()
      .from("chunks")
      .select(
        "content, section_path, heading, meeting_date, doc_type, " +
          "documents(title, source_url), districts(slug)"
      )
      .eq("id", id)
      .maybeSingle();
    if (error) throw new Error(error.message);
    if (!data) return jsonError("Not found", 404);

    // No generated DB types, so cast to the shape we selected. Supabase types
    // joined relations as an object or array depending on the relationship;
    // handle both.
    type Rel<T> = T | T[] | null;
    const row = data as unknown as {
      content: string;
      section_path: string | null;
      heading: string | null;
      meeting_date: string | null;
      doc_type: string | null;
      documents: Rel<{ title: string; source_url: string }>;
      districts: Rel<{ slug: string }>;
    };
    const first = <T>(r: Rel<T>): T | null => (Array.isArray(r) ? r[0] ?? null : r);
    const doc = first(row.documents);
    const dist = first(row.districts);
    return Response.json(
      {
        content: row.content,
        section_path: row.section_path,
        heading: row.heading,
        meeting_date: row.meeting_date,
        doc_type: row.doc_type,
        doc_title: doc?.title ?? null,
        source_url: doc?.source_url ?? null,
        district: dist?.slug ?? null,
      },
      { headers: { "Cache-Control": "public, s-maxage=86400" } }
    );
  } catch (err) {
    const message = err instanceof Error ? err.message : "Failed to load passage";
    return jsonError(message, 500);
  }
}
