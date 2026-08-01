// District-panel synthesis for the schools corpus — the TS port of
// herald.ask_schools. Evidence is grouped by district and numbered; the model
// is told which districts produced nothing; every claim must cite [N]. Uses
// claude-sonnet-5 (adaptive thinking on by default — no `temperature`, and
// max_tokens caps thinking+text combined, so keep it generous).

import Anthropic from "@anthropic-ai/sdk";
import type { EvidenceChunk, Panel } from "./schools-retrieval";

const MODEL = "claude-sonnet-5";
const MAX_TOKENS = 16000;
const MAX_CHUNK_CHARS = 1800;

let _client: Anthropic | null = null;
function getClient(): Anthropic {
  if (_client) return _client;
  const key = process.env.ANTHROPIC_API_KEY;
  if (!key) throw new Error("Missing ANTHROPIC_API_KEY");
  _client = new Anthropic({ apiKey: key });
  return _client;
}

const SYSTEM_PROMPT = `You are a research assistant grounded in a corpus of public school-district \
governance documents from Westchester County, NY — board agendas, meeting \
minutes, policies, student handbooks, contracts, and budgets from eight \
districts (Port Chester-Rye, Ossining, Peekskill, the Tarrytowns, Elmsford, \
Mount Vernon, Greenburgh Central, White Plains). Answer only from the \
numbered evidence passages provided.

The evidence is a PANEL: passages are grouped by district, and the prompt \
tells you which districts produced no evidence for this question. The \
questions you receive are usually comparative — treat the district as the \
unit of analysis:
- When asked what is "normal" or "typical", describe the pattern across \
districts, then name which districts match it and which deviate, district \
by district.
- When asked "which districts …", answer as a roster: for each district, \
what the evidence shows, with citations — and list the districts whose \
documents show nothing on the topic.
- For quantitative questions (stipends, salaries, budgets), quote figures \
exactly as written, attribute each to its district and date, and do not \
compute averages or call something an outlier unless the evidence for the \
comparison is actually present. If coverage is too thin to support \
"abnormal" or "highest", say so.

Honesty about absence. "No evidence found" means this corpus retrieved \
nothing — NOT that the district does not do the thing. Say "no evidence in \
the retrieved documents", never "District X does not have such a policy". \
Corpus coverage is uneven (some districts publish far more than others, \
and some scanned documents are not yet readable), so absence is weak \
evidence at best.

Citation rule. Every factual claim must carry one or more markers [N] \
referring to the numbered evidence. Do not cite numbers that are not in \
the list. Do not pad from general knowledge. If the evidence cannot \
answer the question, say exactly that and stop.

Dates matter: policies change. Prefer the most recent evidence, and when \
older passages conflict with newer ones, present it as a change over \
time, with dates.

Tone: a careful analyst briefing a school-board watcher — precise, plain, \
district-by-district. Quote documents sparingly, only when exact wording \
is the point.`;

const CITE_RE = /\[(\d+)\]/g;

export interface Citation {
  index: number;
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  section_path: string;
  heading: string | null;
  source_url: string;
  snippet: string;
}

export interface AskResponse {
  text: string;
  citations: Citation[];
  empty_districts: string[];
  refused: boolean;
  input_tokens: number;
  output_tokens: number;
}

// Numbered, district-grouped evidence block + the chunks in [N] order.
function formatEvidence(panel: Panel): { block: string; ordered: EvidenceChunk[] } {
  const ordered: EvidenceChunk[] = [];
  const lines: string[] = [];
  let n = 0;
  for (const slug of Object.keys(panel.by_district).sort()) {
    lines.push(`### District: ${slug}`);
    for (const c of panel.by_district[slug]) {
      n += 1;
      ordered.push(c);
      const date = c.meeting_date ?? "undated";
      const head = c.heading ? ` — ${c.heading}` : "";
      lines.push(
        `[${n}] (${slug}, ${date}, ${c.doc_type ?? "document"}: ${c.doc_title}, §${c.section_path}${head})`
      );
      let body = c.content;
      if (body.length > MAX_CHUNK_CHARS) body = body.slice(0, MAX_CHUNK_CHARS) + " …[truncated]";
      lines.push(body);
      lines.push("");
    }
  }
  if (panel.empty_districts.length) {
    lines.push(
      `### Districts with NO evidence retrieved for this question: ${panel.empty_districts.join(", ")}`
    );
  }
  return { block: lines.join("\n").trimEnd(), ordered };
}

