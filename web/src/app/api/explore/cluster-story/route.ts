import { NextRequest } from "next/server";
import { getSupabase } from "@/lib/supabase";
import { synthesizeStream } from "@/lib/synth";
import type { RankedChunk, Citation, AskResponse } from "@/lib/types";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

const STORY_QUESTION =
  "These passages were selected as the most representative samples from a single semantic cluster of 1840s newspaper coverage. What is the dominant story they tell? Summarize the key events, people, places, and dates. Note how coverage evolves across dates. If a small number of passages stray onto an unrelated topic (e.g. shared vocabulary about violence but a different event), focus on the dominant theme and ignore the strays — do not write a 'two stories' summary.";

const REP_CHUNK_COUNT = 12;
// If the clicked cluster has fewer chunks than this, we'll supplement
// with chunks from the closest-centroid sibling clusters at the same
// tier — otherwise the synthesis runs strictly on cluster members.
const MIN_CLUSTER_CHUNKS = 6;
const NEIGHBOR_TOP_K = 2;

interface ProjectionRow {
  chunk_id: string;
  chunks: {
    embedding?: number[] | string | null;
    pages: { issues: { date_issued: string } | null } | null;
  } | null;
}

interface ChunkRow {
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

interface ClusterRow {
  id: string;
  label: number;
  size: number;
  centroid: number[] | string | null;
}

function pickEvenlyByDate(rows: { chunk_id: string; date: string }[], n: number): string[] {
  if (rows.length <= n) return rows.map((r) => r.chunk_id);
  const sorted = [...rows].sort((a, b) => a.date.localeCompare(b.date));
  const step = sorted.length / n;
  const picked: string[] = [];
  for (let k = 0; k < n; k++) {
    const idx = Math.min(Math.floor(k * step), sorted.length - 1);
    picked.push(sorted[idx].chunk_id);
  }
  return Array.from(new Set(picked));
}

function parseCentroid(c: number[] | string | null): number[] | null {
  if (Array.isArray(c)) return c;
  if (typeof c !== "string") return null;
  try {
    const trimmed = c.trim();
    if (trimmed.startsWith("[")) return JSON.parse(trimmed) as number[];
    return null;
  } catch {
    return null;
  }
}

function cosineDistance(a: number[], b: number[]): number {
  if (a.length !== b.length) return 2;
  let dot = 0;
  let na = 0;
  let nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  const denom = Math.sqrt(na) * Math.sqrt(nb);
  if (denom === 0) return 2;
  return 1 - dot / denom;
}

export async function POST(req: NextRequest) {
  if (!checkRateLimit("cluster-story", clientIp(req))) {
    return rateLimitResponse();
  }

  let body: { tier?: number; label?: number; refresh?: boolean };
  try {
    body = await req.json();
  } catch {
    return jsonError("Invalid JSON body", 400);
  }

  const { tier, label, refresh } = body;
  if (
    tier === undefined ||
    tier === null ||
    label === undefined ||
    label === null
  ) {
    return jsonError("Missing tier or label", 400);
  }
  if (tier < 0 || tier > 3) {
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
  const runId = activeRun.run_id;

  const { data: cluster, error: clusterError } = await supabase
    .from("clusters")
    .select("id, size, centroid, story_text, story_citations")
    .eq("run_id", runId)
    .eq("tier", tier)
    .eq("label", label)
    .single();

  if (clusterError || !cluster) {
    return jsonError("Cluster not found", 404);
  }

  const encoder = new TextEncoder();

  if (!refresh && cluster.story_text) {
    const cached: AskResponse = {
      text: cluster.story_text,
      citations: (cluster.story_citations ?? []) as Citation[],
      refused: false,
      input_tokens: 0,
      output_tokens: 0,
    };
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(
          encoder.encode(
            `event: token\ndata: ${JSON.stringify({ text: cached.text })}\n\n`
          )
        );
        controller.enqueue(
          encoder.encode(`event: done\ndata: ${JSON.stringify(cached)}\n\n`)
        );
        controller.close();
      },
    });
    return new Response(stream, {
      headers: {
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Cache": "HIT",
      },
    });
  }

  const tierCol = `cluster_t${tier}` as
    | "cluster_t0" | "cluster_t1" | "cluster_t2" | "cluster_t3";

  // Determine which cluster labels to pull from. STRICT by default —
  // only the clicked cluster. If the cluster has fewer chunks than
  // MIN_CLUSTER_CHUNKS, supplement with the K nearest-centroid sibling
  // clusters so the synthesis has enough material.
  const sourceLabels: number[] = [label];
  if (cluster.size < MIN_CLUSTER_CHUNKS) {
    const targetCentroid = parseCentroid(cluster.centroid);
    if (targetCentroid) {
      const { data: siblings } = await supabase
        .from("clusters")
        .select("label, centroid")
        .eq("run_id", runId)
        .eq("tier", tier)
        .neq("label", label);
      const ranked: { label: number; dist: number }[] = [];
      for (const s of (siblings ?? []) as ClusterRow[]) {
        const vec = parseCentroid(s.centroid);
        if (vec && vec.length === targetCentroid.length) {
          ranked.push({ label: s.label, dist: cosineDistance(targetCentroid, vec) });
        }
      }
      ranked.sort((a, b) => a.dist - b.dist);
      for (const n of ranked.slice(0, NEIGHBOR_TOP_K)) {
        sourceLabels.push(n.label);
      }
    }
  }

  // Fetch chunks IN the source clusters along with their embeddings,
  // so we can rank by distance to the target cluster's centroid and
  // drop mixed-content boundary chunks before they reach Sonnet.
  const all: {
    chunk_id: string;
    date: string;
    embedding: number[] | string | null;
  }[] = [];
  const pageSize = 1000;
  let offset = 0;
  while (true) {
    const { data, error } = await supabase
      .from("chunk_projections")
      .select(
        "chunk_id, chunks!inner(embedding, pages!inner(issues!inner(date_issued)))"
      )
      .eq("run_id", runId)
      .in(tierCol, sourceLabels)
      .range(offset, offset + pageSize - 1);

    if (error) {
      return jsonError(error.message, 500);
    }
    if (!data || data.length === 0) break;

    for (const row of data as unknown as ProjectionRow[]) {
      const d = row.chunks?.pages?.issues?.date_issued;
      if (d) {
        all.push({
          chunk_id: row.chunk_id,
          date: d,
          embedding: row.chunks?.embedding ?? null,
        });
      }
    }

    if (data.length < pageSize) break;
    offset += pageSize;
  }

  if (all.length === 0) {
    return jsonError("No chunks found for cluster", 404);
  }

  // Rank chunks by cosine distance to the TARGET cluster's centroid,
  // take the top REP_CHUNK_COUNT (most cluster-typical), then sort
  // those by date so Sonnet sees the story in chronological order.
  const targetCentroid = parseCentroid(cluster.centroid);
  let repIds: string[];
  if (targetCentroid) {
    const ranked = all
      .map((c) => {
        const vec = parseCentroid(c.embedding);
        const dist =
          vec && vec.length === targetCentroid.length
            ? cosineDistance(targetCentroid, vec)
            : 2;
        return { chunk_id: c.chunk_id, date: c.date, dist };
      })
      .filter((r) => r.dist < 1.5)
      .sort((a, b) => a.dist - b.dist)
      .slice(0, REP_CHUNK_COUNT)
      .sort((a, b) => a.date.localeCompare(b.date));
    repIds = ranked.map((r) => r.chunk_id);
  } else {
    // Fallback if the centroid is missing — preserves old behavior.
    repIds = pickEvenlyByDate(all, REP_CHUNK_COUNT);
  }

  if (repIds.length === 0) {
    return jsonError("Could not select representative chunks", 500);
  }

  const { data: chunksData, error: chunksErr } = await supabase
    .from("chunks")
    .select(
      "id, content, page_id, pages!inner(sequence, image_url, issues!inner(date_issued, edition, papers!inner(lccn, title)))"
    )
    .in("id", repIds);

  if (chunksErr || !chunksData) {
    return Response.json(
      { error: chunksErr?.message ?? "Failed to load chunks" },
      { status: 500 }
    );
  }

  const chunksById = new Map<string, ChunkRow>();
  for (const c of chunksData as unknown as ChunkRow[]) chunksById.set(c.id, c);

  const chunks: RankedChunk[] = [];
  for (const id of repIds) {
    const row = chunksById.get(id);
    if (!row?.pages?.issues?.papers) continue;
    chunks.push({
      chunk_id: row.id,
      content: row.content,
      page_id: row.page_id,
      paper_lccn: row.pages.issues.papers.lccn,
      paper_title: row.pages.issues.papers.title,
      date_issued: row.pages.issues.date_issued,
      edition: row.pages.issues.edition,
      page_sequence: row.pages.sequence,
      image_url: row.pages.image_url,
      resource_url: row.pages.image_url,
      rrf_score: 0,
    });
  }

  if (chunks.length === 0) {
    return Response.json(
      { error: "Could not assemble chunks" },
      { status: 500 }
    );
  }

  const stream = new ReadableStream({
    async start(controller) {
      let finalResponse: AskResponse | null = null;
      try {
        for await (const event of synthesizeStream(
          STORY_QUESTION,
          chunks,
          "research"
        )) {
          if (event.type === "token") {
            controller.enqueue(
              encoder.encode(
                `event: token\ndata: ${JSON.stringify({ text: event.text })}\n\n`
              )
            );
          } else {
            finalResponse = event.response;
            controller.enqueue(
              encoder.encode(
                `event: done\ndata: ${JSON.stringify(event.response)}\n\n`
              )
            );
          }
        }
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Internal error";
        controller.enqueue(
          encoder.encode(
            `event: error\ndata: ${JSON.stringify({ error: message })}\n\n`
          )
        );
      } finally {
        controller.close();
      }

      if (finalResponse && !finalResponse.refused) {
        try {
          await supabase
            .from("clusters")
            .update({
              story_text: finalResponse.text,
              story_citations: finalResponse.citations,
              story_generated_at: new Date().toISOString(),
            })
            .eq("id", cluster.id);
        } catch {
          // Best-effort cache write; don't block the stream
        }
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      "X-Cache": "MISS",
    },
  });
}
