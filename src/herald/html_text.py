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


def _table_rows(table: Tag) -> list[list[str]]:
    """The table's cells as rows of text; empty rows dropped."""
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if cells:
            rows.append([_cell_text(c) for c in cells])
    return rows


def _rows_to_markdown(rows: list[list[str]]) -> str:
    """Rows as a markdown grid; the first row becomes the header."""
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "---|" * width]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def table_to_markdown(table: Tag) -> str:
    """One HTML table as a markdown grid, header row separated if present."""
    return _rows_to_markdown(_table_rows(table))


def _item_label(table: Tag) -> str:
    """What the surrounding document calls this table.

    BoardDocs renders each agenda item as ``<dt class="leftcol">Subject</dt>``
    followed by a ``<dd>`` holding the item's name, with the item's body — and
    any tables in it — after that. So the nearest preceding Subject/dd pair is
    the table's caption in all but name, and it is the only thing in the markup
    that says a grid of names and dollar amounts is a list of approved
    conferences.

    Falls back to a preceding ``<caption>`` or heading for ordinary HTML, and
    to "" when nothing suitable is near.
    """
    dt = table.find_previous("dt", class_="leftcol")
    while dt is not None:
        if dt.get_text(strip=True).casefold() == "subject":
            dd = dt.find_next_sibling("dd")
            if dd is not None:
                return dd.get_text(" ", strip=True)
            break
        dt = dt.find_previous("dt", class_="leftcol")

    caption = table.find("caption")
    if caption is not None:
        return caption.get_text(" ", strip=True)

    heading = table.find_previous(["h1", "h2", "h3", "h4", "h5", "h6"])
    return heading.get_text(" ", strip=True) if heading is not None else ""


def _merge_split_tables(
    blocks: list[tuple[list[list[str]], str]],
) -> list[tuple[list[list[str]], str]]:
    """Rejoin a table that the source split into a header and a body.

    Word pastes — which is what a BoardDocs agenda item's body usually is —
    routinely emit one ``<table>`` holding only the header row and a second
    holding only the data. Port Chester's 2026-02-26 conference list arrives
    exactly that way: table 4 is ``Name | Conference | Date | Amount`` and
    nothing else, table 5 is three people and their amounts under the same
    agenda item. Left alone that produces one chunk of pure noise and one
    chunk of numbers whose columns are anonymous.

    So a block holding a single row absorbs the blocks that follow it under
    the same label at the same width. The rule is deliberately narrow: a
    genuine table has more than one row and never triggers this, so two real
    grids under one agenda item are not glued together.
    """
    out: list[tuple[list[list[str]], str]] = []
    for rows, label in blocks:
        if out:
            prev_rows, prev_label = out[-1]
            same_item = bool(label) and label == prev_label
            same_width = bool(rows) and bool(prev_rows) and (
                max(len(r) for r in rows) == max(len(r) for r in prev_rows)
            )
            if len(prev_rows) == 1 and same_item and same_width:
                out[-1] = (prev_rows + rows, prev_label)
                continue
        out.append((rows, label))
    return out


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

    # Read every table first — labels come from the markup *around* a table, so
    # nothing may be decomposed until they are all collected.
    found = [(_table_rows(t), _item_label(t)) for t in soup.find_all("table")]
    for t in soup.find_all("table"):
        t.decompose()          # keep it out of the prose, as extract_pdf does

    tables: list[TableBlock] = []
    for rows, label in _merge_split_tables(found):
        # One row is a header with nothing under it, or a lone stray cell.
        # Either way it carries no fact — and stored as a chunk it is a
        # retrievable, embeddable piece of nothing.
        if len(rows) < 2:
            continue
        md = _rows_to_markdown(rows)
        if md:
            tables.append(TableBlock(page=1, markdown=md, label=label))

    text = sanitize(soup.get_text("\n", strip=True))
    # Collapse the blank-line runs that stripping block tags leaves behind.
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return ExtractedDoc(text=text, tables=tables, page_count=1)
