import { NextRequest } from "next/server";
import { retrievePanel } from "@/lib/schools-retrieval";
import { synthesizeStream } from "@/lib/schools-synth";
import {
  checkRateLimit,
  clientIp,
  jsonError,
  rateLimitResponse,
} from "@/lib/rate-limit";

const MAX_QUESTION_LENGTH = 500;

export const maxDuration = 120; // Vercel function timeout (Sonnet + rerank)

interface AskBody {
  question?: string;
  districts?: string[] | null;
  doc_type?: string | null;
  date_from?: string | null;
  date_to?: string | null;
}

export async function POST(req: NextRequest) {
  if (!checkRateLimit("ask", clientIp(req))) return rateLimitResponse();

  let body: AskBody;
  try {
    body = await req.json();
  } catch {
    return jsonError("Invalid JSON body", 400);
  }

  const { question, districts, doc_type, date_from, date_to } = body;
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
        const panel = await retrievePanel(question, {
          districts: districts ?? null,
          docType: doc_type ?? null,
          dateFrom: date_from ?? null,
          dateTo: date_to ?? null,
        });
        for await (const event of synthesizeStream(panel)) {
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
        const message = err instanceof Error ? err.message : "Internal server error";
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
