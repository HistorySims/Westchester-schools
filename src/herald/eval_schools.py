"""Graded questions for the schools corpus — does it still hold what it should?

Every failure this project has hit has the same shape: a document that is
absent, or present but unreadable, and an answer layer that reports the gap as
a fact about the world. "Only Tarrytowns denies credit at 18 absences" was
true of the corpus and false of Westchester.

Unit tests cannot catch that — the code was correct each time. What catches it
is asking the corpus a question whose answer is *known*, and checking the right
passage comes back.

Two deliberate choices:

* **Grade retrieval, not prose.** If the passage reaches the answer layer, the
  answer layer's job is a separate concern with separate tests. Retrieval is
  what has actually broken, and grading it is deterministic and free.
* **Presence is strong, absence is weak.** ``expect_present`` asserts a
  district demonstrably says something. ``no_known_rule`` records only that a
  full manual was read and no such sentence was found — which is *not* proof
  of absence, and is never graded as a required "no". The one place absence is
  enforced is a negative control, where the claim would be invented outright.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import typer
from rich.console import Console

DEFAULT_CASES = "data/eval/schools_cases.json"


def normalize(text: str) -> str:
    """Casefold + collapse whitespace, so a match is not lost to formatting.

    Real policy text carries non-breaking spaces (U+00A0) and hard line
    wraps. A match lost to formatting looks exactly like a missing
    document, which is the one mistake this suite exists to catch.
    """
    text = unicodedata.normalize("NFKC", text or "").replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip().casefold()


@dataclass(frozen=True)
class Expectation:
    """One district's known fact, and the strings that prove it was retrieved."""

    district: str
    must_match: tuple[str, ...]
    source: str = ""


@dataclass(frozen=True)
class EvalCase:
    id: str
    question: str
    kind: str = "coverage"
    doc_type: str | None = None
    why: str = ""
    expect_present: tuple[Expectation, ...] = ()
    no_known_rule: tuple[str, ...] = ()
    expect_no_evidence: bool = False


@dataclass
class ExpectationResult:
    expectation: Expectation
    passed: bool
    missing: list[str] = field(default_factory=list)
    chunks_seen: int = 0

    @property
    def detail(self) -> str:
        if self.passed:
            return "ok"
        if not self.chunks_seen:
            return "no evidence retrieved for this district"
        return "retrieved, but missing: " + ", ".join(repr(m) for m in self.missing)


@dataclass
class CaseResult:
    case: EvalCase
    results: list[ExpectationResult] = field(default_factory=list)
    unexpected_evidence: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        if self.case.expect_no_evidence and self.unexpected_evidence:
            return False
        return all(r.passed for r in self.results)

    @property
    def summary(self) -> str:
        if self.case.expect_no_evidence:
            n = len(self.unexpected_evidence)
            return "no evidence, as expected" if not n else f"{n} district(s) answered anyway"
        hit = sum(1 for r in self.results if r.passed)
        return f"{hit}/{len(self.results)} expectations met"


def load_cases(path: str | Path = DEFAULT_CASES) -> list[EvalCase]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    out: list[EvalCase] = []
    for c in raw["cases"]:
        out.append(EvalCase(
            id=c["id"],
            question=c["question"],
            kind=c.get("kind", "coverage"),
            doc_type=c.get("doc_type"),
            why=c.get("why", ""),
            expect_present=tuple(
                Expectation(
                    district=e["district"],
                    must_match=tuple(e["must_match"]),
                    source=e.get("source", ""),
                )
                for e in c.get("expect_present", [])
            ),
            no_known_rule=tuple(c.get("no_known_rule", ())),
            expect_no_evidence=bool(c.get("expect_no_evidence", False)),
        ))
    return out


def grade_case(case: EvalCase, by_district: dict[str, list[str]]) -> CaseResult:
    """Grade one case against retrieved chunk text, keyed by district slug.

    Pure: takes text, not a database. ``by_district`` is what the panel
    returned — every district it found evidence for, and that evidence's text.
    """
    res = CaseResult(case=case)
    for exp in case.expect_present:
        chunks = by_district.get(exp.district, [])
        haystack = normalize(" \n ".join(chunks))
        missing = [m for m in exp.must_match if normalize(m) not in haystack]
        res.results.append(ExpectationResult(
            expectation=exp,
            passed=not missing and bool(chunks),
            missing=missing,
            chunks_seen=len(chunks),
        ))
    if case.expect_no_evidence:
        res.unexpected_evidence = sorted(d for d, ch in by_district.items() if ch)
    return res


