import Anthropic from "@anthropic-ai/sdk";
import type { RankedChunk, Citation, AskResponse, ResponseMode } from "./types";

const MODEL = "claude-sonnet-4-6";
const MAX_TOKENS = 2500;
const TEMPERATURE = 0.2;

let _client: Anthropic | null = null;
function getClient(): Anthropic {
  if (_client) return _client;
  const key = process.env.ANTHROPIC_API_KEY;
  if (!key) throw new Error("Missing ANTHROPIC_API_KEY");
  _client = new Anthropic({ apiKey: key });
  return _client;
}

const SYNTHESIS_PROMPT = `You are a research assistant grounded in a specific newspaper corpus. \
You will be given a numbered list of source passages (chunks) from \
historic New York newspapers — primarily the New-York Daily Tribune \
(Horace Greeley) — for queries between roughly 1842 and 1846. \
Each chunk has an ID, a paper name, a date, and a page reference. \
Answer the user's question using only these passages.

Citation rule. Every factual claim must be followed by one or more \
citation markers in the form [N], where N is the chunk's number in \
the source list. Do not cite chunks you did not use. Do not invent \
chunk numbers. If the passages do not contain enough evidence to \
answer, say so plainly and stop — do not pad with general knowledge.

Paper-aware attribution. When a claim derives from a specific paper, \
name the paper in your prose ("the Tribune reports...", "the Evening \
Journal frames it as..."). When papers disagree or use different \
language about the same event, surface the contrast — that contrast \
is often the point of the question.

Tone. Write like a careful historian briefing a colleague: precise, \
plain, neither breezy nor stuffy. Quote the papers sparingly and only \
when their exact wording is the point. Do not modernize 19th-century \
terminology silently; if you use a period term like "Calico Indians" \
or "patroon," let the chunks do the explaining.

Refusal floor. If fewer than two chunks address the question, default \
to "The corpus does not have enough to support a confident answer — \
here is what little it does say: ..." Better to be small than wrong.`;

const RESEARCH_PROMPT = `You are a research assistant grounded in a specific newspaper corpus. \
You will be given a numbered list of source passages (chunks) from \
historic New York newspapers for queries between roughly 1842 and 1846.

The user wants a source-by-source research guide. Structure your response as:

1. **Lead with the richest sources.** Start with whichever 2-3 passages \
would be most valuable for a researcher to read first. Explain in one \
sentence WHY each one matters ("contains the only first-person account," \
"has the fullest physical description of the disguises," etc.).

2. **Quote generously.** For each key source, include one or two direct \
quotes that capture the most vivid or evidentiary language. Use the \
papers' own words — period language is the point.

3. **Note additional references briefly.** After the key sources, list \
any remaining relevant passages in one line each with [N], so the \
researcher knows where else to look.

4. **End with a search suggestion.** Give the user a concrete search \
query they can paste into chroniclingamerica.loc.gov/search/ to find \
more coverage beyond this corpus. Format: \
"For more, search Chronicling America for: [suggested terms]"

Citation rule. Every claim must include [N] markers. Do not cite \
chunks you did not use or invent chunk numbers.

Tone. Write like a research librarian handing someone a stack of \
flagged photocopies: direct, practical, focused on getting them to \
the primary sources fast.`;

const PROMPTS: Record<Exclude<ResponseMode, "directory">, string> = {
  synthesis: SYNTHESIS_PROMPT,
  research: RESEARCH_PROMPT,
};

const CITE_RE = /\[(\d+)\]/g;

function buildUserMessage(question: string, chunks: RankedChunk[]): string {
  const lines = [`QUESTION: ${question.trim()}`, "", "SOURCES:"];
  chunks.forEach((c, i) => {
    const cite = `[${i + 1}] ${c.paper_title}, ${c.date_issued}, p.${c.page_sequence} ed-${c.edition}`;
    let snippet = c.content.trim();
    if (snippet.length > 4000) snippet = snippet.slice(0, 4000) + " ...";
    lines.push(cite);
    lines.push(`    ${snippet}`);
    lines.push("");
  });
  return lines.join("\n").trimEnd();
}

function extractCitationIndices(text: string): number[] {
  const indices: number[] = [];
  let match;
  const re = new RegExp(CITE_RE.source, "g");
  while ((match = re.exec(text)) !== null) {
    indices.push(parseInt(match[1], 10));
  }
  return indices;
}

function looksLikeRefusal(text: string): boolean {
  const needle = "does not have enough to support a confident answer";
  const hasPhrase = text.toLowerCase().includes(needle.toLowerCase());
  const hasCitations = CITE_RE.test(text);
  return hasPhrase && !hasCitations;
}

