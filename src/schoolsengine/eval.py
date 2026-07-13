"""Phase 1 eval harness — run the 10 Anti-Rent validation questions.

Per PLAN §12, Phase 1 isn't done until the CLI produces well-cited,
factually defensible answers to all 10 questions. This module bundles
them and provides ``run_eval()`` + a Markdown report formatter for
hand-grading.

The questions are hardcoded (not in a data file) so the eval-harness
contract lives in version control alongside the code that consumes it.
If we ever change the question set, that's a real-and-versioned change.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from io import StringIO

from schoolsengine.retrieval import HybridRetriever, RetrievedChunk
from schoolsengine.synth import SynthesizedAnswer, Synthesizer


@dataclass(frozen=True)
class EvalQuestion:
    """One validation question from PLAN §12."""

    number: int
    text: str
    is_negative: bool = False  # True for the helium refusal case
    notes: str = ""             # what the grader should look for


EVAL_QUESTIONS: tuple[EvalQuestion, ...] = (
    EvalQuestion(
        number=1,
        text=(
            "Find references to the Helderberg disturbances and the Calico "
            "Indians in the newspaper corpus, 1842-1846."
        ),
        notes=(
            "Basic semantic recall on the corpus's signature topic. "
            "Should surface chunks across multiple dates, ideally both papers "
            "if both are present."
        ),
    ),
    EvalQuestion(
        number=2,
        text=(
            "How does the Tribune characterize tenants versus landlords? "
            "Quote phrasings."
        ),
        notes=(
            "Cross-paper synthesis when Albany Evening Journal is also "
            "ingested. FTS leg must carry exact-phrase weight. "
            "Look for actual quoted phrasings, not paraphrase."
        ),
    ),
    EvalQuestion(
        number=3,
        text=(
            "Trace the newspaper coverage of Stephen Van Rensselaer III's "
            "death and its aftermath as the corpus discusses it."
        ),
        notes=(
            "Van Rensselaer III died Jan 1839 (pre-corpus); coverage should "
            "be about his heirs pressing back-rent claims and the resulting "
            "tenant resistance."
        ),
    ),
    EvalQuestion(
        number=4,
        text=(
            "What language do the papers use for tenant violence vs landlord "
            "property claims? Quote specific phrasings."
        ),
        notes=(
            "Close-reading via exact-quote retrieval. The phrasings "
            "themselves are the deliverable; this isn't a paraphrase task."
        ),
    ),
    EvalQuestion(
        number=5,
        text=(
            "Identify the named anti-rent leaders who appear repeatedly in "
            "the corpus, and describe how each is characterized."
        ),
        notes=(
            "Entity recall. Expected names: Smith Boughton / 'Big Thunder', "
            "Moses Earle, Osman Steele, Silas Wright, John Young, Stephen "
            "Van Rensselaer IV. Coverage will vary by date window."
        ),
    ),
    EvalQuestion(
        number=6,
        text=(
            "How do the papers report the killing of Sheriff Osman Steele "
            "at Andes in August 1845?"
        ),
        notes=(
            "Precise event-level retrieval; date-filtered slice should also "
            "work. Steele died Aug 7, 1845 at Moses Earle's farm."
        ),
    ),
    EvalQuestion(
        number=7,
        text=(
            "How is Governor Silas Wright's 1845 anti-disguise law and "
            "crackdown covered? Does the Tribune lean a particular way?"
        ),
        notes="Political/editorial-stance extraction.",
    ),
    EvalQuestion(
        number=8,
        text=(
            "Do the Anti-Rent Wars share newspaper real estate with national "
            "stories like the 1844 election or Texas annexation? What gets "
            "bumped, what gets prominence?"
        ),
        notes="Cross-topic synthesis and page-prominence reasoning.",
    ),
    EvalQuestion(
        number=9,
        text="What does the corpus say about the discovery of helium?",
        is_negative=True,
        notes=(
            "Hard refusal test. Helium was discovered in 1868 — post-corpus. "
            "The model MUST refuse gracefully. A confident answer here is a "
            "release blocker. Citation-validator should also flush hallucinated "
            "marker IDs if any appear."
        ),
    ),
    EvalQuestion(
        number=10,
        text=(
            "Find any reference to a specific upstate town — Berne, "
            "Rensselaerville, Andes, or Delhi — and summarize what the papers "
            "say happened there."
        ),
        notes=(
            "The demo's emotional pitch: 'what's our town's version of this "
            "story?' At least one of these towns should yield real hits."
        ),
    ),
)


@dataclass(frozen=True)
class EvalRunResult:
    """The outcome of running one eval question."""

    question: EvalQuestion
    hits: list[RetrievedChunk]
    answer: SynthesizedAnswer


async def run_eval(
    *,
    retriever: HybridRetriever,
    synthesizer: Synthesizer,
    questions: list[EvalQuestion] | tuple[EvalQuestion, ...] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    final_top: int = 12,
    rerank: bool = True,
    on_progress: object = None,
) -> list[EvalRunResult]:
    """Run each question through retrieval + synthesis, collect outputs.

    Errors from a single question are caught and logged via ``on_progress``;
    the run continues so a partial failure doesn't waste the rest. (We're
    spending money on every Anthropic call.)
    """
    qs = list(questions or EVAL_QUESTIONS)
    results: list[EvalRunResult] = []
    for q in qs:
        if callable(on_progress):
            on_progress(f"Q{q.number}: {q.text[:60]}...")
        hits = await retriever.retrieve(
            q.text,
            date_from=date_from,
            date_to=date_to,
            final_top=final_top,
            rerank=rerank,
        )
        ans = await synthesizer.answer(q.text, hits)
        results.append(EvalRunResult(question=q, hits=hits, answer=ans))
    return results


# ---- markdown report ------------------------------------------------

def format_results_markdown(
    results: list[EvalRunResult],
    *,
    title: str = "Herald Phase 1 Eval Run",
    date_from: date | None = None,
    date_to: date | None = None,
) -> str:
    """Format eval results as a Markdown document for hand-grading."""
    buf = StringIO()
    buf.write(f"# {title}\n\n")
    if date_from or date_to:
        df = date_from.isoformat() if date_from else "open"
        dt = date_to.isoformat() if date_to else "open"
        buf.write(f"Date window: `{df}` → `{dt}`\n\n")
    total_in = sum(r.answer.input_tokens for r in results)
    total_out = sum(r.answer.output_tokens for r in results)
    buf.write(
        f"Questions: {len(results)} · "
        f"tokens in/out: {total_in}/{total_out}\n\n"
    )

    for r in results:
        q = r.question
        buf.write("---\n\n")
        marker = " (negative case)" if q.is_negative else ""
        buf.write(f"## Q{q.number}{marker}\n\n")
        buf.write(f"> {q.text}\n\n")
        if q.notes:
            buf.write(f"*Grader notes:* {q.notes}\n\n")
        buf.write(f"**Hits retrieved:** {len(r.hits)}\n\n")
        buf.write("**Answer:**\n\n")
        buf.write(r.answer.text.strip() or "_(no text)_")
        buf.write("\n\n")

        # Sources cited list — unique chunks in the order Claude first cited them.
        if r.answer.cited_indices:
            buf.write(f"**Sources cited** ({len(set(r.answer.cited_indices))} unique):\n\n")
            seen: set[int] = set()
            for n in r.answer.cited_indices:
                if n in seen or n - 1 >= len(r.hits):
                    continue
                seen.add(n)
                h = r.hits[n - 1]
                buf.write(
                    f"- **[{n}]** {h.paper_title}, "
                    f"{h.date_issued.isoformat()}, p.{h.page_sequence} — "
                    f"<{h.image_url}>\n"
                )
            buf.write("\n")
        elif q.is_negative and r.answer.refused:
            buf.write("**Sources cited:** none (refusal — expected for this question).\n\n")
        else:
            buf.write("**Sources cited:** none.\n\n")

        flag_bits = [
            f"tokens={r.answer.input_tokens}/{r.answer.output_tokens}",
            f"refused={r.answer.refused}",
            f"cited_markers={len(r.answer.cited_indices)}",
        ]
        buf.write(f"*Stats:* {' · '.join(flag_bits)}\n\n")

    return buf.getvalue()
