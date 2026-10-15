#!/usr/bin/env python
"""Report where districts broke from the template in a class of filing.

Reads the committed text snapshot rather than crawling, because BoardDocs
blocks GitHub runner IP ranges — the same reason `data/snapshots/` exists for
agendas. Acquisition happens where the network works and the result is
committed; this expands it on the runner.

Needs ``VOYAGE_API_KEY``. No database: the snapshot is self-contained, which
also means this runs when Supabase is asleep.

    uv run scripts/plan_drift.py
    uv run scripts/plan_drift.py --year 2024-25 --top 6

The method and its validation are in docs/DRIFT_PILOT.md. The short version:
compare sections against the same district's previous filing, rank by how far
each moved, then ask whether a peer filed the same thing that year. Shared is
the state; unshared is a decision.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import gzip
import json
import os
import pathlib
import sys
from collections import defaultdict

import numpy as np
import typer

from herald.drift_schools import (
    changed_sections,
    classify_change,
    consensus_movement,
    sections,
)
from herald.embed import VoyageEmbedder

app = typer.Typer(add_completion=False)

SNAPSHOT = pathlib.Path("data/snapshots/safety-plans.jsonl.gz")

#: A section has to move this far before it is worth a human reading it.
#: Untouched template re-extracts around 0.01.
REPORT_FLOOR = 0.12


def load(path: pathlib.Path) -> list[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh]


async def embed_all(texts: list[str], cache: pathlib.Path | None) -> np.ndarray:
    if cache and cache.exists():
        held = np.load(cache, allow_pickle=True)
        if list(held["texts"]) == texts:
            return held["vecs"]
    async with VoyageEmbedder(os.environ["VOYAGE_API_KEY"]) as voyage:
        vecs = np.array(await voyage.embed_documents(texts), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    if cache:
        np.savez(cache, vecs=vecs, texts=np.array(texts, dtype=object))
    return vecs


@app.command()
def main(
    snapshot: pathlib.Path = typer.Option(SNAPSHOT, help="Committed text snapshot."),
    year: str = typer.Option("", help="Only report this school year, e.g. 2024-25."),
    top: int = typer.Option(4, help="Passages to quote per district-year."),
    floor: float = typer.Option(REPORT_FLOOR, help="Minimum distance to report."),
    out: pathlib.Path = typer.Option(
        pathlib.Path(f"scripts/plan_drift_{dt.date.today():%Y%m%d}.md"),
        help="Markdown report to write.",
    ),
    cache: pathlib.Path = typer.Option(
        pathlib.Path("") , help="Optional .npz to cache embeddings between runs."
    ),
) -> None:
    if not snapshot.exists():
        typer.echo(f"no snapshot at {snapshot}", err=True)
        raise typer.Exit(1)

    rows = load(snapshot)
    texts: list[str] = []
    keys: list[tuple[str, str]] = []
    provenance: dict[tuple[str, str], str] = {}
    for row in rows:
        key = (row["district"], row["school_year"])
        chunks = sections(row["text"])
        if len(chunks) < 5:
            continue
        provenance[key] = row.get("extraction", "text")
        texts.extend(chunks)
        keys.extend([key] * len(chunks))

    if not texts:
        typer.echo("snapshot held no filing with enough sections", err=True)
        raise typer.Exit(1)

    typer.echo(f"{len(texts)} sections across {len(set(keys))} district-years")
    vecs = asyncio.run(embed_all(texts, cache if str(cache) else None))

    index: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, key in enumerate(keys):
        index[key].append(i)
    years = sorted({y for _, y in index})

    lines = [
        "# Where districts broke from the template",
        "",
        f"Generated {dt.date.today()} from `{snapshot}` — "
        f"{len(set(keys))} filings, {len(texts)} sections.",
        "",
        "Method and validation: `docs/DRIFT_PILOT.md`. A passage a peer also "
        "filed that year is the state; a passage nobody else filed is a "
        "decision.",
        "",
    ]

    by_year = {y: {d: vecs[np.array(index[(d, y)])]
                   for d, yy in index if yy == y} for y in years}
    lines += ["## Group movement, year to year", "",
              "| transition | districts in both | consensus moved |", "|---|---|---|"]
    for a, b in zip(years, years[1:]):
        shared = sorted(set(by_year[a]) & set(by_year[b]))
        lines.append(f"| {a} → {b} | {len(shared)} | "
                     f"{consensus_movement(by_year[a], by_year[b]):.4f} |")
    lines += ["", "Whole-filing movement is reported for the group only. It does "
              "not work per district on mandated documents — that is the finding "
              "in DRIFT_PILOT.md, kept here so the number is not mistaken for a "
              "per-district signal.", ""]

    found = 0
    for district in sorted({d for d, _ in index}):
        mine = sorted(y for y in years if (district, y) in index)
        for previous, current in zip(mine, mine[1:]):
            if year and current != year:
                continue
            cur_i = np.array(index[(district, current)])
            peer_i = np.array([i for (d, y), ii in index.items()
                               if y == current and d != district for i in ii])
            changes = changed_sections(
                vecs[cur_i], vecs[np.array(index[(district, previous)])],
                [texts[i] for i in cur_i],
                peers=vecs[peer_i] if len(peer_i) else None,
            )
            worth = [c for c in changes if c.distance >= floor][:top]
            gap = int(current[:4]) - int(previous[:4])
            notes = []
            if gap > 1:
                notes.append(f"{gap}-year gap")
            if "ocr" in (provenance.get((district, current), ""),
                         provenance.get((district, previous), "")):
                notes.append("OCR — distances inflated")
            if not len(peer_i):
                notes.append("no peer filings this year, so nothing can be "
                             "called local")

            lines += [f"### {district}  {previous} → {current}", ""]
            if notes:
                lines += [f"*{'; '.join(notes)}*", ""]
            if not worth:
                lines += ["Nothing above the floor — this filing restates the "
                          "previous one.", ""]
                continue
            for change in worth:
                found += 1
                kind = classify_change(change)
                label = {"mandate": "shared with a peer",
                         "local": "**no peer filed this**"}.get(kind, kind)
                lines += [
                    f"- **{change.distance:.3f}** moved, {label} "
                    f"(peer {change.best_peer:.2f})",
                    f"  > {change.text[:600]}",
                    "",
                ]

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines))
    typer.echo(f"{found} passages above {floor} -> {out}")


if __name__ == "__main__":
    sys.exit(app())
