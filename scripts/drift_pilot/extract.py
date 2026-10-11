"""Turn a document class's downloaded files into comparable text.

Three readers, tried in order, because the corpus mixes all three: a text
layer, a legacy Word binary, and page scans. OCR is last because it is slow and
noisy, and which one was used is recorded — OCR noise inflates section distance
exactly like real editing does, so the drift report has to be able to say so.

Also guards the failure that nearly became a finding in the first pilot: a PDF
whose font encoding is broken extracts as thousands of characters of mojibake
rather than raising, and a vocabulary count over that text reads as a district
*deleting* language it had the year before. Text that is mostly unreadable is
sent to OCR instead of being trusted.

    uv run scripts/drift_pilot/extract.py --doc-class code-of-conduct
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys

import typer

from herald.office_text import extract_docx, rtf_to_text
from herald.pdf_text import extract_pdf

app = typer.Typer(add_completion=False)

ROOT = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))

#: Share of characters that must be ordinary prose for a text layer to be
#: believed. A broken CID map yields runs like `!"#$%&$' ! !" ! # $`, which is
#: well under half.
MIN_LEGIBLE = 0.72

#: Below this a file holds a resolution or a cover page, not the document.
MIN_CHARS = 4000


def legible_fraction(text: str) -> float:
    if not text:
        return 0.0
    sample = text[:20000]
    ok = sum(ch.isalnum() or ch.isspace() or ch in ".,;:'\"()&/-–—%$#@!?*" for ch in sample)
    return ok / len(sample)


def read_any(path: pathlib.Path) -> tuple[str, str]:
    """``(text, how)``. ``how`` is one of text, docx, rtf, ocr, unreadable."""
    if path.suffix.lower() == ".pdf":
        try:
            text = extract_pdf(path).text
        except Exception:
            text = ""
        if len(text) >= MIN_CHARS and legible_fraction(text) >= MIN_LEGIBLE:
            return text, "text"
        return ocr(path)

    try:
        return extract_docx(path).text, "docx"
    except Exception:
        pass
    raw = path.read_bytes()
    if raw[:5] == b"{\\rtf":
        return rtf_to_text(raw.decode("latin-1", "replace")), "rtf"
    return "", "unreadable"


def ocr(path: pathlib.Path) -> tuple[str, str]:
    try:
        from herald.ocr import ocr_pdf
    except Exception as exc:  # pragma: no cover - depends on the environment
        typer.echo(f"   no OCR available: {str(exc)[:60]}", err=True)
        return "", "unreadable"
    try:
        return ocr_pdf(path, dpi=250).text, "ocr"
    except Exception as exc:
        typer.echo(f"   OCR failed on {path.name}: {str(exc)[:60]}", err=True)
        return "", "unreadable"


@app.command()
def main(
    doc_class: str = typer.Option(..., "--doc-class"),
    force: bool = typer.Option(False, help="Re-read files that already have text."),
) -> None:
    out = ROOT / doc_class
    manifest = json.loads((out / "manifest.json").read_text())
    provenance: dict[str, str] = {}
    held = out / "extraction.json"
    if held.exists() and not force:
        provenance = json.loads(held.read_text())

    typer.echo(f"{'district':<19}{'year':<9}{'KB':>7}{'chars':>9}  how")
    thin: list[str] = []
    for row in manifest:
        path = pathlib.Path(row["path"])
        stem = f"{row['district']}__{row['school_year']}"
        target = out / f"{stem}.txt"
        if target.exists() and not force and stem in provenance:
            text, how = target.read_text(errors="replace"), provenance[stem]
        else:
            text, how = read_any(path)
            if text:
                target.write_text(text)
                provenance[stem] = how
        typer.echo(f"{row['district']:<19}{row['school_year']:<9}"
                   f"{row['bytes'] / 1024:>7.0f}{len(text):>9,}  {how}")
        if len(text) < MIN_CHARS:
            thin.append(f"{stem} ({len(text):,} chars, {how})")

    held.write_text(json.dumps(provenance, indent=1))
    ocred = sorted(k for k, v in provenance.items() if v == "ocr")
    (out / "ocr.json").write_text(json.dumps(ocred))
    typer.echo(f"\n{len(manifest) - len(thin)} of {len(manifest)} usable; "
               f"{len(ocred)} needed OCR")
    for t in thin:
        typer.echo(f"   unusable: {t}")


if __name__ == "__main__":
    sys.exit(app())