def render_report(results: list[CaseResult]) -> str:
    """Markdown for the run summary — failures first, with why each matters."""
    passed = [r for r in results if r.passed]
    failed = [r for r in results if not r.passed]
    lines = [
        "# Corpus eval",
        "",
        f"**{len(passed)}/{len(results)} cases passed.**",
        "",
        "| case | kind | result |",
        "|---|---|---|",
    ]
    for r in [*failed, *passed]:
        mark = "PASS" if r.passed else "**FAIL**"
        lines.append(f"| `{r.case.id}` | {r.case.kind} | {mark} — {r.summary} |")

    if failed:
        lines += ["", "## Failures", ""]
        for r in failed:
            lines += [f"### `{r.case.id}`", "", f"> {r.case.question}", ""]
            if r.case.why:
                lines += [f"*{r.case.why}*", ""]
            for er in r.results:
                if not er.passed:
                    src = f" ({er.expectation.source})" if er.expectation.source else ""
                    lines.append(f"- **{er.expectation.district}**{src} — {er.detail}")
            for d in r.unexpected_evidence:
                lines.append(f"- **{d}** returned evidence for a rule that does not exist")
            lines.append("")

    lines += [
        "",
        "_Presence expectations assert a district demonstrably says something. "
        "`no_known_rule` records only that a manual was read and no such sentence "
        "was found — never graded as a required \"no\"._",
    ]
    return "\n".join(lines) + "\n"


async def run_eval(
    conn,
    voyage,
    cases: list[EvalCase],
    *,
    reranker=None,
    per_district: int = 8,
    on_case=None,
) -> list[CaseResult]:
    """Ask the corpus every case and grade what comes back.

    Retrieval only — no synthesis model, no Anthropic key, no per-run cost
    beyond the query embeddings. The corpus is what is under test.
    """
    from herald.schools_retrieval import retrieve_panel

    out: list[CaseResult] = []
    for case in cases:
        panel = await retrieve_panel(
            conn, voyage,
            question=case.question,
            reranker=reranker,
            per_district=per_district,
            doc_type=case.doc_type,
        )
        by_district = {
            slug: [c.content for c in chunks]
            for slug, chunks in panel.by_district.items()
        }
        result = grade_case(case, by_district)
        out.append(result)
        if on_case is not None:
            on_case(result)
    return out


app = typer.Typer(help="Ask the corpus questions whose answers are known.",
                  no_args_is_help=True)
console = Console()


@app.command()
def run(
    cases: str = typer.Option(DEFAULT_CASES, help="Graded question file."),
    only: str | None = typer.Option(None, help="Only these case id(s), comma-separated."),
    per_district: int = typer.Option(8, help="Evidence passages per district."),
    rerank: bool = typer.Option(True, help="Voyage rerank the fused pool."),
    report: str | None = typer.Option(None, help="Write the markdown report here."),
    fail_on_regression: bool = typer.Option(
        True, help="Exit non-zero if any case fails."
    ),
) -> None:
    """Grade the corpus against its known facts.

    Retrieval only: needs ``SUPABASE_DB_URL`` and ``VOYAGE_API_KEY``, not an
    Anthropic key. What is being tested is whether the corpus can put the
    right passage in front of the answer layer — the thing that has actually
    broken every time.
    """
    import asyncio
    import os

    from herald import schools_db
    from herald.embed import VoyageEmbedder
    from herald.rerank import VoyageReranker

    db = os.environ.get("SUPABASE_DB_URL", "")
    key = os.environ.get("VOYAGE_API_KEY", "")
    if not db or not key:
        console.print("[red]SUPABASE_DB_URL and VOYAGE_API_KEY must both be set[/red]")
        raise typer.Exit(1)

    wanted = {c.strip() for c in (only or "").split(",") if c.strip()} or None
    all_cases = [c for c in load_cases(cases) if not wanted or c.id in wanted]
    if not all_cases:
        console.print("[red]no cases selected[/red]")
        raise typer.Exit(1)

    conn = schools_db.connect(db)
    voyage = VoyageEmbedder(key)
    reranker = VoyageReranker(key) if rerank else None

    def show(r: CaseResult) -> None:
        mark = "[green]PASS[/green]" if r.passed else "[red]FAIL[/red]"
        console.print(f"  {mark} {r.case.id} — {r.summary}")

    results = asyncio.run(run_eval(
        conn, voyage, all_cases, reranker=reranker,
        per_district=per_district, on_case=show,
    ))

    text = render_report(results)
    if report:
        Path(report).write_text(text, encoding="utf-8")
        console.print(f"report: {report}")
    failed = [r for r in results if not r.passed]
    console.print(f"\n[bold]{len(results) - len(failed)}/{len(results)}[/bold] cases passed")
    if failed and fail_on_regression:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
