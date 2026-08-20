"""Text extraction for HTML documents, mirroring :mod:`herald.pdf_text`.

Most of the corpus arrives as PDF, but the adopted policy manuals do not: the
BoardPolicyOnline portals hand us clean HTML (see
``herald.scrape.policy_manual``), and re-printing that to PDF just to read it
back would throw away the structure we were given for free.

The contract is the same as ``extract_pdf`` — an :class:`ExtractedDoc` whose
``text`` is prose with table regions removed and whose ``tables`` are whole
markdown blocks — so ingest treats an HTML policy exactly like a PDF one.
"""

from __future__ import annotations

from pathlib import Path

from bs4 import BeautifulSoup, Tag

from herald.pdf_text import ExtractedDoc, TableBlock, sanitize

#: Tags that carry no readable content.
_DROP = ("script", "style", "noscript", "head", "meta", "link")


def _cell_text(cell: Tag) -> str:
    # Pipes would break the markdown row this cell is about to become.
    return cell.get_text(" ", strip=True).replace("|", "/").replace("\n", " ")


def table_to_markdown(table: Tag) -> str:
    """One HTML table as a markdown grid, header row separated if present."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if cells:
            rows.append([_cell_text(c) for c in cells])
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def extract_html(path: str | Path) -> ExtractedDoc:
    """Prose + tables from an HTML file.

    Tables are pulled out of the prose the same way ``extract_pdf`` does, so a
    policy's fee schedule or calendar survives as a whole table chunk instead
    of dissolving into a run of unlabelled numbers.
    """
    raw = Path(path).read_text(encoding="utf-8", errors="replace")
    return extract_html_text(raw)


def extract_html_text(html: str) -> ExtractedDoc:
    """Same as :func:`extract_html`, from a string."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup.find_all(_DROP):
        tag.decompose()

    tables: list[TableBlock] = []
    for t in soup.find_all("table"):
        md = table_to_markdown(t)
        if md:
            tables.append(TableBlock(page=1, markdown=md))
        t.decompose()          # keep it out of the prose, as extract_pdf does

    text = sanitize(soup.get_text("\n", strip=True))
    # Collapse the blank-line runs that stripping block tags leaves behind.
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return ExtractedDoc(text=text, tables=tables, page_count=1)
