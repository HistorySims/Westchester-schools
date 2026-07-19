"""Ask the schools corpus a question: panel retrieval → cited synthesis.

Built for the questions this project actually gets asked — "what's the
normal cell-phone policy?", "which districts are doing Middle States
accreditation?", "who pays coaches unusually much?" — which are
*comparative* questions where the district is the unit of analysis.
Evidence arrives as a per-district panel (see ``schools_retrieval``), the
synthesis prompt is told which districts produced nothing, and the answer
must treat absence honestly: "no evidence found" is a finding, not a gap
to paper over.

Citations: every claim carries ``[N]`` markers resolved against the
evidence list; hallucinated markers trigger one retry, then a hard error
(inherited from the newspaper engine's validator — it worked).
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import os
import re
from dataclasses import dataclass

import typer
from rich.console import Console

from herald.embed import VoyageEmbedder
from herald.rerank import VoyageReranker
from herald.schools_retrieval import (
    DEFAULT_MAX_PER_DOC,
    DEFAULT_PER_DISTRICT,
    DEFAULT_POOL,
    EvidenceChunk,
    Panel,
    retrieve_panel,
)

console = Console()

DEFAULT_MODEL = "claude-sonnet-5"
# max_tokens caps thinking + visible text *combined*. claude-sonnet-5 runs
# adaptive thinking by default (when `thinking` is unset), and that reasoning
# is billed against this budget — too low a cap truncates the visible answer
# mid-sentence. 16000 leaves ample room and stays under the SDK's non-streaming
# HTTP timeout. We deliberately don't set `thinking`: omitting it is portable —
# Sonnet 5 thinks adaptively (good for the comparative reasoning), Haiku/Opus
# simply don't, and none of them reject the request.
DEFAULT_MAX_TOKENS = 16000
MAX_CHUNK_CHARS = 1800          # per-chunk cap in the prompt (keep panels affordable)

_CITE_RE = re.compile(r"\[(\d+)\]")

SYSTEM_PROMPT = """\
You are a research assistant grounded in a corpus of public school-district \
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
is the point.\
"""


# ---- evidence formatting ----------------------------------------------

def format_evidence(panel: Panel) -> tuple[str, list[EvidenceChunk]]:
    """Numbered, district-grouped evidence block + the chunks in [N] order."""
    ordered: list[EvidenceChunk] = []
    lines: list[str] = []
    n = 0
    for slug in sorted(panel.by_district):
        lines.append(f"### District: {slug}")
        for c in panel.by_district[slug]:
            n += 1
            ordered.append(c)
            date = c.meeting_date.isoformat() if c.meeting_date else "undated"
            head = f" — {c.heading}" if c.heading else ""
            lines.append(
                f"[{n}] ({slug}, {date}, {c.doc_type or 'document'}: "
                f"{c.doc_title}, §{c.section_path}{head})"
            )
            body = c.content
            if len(body) > MAX_CHUNK_CHARS:
                body = body[:MAX_CHUNK_CHARS] + " …[truncated]"
            lines.append(body)
            lines.append("")
    if panel.empty_districts:
        lines.append(
            "### Districts with NO retrieved evidence for this question: "
            + ", ".join(panel.empty_districts)
        )
    return "\n".join(lines), ordered


def build_user_prompt(panel: Panel) -> tuple[str, list[EvidenceChunk]]:
    evidence, ordered = format_evidence(panel)
    prompt = (
        f"Question: {panel.question}\n\n"
        f"Evidence panel ({len(ordered)} passages, grouped by district):\n\n"
        f"{evidence}\n\n"
        "Answer the question per the panel instructions in your system "
        "prompt. Every claim needs [N] citations."
    )
    return prompt, ordered


def validate_citations(text: str, n_evidence: int) -> list[int]:
    """Return the invalid [N] markers (out of range); empty list = clean."""
    cited = {int(m) for m in _CITE_RE.findall(text)}
    return sorted(m for m in cited if m < 1 or m > n_evidence)


# ---- synthesis ---------------------------------------------------------

# Approximate USD per million tokens (input, output), for a rough per-question
# cost readout. Sonnet 5 is on introductory pricing through 2026-08-31 ($2/$10),
# reverting to $3/$15 after; update when it does. Voyage embed/rerank is a
# fraction of a cent and omitted.
_PRICING: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (2.0, 10.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-haiku-4-5": (1.0, 5.0),
}


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    """Rough USD cost of one synthesis, or None if the model's price is unknown."""
    price = _PRICING.get(model) or _PRICING.get(model.rsplit("-", 1)[0])
    if not price:
        return None
    return input_tokens / 1e6 * price[0] + output_tokens / 1e6 * price[1]


@dataclass
class Answer:
    text: str
    panel: Panel
    evidence: list[EvidenceChunk]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0    # includes adaptive-thinking tokens

    @property
    def cost_usd(self) -> float | None:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


class CitationError(RuntimeError):
    """A hallucinated citation marker survived the retry."""


