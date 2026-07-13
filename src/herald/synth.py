# ============================================================
# FORK TODO (herald): rewrite persona/attribution prompt for district (not paper) attribution
# Inherited from Herald; rewrite for this corpus before use.
# See docs/ENGINE_EXTRACTION.md.
# ============================================================
"""Claude-Sonnet synthesis over retrieved chunks.

Given a question and a ranked list of ``RetrievedChunk``s from the
hybrid retriever, build the prompt described in PLAN.md §9 and call
Claude. After the response, validate every ``[N]`` citation marker
against the chunk dossier we sent. Hallucinated markers trigger one
retry with a stronger reminder; if a hallucinated marker survives
the retry the response is surfaced as a hard error rather than as a
wrong answer.

Synthesis model: Claude Sonnet 4.6, per PLAN §9.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from anthropic import AsyncAnthropic

from herald.retrieval import RetrievedChunk

DEFAULT_MODEL = "claude-sonnet-4-6"
DEFAULT_MAX_TOKENS = 2500
DEFAULT_TEMPERATURE = 0.2

_CITE_RE = re.compile(r"\[(\d+)\]")

# Two papers in the demo corpus. Mentioned by name in the system prompt
# so Claude knows the editorial vantages; falls back gracefully if the
# corpus expands.
SYSTEM_PROMPT = """\
You are a research assistant grounded in a specific newspaper corpus. \
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
here is what little it does say: ..." Better to be small than wrong.\
"""


@dataclass(frozen=True)
class SynthesizedAnswer:
    """The output of one synthesis call.

    ``citations`` is the ordered list of chunk UUIDs corresponding to
    [1], [2], ... in the source dossier we sent. The application layer
    uses this to map inline [N] markers in ``text`` back to page-image
    URLs at render time.
    """

    text: str
    citations: list[UUID]
    refused: bool          # heuristic — model emitted the refusal pattern
    cited_indices: list[int]  # 1-indexed marker numbers actually emitted
    input_tokens: int
    output_tokens: int


class SynthError(RuntimeError):
    """Non-recoverable synthesis failure (e.g. hallucinated citations
    persisting after one retry)."""


class Synthesizer:
    """Wraps the Anthropic SDK with the cite-or-refuse prompt + validator."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
        client: AsyncAnthropic | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Anthropic api_key is required")
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._client = client or AsyncAnthropic(api_key=api_key)

    async def answer(
        self,
        question: str,
        chunks: list[RetrievedChunk],
    ) -> SynthesizedAnswer:
        """Synthesize an answer for ``question`` over ``chunks``.

        When ``chunks`` is empty we emit a deterministic refusal without
        calling Claude (saves a token bill on bad upstream queries).
        """
        if not chunks:
            return SynthesizedAnswer(
                text=(
                    "The corpus does not have enough to support a confident "
                    "answer — no passages matched this question."
                ),
                citations=[],
                refused=True,
                cited_indices=[],
                input_tokens=0,
                output_tokens=0,
            )

        user_msg = _build_user_message(question, chunks)
        text, in_tok, out_tok = await self._call_claude(user_msg)

        valid_indices = set(range(1, len(chunks) + 1))
        cited = _extract_citation_indices(text)
        bad = [n for n in cited if n not in valid_indices]

        if bad:
            # One retry with an explicit reminder of valid indices.
            reminder = (
                f"\n\nNOTE: your previous response cited chunk numbers "
                f"{sorted(set(bad))} that don't exist. Valid chunk "
                f"numbers are 1..{len(chunks)}. Rewrite your answer "
                "without inventing chunk numbers."
            )
            text, in2, out2 = await self._call_claude(user_msg + reminder)
            in_tok += in2
            out_tok += out2
            cited = _extract_citation_indices(text)
            still_bad = [n for n in cited if n not in valid_indices]
            if still_bad:
                raise SynthError(
                    f"hallucinated citation markers persisted after retry: "
                    f"{sorted(set(still_bad))}"
                )

        citation_uuids = [chunks[n - 1].chunk_id for n in cited]
        refused = _looks_like_refusal(text)
        return SynthesizedAnswer(
            text=text.strip(),
            citations=citation_uuids,
            refused=refused,
            cited_indices=cited,
            input_tokens=in_tok,
            output_tokens=out_tok,
        )

    async def _call_claude(self, user_message: str) -> tuple[str, int, int]:
        resp = await self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        # Concatenate text blocks; ignore any non-text blocks Claude
        # might emit (tool use, etc.). Use getattr so we don't have to
        # import every concrete block type the SDK might return.
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                if isinstance(text, str):
                    text_parts.append(text)
        text = "".join(text_parts)
        usage = resp.usage
        in_tok = getattr(usage, "input_tokens", 0)
        out_tok = getattr(usage, "output_tokens", 0)
        return text, in_tok, out_tok


def _build_user_message(question: str, chunks: list[RetrievedChunk]) -> str:
    """Construct the SOURCES dossier exactly as described in PLAN §9."""
    lines = [f"QUESTION: {question.strip()}", "", "SOURCES:"]
    for i, c in enumerate(chunks, start=1):
        cite = (
            f"[{i}] {c.paper_title}, {c.date_issued.isoformat()}, "
            f"p.{c.page_sequence} ed-{c.edition}"
        )
        # Truncate very long chunks defensively — Sonnet handles long
        # context but our chunks are ~400 words by design.
        snippet = c.content.strip()
        if len(snippet) > 4000:
            snippet = snippet[:4000] + " ..."
        lines.append(f"{cite}")
        lines.append(f"    {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip()


def _extract_citation_indices(text: str) -> list[int]:
    """Return the ordered list of [N] markers in ``text`` (with duplicates)."""
    return [int(m.group(1)) for m in _CITE_RE.finditer(text)]


def _looks_like_refusal(text: str) -> bool:
    """Heuristic: did Claude emit the PLAN §9 refusal phrasing with no citations?

    Used for analytics / UI hinting only; doesn't gate any behavior.
    Requires BOTH the canonical phrase AND zero citation markers — a
    response that contains the phrase but still cites sources is
    substantive, not a refusal.
    """
    needle = "does not have enough to support a confident answer"
    has_phrase = needle.lower() in text.lower()
    has_citations = bool(_CITE_RE.search(text))
    return has_phrase and not has_citations
