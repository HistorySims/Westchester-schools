"""Text extraction for the Word/RTF documents that turn up as attachments.

Most of the corpus is PDF, and the policy manuals are HTML. But a policy's
attachment is whatever the district happened to upload, and some districts
upload the regulation as a Word file — White Plains' *5700 Purchasing
Regulation*, *5830 Expense Reimbursement* and Mount Vernon's *7530-R Child
Abuse and Maltreatment* among them. PyMuPDF cannot open any of these, so
without this module they land in the corpus as errors: a policy present by
title, with no text behind it.

Both formats are handled with the standard library. ``.docx`` is a zip whose
``word/document.xml`` holds the runs; ``.rtf`` is control words around plain
text. Legacy binary ``.doc`` (OLE2) is **not** handled — it needs a real
converter, and there is exactly one in the corpus. It is reported as an
error rather than silently returning nothing.
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree

from herald.pdf_text import ExtractedDoc, sanitize

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# RTF: \uN escapes, control words, groups, and the destinations whose contents
# are metadata rather than document text.
# Matches at the character after "{". Two cases: a named destination whose
# contents are metadata rather than prose, and "\*\anything" — the RTF
# ignorable-destination marker, which a reader that does not understand the
# destination is *supposed* to skip. Honoring "\*" generically is what keeps
# writer-specific junk (a Grammarly blob, an embedded document-properties
# stream) out of the text, without having to enumerate every producer.
_RTF_SKIP_GROUP = re.compile(
    r"\\(?:\*\\[a-zA-Z]+"
    r"|(?:fonttbl|colortbl|stylesheet|listtable|listoverridetable|rsidtbl"
    r"|generator|info|pict|themedata|colorschememapping|latentstyles|datastore"
    r"|xmlnstbl|filetbl|revtbl|upr|shppict|nonshppict|header|footer|footnote)\b)",
    re.I,
)
_RTF_UNICODE = re.compile(r"\\u(-?\d+) ?")
_RTF_HEX = re.compile(r"\\'([0-9a-fA-F]{2})")
_RTF_PARA = re.compile(r"\\(?:par|line|sect|page)\b")
_RTF_CONTROL = re.compile(r"\\[a-zA-Z]+-?\d*\s?")
_RTF_ESCAPED = re.compile(r"\\([{}\\])")


def extract_docx(path: str | Path) -> ExtractedDoc:
    """Paragraph text from a ``.docx``.

    Tables are read as paragraphs rather than as grids: a policy's Word
    attachment is prose with the occasional layout table, not a salary
    schedule, so a separate table chunk would add noise, not structure.
    """
    with zipfile.ZipFile(path) as z:
        xml = z.read("word/document.xml")
    root = ElementTree.fromstring(xml)
    paragraphs: list[str] = []
    for p in root.iter(f"{_W}p"):
        text = "".join(t.text or "" for t in p.iter(f"{_W}t"))
        if text.strip():
            paragraphs.append(text.strip())
    return ExtractedDoc(text=sanitize("\n".join(paragraphs)), tables=[], page_count=1)


def extract_rtf(path: str | Path) -> ExtractedDoc:
    """Plain text from an ``.rtf``, control words and metadata groups removed."""
    raw = Path(path).read_text(encoding="latin-1", errors="replace")
    return ExtractedDoc(text=sanitize(rtf_to_text(raw)), tables=[], page_count=1)


def rtf_to_text(raw: str) -> str:
    """Strip RTF markup. Deliberately simple — enough for prose documents."""
    out: list[str] = []
    depth = 0
    skip_to_depth: int | None = None
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "{":
            depth += 1
            m = _RTF_SKIP_GROUP.match(raw, i + 1)
            if m and skip_to_depth is None:
                skip_to_depth = depth
            i += 1
            continue
        if ch == "}":
            if skip_to_depth is not None and depth == skip_to_depth:
                skip_to_depth = None
            depth -= 1
            i += 1
            continue
        if skip_to_depth is not None:
            i += 1
            continue
        if ch == "\\":
            m = _RTF_ESCAPED.match(raw, i)
            if m:
                out.append(m.group(1))
                i = m.end()
                continue
            m = _RTF_UNICODE.match(raw, i)
            if m:
                code = int(m.group(1))
                out.append(chr(code if code >= 0 else code + 65536))
                i = m.end()
                # \uN is followed by one fallback character for readers that
                # cannot handle Unicode (usually "?"); it is not document text.
                if i < n and raw[i] not in "\\{}":
                    i += 1
                continue
            m = _RTF_HEX.match(raw, i)
            if m:
                out.append(bytes([int(m.group(1), 16)]).decode("cp1252", errors="replace"))
                i = m.end()
                continue
            if _RTF_PARA.match(raw, i):
                out.append("\n")
                i = _RTF_CONTROL.match(raw, i).end()
                continue
            m = _RTF_CONTROL.match(raw, i)
            if m:
                i = m.end()
                continue
            i += 1
            continue
        out.append(ch)
        i += 1

    text = "".join(out)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)
