import { NextRequest } from "next/server";
import { retrieve, retrieveScoped } from "@/lib/retrieval";
import { synthesizeStream } from "@/lib/synth";
import type { AskRequest } from "@/lib/types";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

const MAX_QUESTION_LENGTH = 500;

export async function POST(req: NextRequest) {
  if (!checkRateLimit("ask", clientIp(req))) {
    return rateLimitResponse();
  }

  let body: AskRequest;
  try {
    body = await req.json();
  } catch {
    return jsonError("Invalid JSON body", 400);
  }

  const { question, mode, paper_lccn, date_from, date_to, scope_tier, scope_label } = body;

  if (!question || typeof question !== "string") {
    return jsonError("Missing or invalid 'question' field", 400);
  }

  if (question.length > MAX_QUESTION_LENGTH) {
    return jsonError(`Question exceeds ${MAX_QUESTION_LENGTH} character limit`, 400);
  }

  const encoder = new TextEncoder();

  const stream = new ReadableStream({
    async start(controller) {
      try {
        const isScoped =
          scope_tier !== null && scope_tier !== undefined &&
          scope_label !== null && scope_label !== undefined;
        const chunks = isScoped
          ? await retrieveScoped(question, scope_tier!, scope_label!)
          : await retrieve(question, {
              paperLccn: paper_lccn ?? null,
              dateFrom: date_from ?? null,
              dateTo: date_to ?? null,
            });

        for await (const event of synthesizeStream(question, chunks, mode)) {
          if (event.type === "token") {
            controller.enqueue(
              encoder.encode(`event: token\ndata: ${JSON.stringify({ text: event.text })}\n\n`)
            );
          } else {
            controller.enqueue(
              encoder.encode(`event: done\ndata: ${JSON.stringify(event.response)}\n\n`)
            );
          }
        }
      } catch (err) {
        const message =
          err instanceof Error ? err.message : "Internal server error";
        controller.enqueue(
          encoder.encode(`event: error\ndata: ${JSON.stringify({ error: message })}\n\n`)
        );
      } finally {
        controller.close();
      }
    },
  });

  return new Response(stream, {
    headers: {
      "Content-Type": "text/event-stream",
      "Cache-Control": "no-cache",
      Connection: "keep-alive",
    },
  });
}
