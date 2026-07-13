"""Tests for the Phase 1 eval harness."""

from __future__ import annotations

from datetime import date
from uuid import UUID

import pytest

from herald.eval import (
    EVAL_QUESTIONS,
    EvalRunResult,
    format_results_markdown,
    run_eval,
)
from herald.retrieval import RetrievedChunk
from herald.synth import SynthesizedAnswer


def _chunk(n: int) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=UUID(int=n),
        content=f"chunk {n} text",
        paper_lccn="sn83030213",
        paper_title="New-York Daily Tribune",
        date_issued=date(1845, 8, 9),
        edition=1,
        page_sequence=n,
        image_url=f"https://chroniclingamerica.loc.gov/.../seq-{n}.jpg",
        resource_url=f"https://www.loc.gov/resource/sn83030213/1845-08-09/ed-1/seq-{n}",
        score=0.9,
        rrf_score=0.05,
        rerank_score=0.9,
    )


def _answer(text: str, cited: list[int]) -> SynthesizedAnswer:
    return SynthesizedAnswer(
        text=text,
        citations=[UUID(int=n) for n in cited],
        refused="does not have enough" in text.lower(),
        cited_indices=cited,
        input_tokens=100,
        output_tokens=80,
    )


# ---- question set integrity -----------------------------------------

def test_eval_set_has_ten_questions():
    assert len(EVAL_QUESTIONS) == 10


def test_eval_set_question_numbers_are_1_through_10():
    assert [q.number for q in EVAL_QUESTIONS] == list(range(1, 11))


def test_exactly_one_negative_case():
    """Per PLAN §12 only Q9 is the refusal/negative case."""
    negatives = [q for q in EVAL_QUESTIONS if q.is_negative]
    assert len(negatives) == 1
    assert negatives[0].number == 9
    assert "helium" in negatives[0].text.lower()


def test_eval_questions_are_frozen():
    """EvalQuestion is intended to be immutable — accidentally mutating
    one would screw up cross-run comparisons."""
    import dataclasses
    q = EVAL_QUESTIONS[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        q.number = 99  # type: ignore[misc]


# ---- run_eval orchestration ------------------------------------------

class _FakeRetriever:
    def __init__(self, *, chunks_per_q: int = 3) -> None:
        self.calls: list[str] = []
        self._chunks_per_q = chunks_per_q

    async def retrieve(self, query: str, **kwargs: object) -> list[RetrievedChunk]:
        self.calls.append(query)
        return [_chunk(i + 1) for i in range(self._chunks_per_q)]


class _FakeSynth:
    """Returns a deterministic answer per question."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def answer(
        self, question: str, chunks: list[RetrievedChunk]
    ) -> SynthesizedAnswer:
        self.calls.append((question, len(chunks)))
        if "helium" in question.lower():
            return _answer(
                "The corpus does not have enough to support a confident answer.",
                cited=[],
            )
        return _answer(f"Answer about: {question[:30]} [1] [2].", cited=[1, 2])


@pytest.mark.asyncio
async def test_run_eval_runs_each_question_once():
    retr = _FakeRetriever()
    synth = _FakeSynth()
    results = await run_eval(retriever=retr, synthesizer=synth)  # type: ignore[arg-type]
    assert len(results) == 10
    # One retrieve + one synth call per question
    assert len(retr.calls) == 10
    assert len(synth.calls) == 10


@pytest.mark.asyncio
async def test_run_eval_passes_through_date_filters():
    retr = _FakeRetriever()
    synth = _FakeSynth()
    captured: list[dict] = []

    orig = retr.retrieve

    async def spy(query: str, **kwargs: object) -> list[RetrievedChunk]:
        captured.append(dict(kwargs))
        return await orig(query, **kwargs)
    retr.retrieve = spy  # type: ignore[method-assign]

    await run_eval(
        retriever=retr, synthesizer=synth,  # type: ignore[arg-type]
        date_from=date(1842, 4, 22), date_to=date(1846, 12, 31),
        questions=[EVAL_QUESTIONS[0]],
    )
    assert captured[0]["date_from"] == date(1842, 4, 22)
    assert captured[0]["date_to"] == date(1846, 12, 31)


@pytest.mark.asyncio
async def test_run_eval_subset_runs_only_chosen_questions():
    retr = _FakeRetriever()
    synth = _FakeSynth()
    results = await run_eval(
        retriever=retr, synthesizer=synth,  # type: ignore[arg-type]
        questions=[EVAL_QUESTIONS[0], EVAL_QUESTIONS[8]],  # Q1 + Q9 (helium)
    )
    assert [r.question.number for r in results] == [1, 9]
    assert results[1].answer.refused is True


@pytest.mark.asyncio
async def test_run_eval_invokes_progress_callback():
    retr = _FakeRetriever()
    synth = _FakeSynth()
    messages: list[str] = []
    await run_eval(
        retriever=retr, synthesizer=synth,  # type: ignore[arg-type]
        questions=[EVAL_QUESTIONS[0], EVAL_QUESTIONS[1]],
        on_progress=lambda m: messages.append(m),
    )
    assert len(messages) == 2
    assert messages[0].startswith("Q1:")
    assert messages[1].startswith("Q2:")


# ---- markdown formatter ----------------------------------------------

def _mk_result(
    qnum: int, *, text: str = "Answer [1] [2].", cited: list[int] | None = None,
    hits: int = 3,
) -> EvalRunResult:
    q = next(q for q in EVAL_QUESTIONS if q.number == qnum)
    return EvalRunResult(
        question=q,
        hits=[_chunk(i + 1) for i in range(hits)],
        answer=_answer(text, cited if cited is not None else [1, 2]),
    )


def test_format_results_markdown_contains_question_block_per_result():
    md = format_results_markdown([_mk_result(1), _mk_result(2)])
    assert "## Q1" in md
    assert "## Q2" in md
    assert "Calico Indians" in md  # from Q1's question text


def test_format_results_markdown_marks_negative_case():
    md = format_results_markdown([_mk_result(9, cited=[])])
    assert "## Q9 (negative case)" in md


def test_format_results_markdown_emits_image_urls_for_cited_chunks():
    r = _mk_result(1, text="Answer [1] [3].", cited=[1, 3])
    md = format_results_markdown([r])
    assert "**[1]**" in md
    assert "**[3]**" in md
    # The chunk-2 image URL must NOT appear since Claude didn't cite [2]
    assert "seq-2.jpg" not in md
    assert "seq-1.jpg" in md and "seq-3.jpg" in md


def test_format_results_markdown_handles_no_citations():
    r = _mk_result(1, text="Plain prose, no markers.", cited=[])
    md = format_results_markdown([r])
    assert "**Sources cited:** none." in md


def test_format_results_markdown_notes_refusal_for_negative_case():
    r = _mk_result(
        9,
        text="The corpus does not have enough to support a confident answer.",
        cited=[],
    )
    md = format_results_markdown([r])
    assert "refusal — expected" in md


def test_format_results_markdown_includes_date_window():
    md = format_results_markdown(
        [_mk_result(1)],
        date_from=date(1842, 4, 22),
        date_to=date(1846, 12, 31),
    )
    assert "1842-04-22" in md and "1846-12-31" in md


def test_format_results_markdown_handles_empty_results():
    md = format_results_markdown([])
    assert "Herald Phase 1 Eval Run" in md
    assert "Questions: 0" in md
