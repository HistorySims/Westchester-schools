"""Build the Elmsford CBA salary-schedule snapshot.

Elmsford UFSD and the Elmsford Teachers Association, **July 1 2024 - June 30
2027** — the last of the eight districts to have no teacher agreement in the
corpus at all, and it is current.

The file is a third kind of unreadable, distinct from the other two the corpus
has met. Port Chester's CBA is a pure scan (no text at all). Ossining's and
Greenburgh's are natively digital. Elmsford's is a scan carrying an **OCR text
layer that is wrong about numbers**:

    printed   $75,039.00   $86,153.00   $101,153.00
    OCR text   375,038.00   $36,153.00:  S101, 153.00:

`$8` reads as `3`, stray colons and quotes attach, thousands separators split.
`extract_pdf` also finds **0 tables** in it, so the grids would never become
`kind='table'` candidates — which is accidentally protective, because the
alternative was loading fabricated salaries. Neither the `no_text` path nor
`ocr --engine partial` catches this: the pages *have* text, it is simply false.

So the grids are transcribed from the page images, as Port Chester's were, and
the prose is left to ingest normally (OCR noise in prose is survivable; in a
salary it is not).

Appendix A-1/A-2/A-3, printed pages 28-30. Steps 1-20 x BA / MA / MA+15 /
MA+30 / MA+45 / MA+60 / ED D. 411 cells.

**Five audit flags, all confirmed real**: in the 2025-26 schedule steps 19 and
20 were left at their 2024-25 values for every lane except ED D, while step 18
was uprated — so 18 -> 19 dips. That is the printed document, not a misread.
Kept verbatim; `audit_salary` reports it, which is the point of the audit.

A $0.00 cell is an empty cell, not a salary: the BA lane ends at step 17, so
BA 18-20 are omitted rather than stored as zero.

NOT transcribed: Appendix B (secondary sports, intramurals, elementary
activities) and Appendix C (extra compensation), printed 31-36 — stipends, and
this district's salary gap was the blocker.

Regenerate:  uv run python scripts/build_elmsford_snapshot.py
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

OUT = Path("data/snapshots/elmsford-contract-2024-2027.jsonl.gz")
DISTRICT = "elmsford"
TITLE = ("Elmsford UFSD - Elmsford Teachers Association Agreement 2024-2027 "
         "- Appendix A Teachers Salary Schedules (OCR)")

# Operator-supplied; no public URL was confirmed for it. A guessed https:// link
# would put a dead link in every citation, so the provenance is a URN. If the
# real URL turns up — Ossining and Greenburgh both publish on Google Sites —
# ingest from it and let this fall out of the manifest.
SOURCE_URL = "urn:westchester-schools:ocr:Elmsford_T_2027.pdf#appendix-a"
SOURCE_PDF_SHA256 = "b1ce572e6584f12186c4d1ed4d1d352e46f43ac459aebb3f8b85796d2cdf829f"

LANES = ["BA", "MA", "MA+15", "MA+30", "MA+45", "MA+60", "ED D"]
PAGES = {"2024-25": 28, "2025-26": 29, "2026-27": 30}
# None = the printed cell is $0.00: the BA lane ends at step 17, so those are
# empty cells, not salaries of zero.
GRIDS = {
"2024-25": [
(1,65858,75039,78496,82319,86153,89981,93052),
(2,67613,77377,80895,84769,88626,92475,95677),
(3,69415,79786,83368,87293,91170,95037,98377),
(4,71265,82270,85915,89891,93786,97670,101153),
(5,73164,84832,88541,92567,96477,100376,104006),
(6,75115,87472,91246,95322,99246,103157,106940),
(7,77117,90195,94035,98159,102095,106016,109958),
(8,79172,93004,96909,101081,105025,108953,113061),
(9,81283,95899,99870,104090,108039,111972,116250),
(10,83449,98884,102922,107188,111141,115074,119530),
(11,85673,101963,106067,110378,114330,118262,122903),
(12,87956,105138,109308,113664,117612,121539,126370),
(13,90300,108412,112649,117048,120987,124906,129935),
(14,92707,111786,116090,120531,124460,128367,133601),
(15,95178,115267,119638,124119,128031,131923,137370),
(16,97715,118855,123294,127814,131706,135578,141247),
(17,100320,122556,127062,131618,135485,139335,145232),
(18,None,126371,130945,135535,139375,143195,149330),
(19,None,128371,132958,137562,141413,145244,151397),
(20,None,130000,134586,139190,143041,146872,153025)],
"2025-26": [
(1,67011,76352,79870,83760,87661,91556,94680),
(2,68796,78731,82311,86252,90177,94093,97351),
(3,70630,81182,84827,88821,92765,96700,100099),
(4,72512,83710,87419,91464,95427,99379,102923),
(5,74444,86317,90090,94187,98165,102133,105826),
(6,76430,89003,92843,96990,100983,104962,108811),
(7,78467,91773,95681,99877,103882,107871,111882),
(8,80558,94632,98605,102850,106863,110860,115040),
(9,82705,97577,101618,105912,109930,113932,118284),
(10,84909,100614,104723,109064,113086,117088,121622),
(11,87172,103747,107923,112310,116331,120332,125054),
(12,89495,106978,111221,115653,119670,123666,128581),
(13,91880,110309,114620,119096,123104,127092,132209),
(14,94329,113742,118122,122640,126638,130613,135939),
(15,96844,117284,121732,126291,130272,134232,139774),
(16,99425,120935,125452,130061,134011,137951,143719),
(17,102076,124701,129286,133921,137856,141773,147774),
(18,None,128582,133237,137907,141814,145701,151943),
(19,None,128371,132958,137562,141413,145244,154803),
(20,None,130000,134586,139190,143041,146872,156468)],
"2026-27": [
(1,68184,77688,81268,85226,89195,93158,96337),
(2,70000,80109,83751,87761,91755,95740,99055),
(3,71866,82603,86311,90375,94388,98392,101851),
(4,73781,85175,88949,93065,97097,101118,104724),
(5,75747,87828,91667,95835,99883,103920,107676),
(6,77768,90561,94468,98687,102750,106799,110715),
(7,79840,93379,97355,101625,105700,109759,113840),
(8,81968,96288,100331,104650,108733,112800,117053),
(9,84152,99285,103396,107765,111854,115926,120354),
(10,86395,102375,106556,110973,115065,119137,123750),
(11,88698,105563,109812,114275,118367,122438,127242),
(12,91061,108850,113167,117677,121764,125830,130831),
(13,93488,112239,116626,121180,125258,129316,134523),
(14,95980,115732,120189,124786,128854,132899,138318),
(15,98539,119336,123862,128501,132552,136581,142220),
(16,101165,123051,127647,132327,136356,140365,146234),
(17,103862,126883,131549,136265,140268,144254,150360),
(18,None,130832,135569,140320,144296,148251,154602),
(19,None,134212,139009,143822,147848,151854,158286),
(20,None,135916,140710,145524,149550,153556,159989)],
}


FOOTNOTE = ("Lanes MA+15 and MA+45 are open to teachers hired prior to "
            "July 1, 1999.")


def build_html() -> str:
    out = ["<h1>Agreement between the Elmsford Union Free School District and "
           "the Elmsford Teachers Association, July 1 2024 to June 30 2027</h1>",
           "<p>Appendix A - Teachers Salary Schedules. Transcribed from the page "
           "images: the file carries an OCR text layer whose figures are wrong.</p>"]
    for year, grid in GRIDS.items():
        out.append(f"<h2>Teachers Salary Schedule {year.replace('-', '/')} "
                   f"(Appendix A, printed page {PAGES[year]})</h2>")
        out.append("<table>")
        out.append("<tr><th>Salary Step</th>"
                   + "".join(f"<th>{lane}</th>" for lane in LANES) + "</tr>")
        for row in grid:
            cells = "".join("<td></td>" if v is None else f"<td>{v:,}</td>"
                            for v in row[1:])
            out.append(f"<tr><td>{row[0]}</td>{cells}</tr>")
        out.append("</table>")
        out.append(f"<p>{FOOTNOTE} An empty cell means the lane does not extend "
                   f"to that step.</p>")
    return "\n".join(out) + "\n"


def main() -> None:
    html = build_html()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "district": DISTRICT,
            "doc_type": "contract",
            "title": TITLE,
            "source_url": SOURCE_URL,
            "date": None,
            "source_pdf_sha256": SOURCE_PDF_SHA256,
            "transcription": "manual-vision-ocr",
            "html": html,
        }, ensure_ascii=False) + "\n")
    cells = sum(1 for g in GRIDS.values() for r in g for v in r[1:] if v is not None)
    print(f"{OUT}: 1 record, {cells} cells, {len(html):,} bytes html "
          f"-> {OUT.stat().st_size:,} bytes gzipped")


if __name__ == "__main__":
    main()