async function callClaude(
  userMessage: string,
  systemPrompt: string = SYNTHESIS_PROMPT
): Promise<{ text: string; inputTokens: number; outputTokens: number }> {
  const resp = await getClient().messages.create({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    temperature: TEMPERATURE,
    system: systemPrompt,
    messages: [{ role: "user", content: userMessage }],
  });

  const text = resp.content
    .filter((b) => b.type === "text")
    .map((b) => (b as Anthropic.TextBlock).text)
    .join("");

  return {
    text,
    inputTokens: resp.usage.input_tokens,
    outputTokens: resp.usage.output_tokens,
  };
}

function buildDirectoryResponse(
  question: string,
  chunks: RankedChunk[]
): AskResponse {
  const citations: Citation[] = chunks.map((c, i) => ({
    index: i + 1,
    chunk_id: c.chunk_id,
    paper_title: c.paper_title,
    paper_lccn: c.paper_lccn,
    date_issued: c.date_issued,
    page_sequence: c.page_sequence,
    edition: c.edition,
    image_url: c.image_url,
    resource_url: c.resource_url,
    snippet: c.content.slice(0, 200),
  }));

  const lines: string[] = [`${chunks.length} sources matched. Sorted by relevance.\n`];
  chunks.forEach((c, i) => {
    const n = i + 1;
    const snippet = c.content.trim().slice(0, 300).replace(/\n+/g, " ");
    lines.push(
      `[${n}] ${c.paper_title}, ${c.date_issued}, p.${c.page_sequence}\n` +
      `"${snippet}${c.content.length > 300 ? "..." : ""}"\n`
    );
  });

  return {
    text: lines.join("\n"),
    citations,
    refused: false,
    input_tokens: 0,
    output_tokens: 0,
  };
}

export async function* synthesizeStream(
  question: string,
  chunks: RankedChunk[],
  mode: ResponseMode = "synthesis"
): AsyncGenerator<
  { type: "token"; text: string } | { type: "done"; response: AskResponse }
> {
  if (chunks.length === 0) {
    yield {
      type: "done",
      response: {
        text: "No passages matched this query.",
        citations: [],
        refused: true,
        input_tokens: 0,
        output_tokens: 0,
      },
    };
    return;
  }

  if (mode === "directory") {
    const response = buildDirectoryResponse(question, chunks);
    yield { type: "done", response };
    return;
  }

  const systemPrompt = PROMPTS[mode];
  const userMsg = buildUserMessage(question, chunks);

  const stream = getClient().messages.stream({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    temperature: TEMPERATURE,
    system: systemPrompt,
    messages: [{ role: "user", content: userMsg }],
  });

  let fullText = "";
  for await (const event of stream) {
    if (
      event.type === "content_block_delta" &&
      event.delta.type === "text_delta"
    ) {
      fullText += event.delta.text;
      yield { type: "token", text: event.delta.text };
    }
  }

  const finalMessage = await stream.finalMessage();
  const inputTokens = finalMessage.usage.input_tokens;
  const outputTokens = finalMessage.usage.output_tokens;

  const validIndices = new Set(
    Array.from({ length: chunks.length }, (_, i) => i + 1)
  );
  let cited = extractCitationIndices(fullText);
  const bad = cited.filter((n) => !validIndices.has(n));

  if (bad.length > 0) {
    const reminder =
      `\n\nNOTE: your previous response cited chunk numbers ` +
      `${JSON.stringify([...new Set(bad)].sort())} that don't exist. Valid chunk ` +
      `numbers are 1..${chunks.length}. Rewrite your answer ` +
      `without inventing chunk numbers.`;
    const retry = await callClaude(userMsg + reminder, systemPrompt);
    fullText = retry.text;
    cited = extractCitationIndices(fullText);
    const stillBad = cited.filter((n) => !validIndices.has(n));
    if (stillBad.length > 0) {
      throw new Error(
        `Hallucinated citation markers persisted after retry: ${JSON.stringify([...new Set(stillBad)].sort())}`
      );
    }
  }

  const citations: Citation[] = chunks.map((c, i) => ({
    index: i + 1,
    chunk_id: c.chunk_id,
    paper_title: c.paper_title,
    paper_lccn: c.paper_lccn,
    date_issued: c.date_issued,
    page_sequence: c.page_sequence,
    edition: c.edition,
    image_url: c.image_url,
    resource_url: c.resource_url,
    snippet: c.content.slice(0, 200),
  }));

  yield {
    type: "done",
    response: {
      text: fullText.trim(),
      citations,
      refused: looksLikeRefusal(fullText),
      input_tokens: inputTokens,
      output_tokens: outputTokens,
    },
  };
}
