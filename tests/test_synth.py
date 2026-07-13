"""Tests for the Claude-Sonnet synthesizer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest

from herald.retrieval import RetrievedChunk
from herald.synth import (
    SynthError,
    Synthesizer,
    _build_user_message,
    _extract_citation_indices,
    _looks_like_refusal,
)

CHUNK_A = UUID(int=1)
CHUNK_B = UUID(int=2)
CHUNK_C = UUID(int=3)


def _chunk(cid: UUID, content: str, seq: int = 1) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=cid, content=content,
        paper_lccn="sn83030213", paper_title="New-York Daily Tribune",
        date_issued=date(1845, 8, 9), edition=1, page_sequence=seq,
        image_url=f"https://x/seq-{seq}.jpg",
        resource_url=f"https://www.loc.gov/resource/sn83030213/1845-08-09/ed-1/seq-{seq}",
        score=0.9, rrf_score=0.05, rerank_score=0.9,
    )


# ---- pure helpers ----------------------------------------------------

def test_extract_citation_indices_finds_all_markers_in_order():
    text = "The Tribune notes anti-rent unrest [1]. Later, [3] and [1] again."
    assert _extract_citation_indices(text) == [1, 3, 1]


def test_extract_citation_indices_ignores_non_citation_brackets():
    text = "List items [a], [b]. Number marker [2]."
    assert _extract_citation_indices(text) == [2]


def test_extract_citation_indices_empty_when_no_markers():
    assert _extract_citation_indices("plain prose with no markers") == []


def test_looks_like_refusal_true_for_canonical_phrasing():
    assert _looks_like_refusal(
        "The corpus does not have enough to support a confident answer..."
    )


def test_looks_like_refusal_false_for_normal_answer():
    assert not _looks_like_refusal("The Tribune reports [1]...")


def test_build_user_message_format():
    msg = _build_user_message(
        "what does the Tribune say?",
        [_chunk(CHUNK_A, "Anti-rent meeting at Berne."),
         _chunk(CHUNK_B, "Calico Indians disguise.", seq=2)],
    )
    assert msg.startswith("QUESTION: what does the Tribune say?")
    assert "SOURCES:" in msg
    assert "[1] New-York Daily Tribune, 1845-08-09" in msg
    assert "[2] New-York Daily Tribune, 1845-08-09" in msg
    assert "Anti-rent meeting at Berne." in msg
    assert "Calico Indians disguise." in msg


def test_build_user_message_truncates_very_long_chunks():
    huge = "x" * 5000
    msg = _build_user_message("q", [_chunk(CHUNK_A, huge)])
    assert "..." in msg
    # The huge body is capped to 4000 chars in the output.
    assert msg.count("x") <= 4001


# ---- Synthesizer with a fake Anthropic client -------------------------

@dataclass
class _FakeUsage:
    input_tokens: int = 100
    output_tokens: int = 50


@dataclass
class _FakeBlock:
    type: str
    text: str


@dataclass
class _FakeResponse:
    content: list[_FakeBlock]
    usage: _FakeUsage


class FakeAnthropic:
    """Records call args and returns scripted responses in order."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.messages = self  # so .messages.create works

    async def create(self, **kwargs) -> _FakeResponse:
        self.calls.append(kwargs)
        if not self._responses:
            raise RuntimeError("FakeAnthropic ran out of scripted responses")
        text = self._responses.pop(0)
        return _FakeResponse(
            content=[_FakeBlock(type="text", text=text)],
            usage=_FakeUsage(),
        )


@pytest.mark.asyncio
async def test_answer_happy_path_with_valid_citations():
    fake = FakeAnthropic(
        ["The Tribune reports anti-rent unrest [1] and Calico Indian disguises [2]."]
    )
    s = Synthesizer(api_key="k", client=fake)  # type: ignore[arg-type]
    out = await s.answer(
        "what does the Tribune say?",
        [_chunk(CHUNK_A, "Anti-rent."), _chunk(CHUNK_B, "Calico Indians.")],
    )
    assert out.cited_indices == [1, 2]
    assert out.citations == [CHUNK_A, CHUNK_B]
    assert not out.refused
    assert out.input_tokens == 100 and out.output_tokens == 50
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_answer_no_chunks_returns_deterministic_refusal_no_api_call():
    fake = FakeAnthropic([])  # empty -- shouldn't be called
    s = Synthesizer(api_key="k", client=fake)  # type: ignore[arg-type]
    out = await s.answer("anything", [])
    assert out.refused
    assert out.citations == []
    assert out.cited_indices == []
    assert "does not have enough" in out.text.lower()
    assert len(fake.calls) == 0


@pytest.mark.asyncio
async def test_answer_retries_once_on_hallucinated_citation():
    """First response cites [3] when only 2 chunks exist -> retry with reminder
    -> second response cites only [1], passes validation."""
    fake = FakeAnthropic([
        "Per the Tribune [1] and another source [3].",  # [3] invalid
        "Per the Tribune [1].",                          # valid
    ])
    s = Synthesizer(api_key="k", client=fake)  # type: ignore[arg-type]
    out = await s.answer("q", [_chunk(CHUNK_A, "x"), _chunk(CHUNK_B, "y")])
    assert out.cited_indices == [1]
    assert out.citations == [CHUNK_A]
    assert len(fake.calls) == 2
    # Reminder text appears in the second call's user message
    second_user = fake.calls[1]["messages"][0]["content"]
    assert "don't exist" in second_user
    assert "Valid chunk numbers are 1..2" in second_user


@pytest.mark.asyncio
async def test_answer_raises_when_hallucination_persists_after_retry():
    fake = FakeAnthropic([
        "Bad [9].",  # invalid
        "Still bad [9] [10].",  # still invalid
    ])
    s = Synthesizer(api_key="k", client=fake)  # type: ignore[arg-type]
    with pytest.raises(SynthError, match="hallucinated"):
        await s.answer("q", [_chunk(CHUNK_A, "x")])


@pytest.mark.asyncio
async def test_answer_not_refused_when_canonical_phrase_present_with_citations():
    fake = FakeAnthropic([
        "The corpus does not have enough to support a confident answer — "
        "here is what little it does say: not much [1]."
    ])
    s = Synthesizer(api_key="k", client=fake)  # type: ignore[arg-type]
    out = await s.answer("q", [_chunk(CHUNK_A, "x")])
    assert out.refused is False
    assert out.cited_indices == [1]


@pytest.mark.asyncio
async def test_answer_sends_correct_model_and_system_prompt():
    fake = FakeAnthropic(["Answer [1]."])
    s = Synthesizer(api_key="k", client=fake, model="claude-sonnet-4-6")  # type: ignore[arg-type]
    await s.answer("q", [_chunk(CHUNK_A, "x")])
    call = fake.calls[0]
    assert call["model"] == "claude-sonnet-4-6"
    assert "Citation rule" in call["system"]
    assert "cite-or-refuse" not in call["system"]  # phrase appears nowhere — sanity
    # Wait — the system prompt actually says "Citation rule." which we did check above.
    assert "Refusal floor" in call["system"]


@pytest.mark.asyncio
async def test_constructor_requires_api_key():
    with pytest.raises(ValueError):
        Synthesizer(api_key="")
