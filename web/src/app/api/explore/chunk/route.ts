import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

interface NestedChunkRow {
  id: string;
  content: string;
  page_id: string;
  pages: {
    sequence: number;
    image_url: string;
    issues: {
      date_issued: string;
      edition: number;
      papers: { lccn: string; title: string } | null;
    } | null;
  } | null;
}

function deriveResourceUrl(
  lccn: string,
  dateIssued: string,
  edition: number,
  pageSequence: number
): string {
  return `https://www.loc.gov/resource/${lccn}/${dateIssued}/ed-${edition}/seq-${pageSequence}/`;
}

export async function GET(request: NextRequest) {
  if (!checkRateLimit("explore-read", clientIp(request))) {
    return rateLimitResponse();
  }

  const id = request.nextUrl.searchParams.get("id");
  if (!id) {
    return jsonError("Missing id parameter", 400);
  }

  const supabase = getSupabase();

  const { data, error } = await supabase
    .from("chunks")
    .select(
      "id, content, page_id, pages!inner(sequence, image_url, issues!inner(date_issued, edition, papers!inner(lccn, title)))"
    )
    .eq("id", id)
    .single();

  if (error || !data) {
    return jsonError("Chunk not found", 404);
  }

  const row = data as unknown as NestedChunkRow;
  if (!row.pages?.issues?.papers) {
    return jsonError("Chunk has no associated paper", 404);
  }

  const { data: activeRun } = await supabase
    .from("active_cluster_run")
    .select("run_id")
    .single();

  let clusterLabels: number[] = [];
  let contentType = 0;
  if (activeRun) {
    const { data: proj } = await supabase
      .from("chunk_projections")
      .select("cluster_t0, cluster_t1, cluster_t2, cluster_t3, content_type")
      .eq("chunk_id", id)
      .eq("run_id", activeRun.run_id)
      .single();
    if (proj) {
      clusterLabels = [proj.cluster_t0, proj.cluster_t1, proj.cluster_t2, proj.cluster_t3];
      contentType = proj.content_type;
    }
  }

  const paper = row.pages.issues.papers;
  const issue = row.pages.issues;
  const page = row.pages;

  return Response.json({
    chunk_id: row.id,
    content: row.content,
    paper_title: paper.title,
    paper_lccn: paper.lccn,
    date_issued: issue.date_issued,
    edition: issue.edition,
    page_sequence: page.sequence,
    image_url: page.image_url,
    resource_url: deriveResourceUrl(
      paper.lccn,
      issue.date_issued,
      issue.edition,
      page.sequence
    ),
    cluster_labels: clusterLabels,
    content_type: contentType,
  });
}
