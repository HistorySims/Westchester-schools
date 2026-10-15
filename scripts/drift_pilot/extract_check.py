"""Can every downloaded plan actually be read? Check before spending on embeddings.

Two known hazards:

* Mount Vernon filed 2021-22 and 2022-23 as legacy ``.doc``, which python-docx
  cannot open. Some BoardDocs ``.doc`` files are really RTF, so that fallback
  is worth trying before writing the district off for those years.
* Elmsford's 2019-20, 2021-22 and 2022-23 attachments are ~150 KB where its
  2020-21 is 10 MB. A 150 KB file is very likely the board's one-page approval
  resolution rather than the plan, which would put a governance sentence in the
  peer set where a 60-page plan belongs. Character counts make that visible.
"""
from __future__ import annotations

import os
import json
import pathlib

from herald.office_text import extract_docx, rtf_to_text
from herald.pdf_text import extract_pdf

SCRATCH = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
PLANS = SCRATCH / "plans"


def read_plan(path: pathlib.Path) -> tuple[str, str]:
    """(text, how) — empty text means nothing could read it."""
    if path.suffix.lower() == ".pdf":
        return extract_pdf(path).text, "pdf"
    try:
        return extract_docx(path).text, "docx"
    except Exception:
        pass
    raw = path.read_bytes()
    if raw[:5] == b"{\\rtf":
        return rtf_to_text(raw.decode("latin-1", "replace")), "rtf"
    return "", "unreadable"


def main() -> None:
    manifest = json.loads((PLANS / "manifest.json").read_text())
    rows = []
    for m in manifest:
        p = pathlib.Path(m["path"])
        try:
            text, how = read_plan(p)
        except Exception as exc:
            text, how = "", f"error: {type(exc).__name__} {str(exc)[:40]}"
        rows.append((m["district"], m["school_year"], m["bytes"], len(text), how))
        print(f"{m['district']:<19}{m['school_year']:<9}{m['bytes'] / 1024:>8.0f} KB"
              f"{len(text):>9,} chars  {how}")
        if text:
            (PLANS / f"{m['district']}__{m['school_year']}.txt").write_text(text)

    bad = [r for r in rows if r[3] < 4000]
    print(f"\n{len(rows) - len(bad)} of {len(rows)} readable with real text")
    for d, yr, kb, n, how in bad:
        print(f"   too thin: {d} {yr}  {n:,} chars  ({how}, {kb / 1024:.0f} KB file)")


if __name__ == "__main__":
    main()