async def synthesize(
    panel: Panel,
    *,
    api_key: str,
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> Answer:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    user_prompt, ordered = build_user_prompt(panel)
    messages = [{"role": "user", "content": user_prompt}]
    in_tok = out_tok = 0
    for attempt in (1, 2):
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=SYSTEM_PROMPT,
            messages=messages,
        )
        in_tok += resp.usage.input_tokens        # a retry adds to the tally
        out_tok += resp.usage.output_tokens
        text = "".join(b.text for b in resp.content if b.type == "text")
        bad = validate_citations(text, len(ordered))
        if not bad:
            return Answer(text=text, panel=panel, evidence=ordered, model=model,
                          input_tokens=in_tok, output_tokens=out_tok)
        if attempt == 1:
            messages = [
                {"role": "user", "content": user_prompt},
                {"role": "assistant", "content": text},
                {"role": "user", "content": (
                    f"Your answer cites markers that do not exist: {bad}. "
                    f"The evidence list has exactly {len(ordered)} passages, "
                    "numbered [1]..[{n}]. Rewrite the answer using only real "
                    "markers.".replace("{n}", str(len(ordered)))
                )},
            ]
    raise CitationError(f"hallucinated citation markers after retry: {bad}")


# ---- rendering ---------------------------------------------------------

def render_markdown(ans: Answer) -> str:
    lines = [
        f"# {ans.panel.question}",
        "",
        ans.text,
        "",
        "---",
        "",
        "## Evidence",
        "",
    ]
    for i, c in enumerate(ans.evidence, start=1):
        date = c.meeting_date.isoformat() if c.meeting_date else "undated"
        lines.append(
            f"**[{i}]** {c.district} · {date} · {c.doc_type or 'document'} · "
            f"{c.doc_title} · §{c.section_path}  \n"
            f"<{c.source_url}>"
        )
        lines.append("")
    if ans.panel.empty_districts:
        lines.append(
            f"_No evidence retrieved from: {', '.join(ans.panel.empty_districts)}._"
        )
        lines.append("")
    cost = ans.cost_usd
    cost_str = f", ~${cost:.3f}" if cost is not None else ""
    lines.append(
        f"_Model: {ans.model}. Tokens: {ans.input_tokens:,} in / "
        f"{ans.output_tokens:,} out{cost_str}. Answers only reflect the "
        "ingested corpus._"
    )
    return "\n".join(lines) + "\n"


def render_evidence_only(panel: Panel) -> str:
    evidence, ordered = format_evidence(panel)
    return (
        f"# Evidence panel: {panel.question}\n\n"
        f"_{len(ordered)} passages; retrieval only, no synthesis._\n\n"
        + evidence + "\n"
    )


# ---- CLI ---------------------------------------------------------------

app = typer.Typer(help="Ask the schools corpus a question.", no_args_is_help=True)


def _env(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        raise typer.BadParameter(f"{name} is not set.")
    return v


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to ask the corpus."),
    districts: str | None = typer.Option(
        None, help="Comma-separated district slugs (default: all)."
    ),
    doc_type: str | None = typer.Option(None, help="Only this doc type."),
    since: str | None = typer.Option(None, help="Earliest meeting date (YYYY-MM-DD)."),
    until: str | None = typer.Option(None, help="Latest meeting date (YYYY-MM-DD)."),
    per_district: int = typer.Option(
        DEFAULT_PER_DISTRICT, help="Evidence passages per district."
    ),
    pool: int = typer.Option(DEFAULT_POOL, help="Candidate pool per district per leg."),
    max_per_doc: int = typer.Option(
        DEFAULT_MAX_PER_DOC, help="Max evidence chunks from any one document."
    ),
    rerank: bool = typer.Option(True, help="Voyage rerank the fused pool."),
    evidence_only: bool = typer.Option(
        False, help="Print the retrieved panel without calling the synthesis model."
    ),
    model: str = typer.Option(DEFAULT_MODEL, help="Synthesis model."),
    report: str | None = typer.Option(None, help="Write the answer markdown here."),
) -> None:
    """Panel retrieval + cited synthesis over the corpus."""
    from pathlib import Path

    from herald import schools_db

    slugs = [s.strip() for s in (districts or "").split(",") if s.strip()] or None
    date_from = _dt.date.fromisoformat(since) if since else None
    date_to = _dt.date.fromisoformat(until) if until else None

    conn = schools_db.connect(_env("SUPABASE_DB_URL"))
    voyage_key = _env("VOYAGE_API_KEY")

    async def go() -> str:
        voyage = VoyageEmbedder(voyage_key)
        reranker = VoyageReranker(voyage_key) if rerank else None
        try:
            panel = await retrieve_panel(
                conn, voyage,
                question=question, reranker=reranker,
                per_district=per_district, pool=pool, max_per_doc=max_per_doc,
                districts=slugs, doc_type=doc_type,
                date_from=date_from, date_to=date_to,
            )
            n = sum(len(v) for v in panel.by_district.values())
            console.print(
                f"panel: {n} passages from {len(panel.by_district)} district(s); "
                f"empty: {', '.join(panel.empty_districts) or 'none'}"
            )
            if evidence_only:
                return render_evidence_only(panel)
            ans = await synthesize(panel, api_key=_env("ANTHROPIC_API_KEY"), model=model)
            return render_markdown(ans)
        finally:
            await voyage.aclose()
            if reranker is not None:
                await reranker.aclose()

    try:
        out = asyncio.run(go())
    finally:
        conn.close()

    console.print()
    console.print(out)
    if report:
        Path(report).write_text(out, encoding="utf-8")
        console.print(f"report: {report}")


if __name__ == "__main__":
    app()
