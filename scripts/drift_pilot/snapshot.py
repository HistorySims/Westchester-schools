"""Commit a document class's extracted text as a snapshot the runner can read.

Acquisition cannot run on a GitHub runner — BoardDocs blocks those IP ranges —
so the established pattern in `data/snapshots/` is to collect where the network
works, commit the result, and expand it on the runner. The text, not the PDFs:
30 safety plans are ~60 MB of PDF and 676 KB of gzipped text, and this repo has
a 500 MB database tier behind it (docs/DISK_SPACE.md).

Extraction provenance travels with each row, because OCR noise inflates section
distance exactly like real editing does and the drift report has to be able to
say which filings are suspect.

    uv run scripts/drift_pilot/snapshot.py --doc-class code-of-conduct
"""

from __future__ import annotations

import gzip
import json
import os
import pathlib
import re
import sys

import typer

app = typer.Typer(add_completion=False)

ROOT = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
OUT = pathlib.Path("data/snapshots")

#: Below this a file is a resolution or a cover page, not the document.
MIN_CHARS = 4000


@app.command()
def main(
    doc_class: str = typer.Option(..., "--doc-class"),
    out: pathlib.Path = typer.Option(OUT, help="Where to write <doc-class>s.jsonl.gz"),
) -> None:
    src = ROOT / doc_class
    manifest = {(m["district"], m["school_year"]): m
                for m in json.loads((src / "manifest.json").read_text())}
    provenance = json.loads((src / "extraction.json").read_text())

    target = out / f"{doc_class}s.jsonl.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    written = skipped = 0
    with gzip.open(target, "wt", encoding="utf-8") as fh:
        for path in sorted(src.glob("*__20*.txt")):
            district, year = path.stem.rsplit("__", 1)
            if not re.fullmatch(r"20\d\d-\d\d", year):
                continue
            text = path.read_text(errors="replace")
            if len(text) < MIN_CHARS:
                typer.echo(f"   too thin, skipped: {path.stem} ({len(text):,})")
                skipped += 1
                continue
            row = manifest.get((district, year), {})
            fh.write(json.dumps({
                "district": district, "school_year": year,
                "doc_class": doc_class,
                "meeting_date": row.get("meeting_date"),
                "title": row.get("title"),
                "source_url": row.get("url"),
                "extraction": provenance.get(path.stem, "text"),
                "chars": len(text), "text": text,
            }, ensure_ascii=False) + "\n")
            written += 1

    size = target.stat().st_size / 1024
    typer.echo(f"\n{written} filings -> {target} ({size:.0f} KB gzipped)"
               + (f", {skipped} too thin" if skipped else ""))


if __name__ == "__main__":
    sys.exit(app())