function buildUserMessage(question: string, evidence: string): string {
  return `QUESTION: ${question.trim()}\n\nEVIDENCE PANEL:\n\n${evidence}`;
}

function extractCitations(text: string): number[] {
  const out: number[] = [];
  let m;
  const re = new RegExp(CITE_RE.source, "g");
  while ((m = re.exec(text)) !== null) out.push(parseInt(m[1], 10));
  return out;
}

function buildCitations(ordered: EvidenceChunk[]): Citation[] {
  return ordered.map((c, i) => ({
    index: i + 1,
    chunk_id: c.chunk_id,
    district: c.district,
    meeting_date: c.meeting_date,
    doc_type: c.doc_type,
    doc_title: c.doc_title,
    section_path: c.section_path,
    heading: c.heading,
    source_url: c.source_url,
    snippet: c.content.slice(0, 220),
  }));
}

async function callClaude(userMsg: string): Promise<string> {
  const resp = await getClient().messages.create({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: userMsg }],
  });
  return resp.content
    .filter((b) => b.type === "text")
    .map((b) => (b as Anthropic.TextBlock).text)
    .join("");
}

export async function* synthesizeStream(
  panel: Panel
): AsyncGenerator<
  { type: "token"; text: string } | { type: "done"; response: AskResponse }
> {
  const { block, ordered } = formatEvidence(panel);

  if (ordered.length === 0) {
    yield {
      type: "done",
      response: {
        text: "No evidence in the retrieved documents matched this question.",
        citations: [],
        empty_districts: panel.empty_districts,
        refused: true,
        input_tokens: 0,
        output_tokens: 0,
      },
    };
    return;
  }

  const userMsg = buildUserMessage(panel.question, block);
  const stream = getClient().messages.stream({
    model: MODEL,
    max_tokens: MAX_TOKENS,
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: userMsg }],
  });

  let fullText = "";
  for await (const event of stream) {
    if (event.type === "content_block_delta" && event.delta.type === "text_delta") {
      fullText += event.delta.text;
      yield { type: "token", text: event.delta.text };
    }
  }

  const finalMessage = await stream.finalMessage();
  let inputTokens = finalMessage.usage.input_tokens;
  let outputTokens = finalMessage.usage.output_tokens;

  // Citation validation: markers must reference real evidence. One retry.
  const valid = new Set(ordered.map((_, i) => i + 1));
  let cited = extractCitations(fullText);
  const bad = cited.filter((n) => !valid.has(n));
  if (bad.length > 0) {
    const reminder =
      `\n\nNOTE: your previous response cited [${[...new Set(bad)].sort((a, b) => a - b)}] ` +
      `which don't exist. Valid markers are 1..${ordered.length}. Rewrite without inventing markers.`;
    fullText = await callClaude(userMsg + reminder);
    cited = extractCitations(fullText);
    const stillBad = cited.filter((n) => !valid.has(n));
    if (stillBad.length > 0) {
      throw new Error(
        `Hallucinated citation markers persisted after retry: ${JSON.stringify([...new Set(stillBad)].sort((a, b) => a - b))}`
      );
    }
    inputTokens = 0;
    outputTokens = 0;
  }

  const refused =
    fullText.toLowerCase().includes("cannot answer") && !CITE_RE.test(fullText);

  yield {
    type: "done",
    response: {
      text: fullText.trim(),
      citations: buildCitations(ordered),
      empty_districts: panel.empty_districts,
      refused,
      input_tokens: inputTokens,
      output_tokens: outputTokens,
    },
  };
}
