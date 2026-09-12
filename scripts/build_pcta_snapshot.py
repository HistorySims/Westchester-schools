"""Build the Port Chester CBA salary-schedule snapshot.

`PCTA_Contract_20232027.pdf` — Port Chester-Rye UFSD and the Port Chester
Teachers Association, 2023-24 through 2026-27, in term to 2027-06-30 — is a
56-page SCAN. `extract_pdf` returns 0 characters and 0 tables for it, so the
salary grids are invisible to the normal ingest path.

The repo's answer to a document the pipeline cannot reach is the snapshot:
transcribe where the transcription can happen, commit the result, expand it on
a runner into the same raw store and manifest a live crawl would have left.
`policy-boarddocs` and `agendas` already do this for the IP-blocked BoardDocs
crawls; this does it for a scan.

The transcription below is Appendix A's four teacher grids (printed pages
26-29), read from the page images at 150 dpi — the same job `herald-ingest ocr
--engine vision` does on a runner, done here because this container has no
ANTHROPIC_API_KEY and no route to the database.

**Every cell passes the extractor's own audit invariants** (`audit_salary`):
monotonic within each lane as the step rises, canonical lane ordering at equal
step, year-over-year non-decreasing per cell, and all 784 values inside the
$30k-$250k sanity band. That is the check that makes a hand transcription
defensible — a transposed digit almost always breaks one of them.

NOT transcribed, and still missing from the corpus: the teaching-assistant
grids (printed 30-33), Appendix A Addenda (34) and Appendix B Additional
Compensation (35-42), which is where this district's stipends live.

The lane header carries a second printed row of "Level" numbers (BA = Level 1,
MA = 6, MA+30 = 8, MA+45 = 9, MA+60 = 10, MA+90 = 11, Doctorate = 12). It is
recorded as prose UNDER each table rather than as a header row on purpose: as
a row it reads as a second step axis, and as a suffix on the column header
("BA (Level 1)") `normalize_lane` parses it into a credit count and invents a
lane. The numbers are kept; the ambiguity is not.

Regenerate:  uv run python scripts/build_pcta_snapshot.py
"""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

OUT = Path("data/snapshots/pcta-contract-2023-2027.jsonl.gz")
DISTRICT = "port-chester-rye"
TITLE = "PCTA Contract 2023-2027 - Appendix A Teacher Salary Schedules (OCR)"

# No public URL for this file was ever confirmed: the operator supplied it, and
# the search engines conflate at least four "PCTA"s. A guessed https:// link
# would put a dead link in every citation the schedule produces, so the
# provenance is stated as a URN instead. If the real URL turns up, ingest the
# document from it and let this one fall out of the manifest.
SOURCE_URL = "urn:westchester-schools:ocr:PCTA_Contract_20232027.pdf#appendix-a"

SOURCE_PDF_SHA256 = "ce3903f1c8c7bd4b4de3289c1fca2245e9c45f2ffcb0840d21d13441fd71329d"

LANES = ["BA", "MA", "MA+30", "MA+45", "MA+60", "MA+90", "Doctorate"]
LEVELS = [1, 6, 8, 9, 10, 11, 12]          # the contract's own "Level" row
PAGES = {"2023-24": 26, "2024-25": 27, "2025-26": 28, "2026-27": 29}

GRIDS = {
"2023-24": [
(1,62731,72076,77942,80987,84055,90659,93966),
(2,63923,73446,79423,82526,85653,92381,95751),
(3,65137,74842,80932,84093,87280,94136,97570),
(4,66375,76264,82470,85691,88938,95924,99424),
(5,67636,77714,84037,87319,90629,97746,101313),
(6,68921,79191,85633,88979,92351,99603,103238),
(7,70231,80695,87261,90669,94106,101495,105199),
(8,71565,82229,88919,92392,95894,103423,107198),
(9,72925,83792,90608,94147,97716,105389,109235),
(10,74310,85384,92329,95936,99573,107391,111310),
(11,75723,87006,94083,97759,101465,109431,113425),
(12,77161,88659,95871,99616,103393,111511,115579),
(13,78627,90343,97693,101510,105358,113630,117775),
(14,80121,92060,99550,103439,107359,115788,120014),
(15,81643,93809,101441,105404,109398,117989,122294),
(16,83194,95591,103368,107406,111477,120230,124618),
(17,84775,97407,105332,109447,113595,122514,126986),
(18,86386,99258,107333,111526,115752,124842,129398),
(19,88028,101144,109373,113645,117951,127214,131856),
(20,89701,103066,111451,115804,120193,129632,134361),
(21,91405,105024,113569,118004,122476,132094,136915),
(22,93142,107019,115727,120246,124803,134605,139517),
(23,94911,109052,117926,122531,127174,137162,142168),
(24,96715,111125,120166,124859,129590,139768,144870),
(25,98552,113236,122449,127231,132053,142423,147622),
(26,100425,115388,124776,129648,134562,145129,150426),
(27,102334,117581,127147,132112,137119,147886,153285),
(28,104278,119815,129563,134622,139724,150696,156197)],
"2024-25": [
(1,64613,74238,80280,83417,86577,93379,96785),
(2,65841,75649,81806,85002,88223,95152,98624),
(3,67091,77087,83360,86616,89898,96960,100497),
(4,68366,78552,84944,88262,91606,98802,102407),
(5,69665,80045,86558,89939,93348,100678,104352),
(6,70989,81567,88202,91648,95122,102591,106335),
(7,72338,83116,89879,93389,96929,104540,108355),
(8,73712,84696,91587,95164,98771,106526,110414),
(9,75113,86306,93326,96971,100647,108551,112512),
(10,76539,87946,95099,98814,102560,110613,114649),
(11,77995,89616,96905,100692,104509,112714,116828),
(12,79476,91319,98747,102604,106495,114856,119046),
(13,80986,93053,100624,104555,108519,117039,121308),
(14,82525,94822,102537,106542,110580,119262,123614),
(15,84092,96623,104484,108566,112680,121529,125963),
(16,85690,98459,106469,110628,114821,123837,128357),
(17,87318,100329,108492,112730,117003,126189,130796),
(18,88978,102236,110553,114872,119225,128587,133280),
(19,90669,104178,112654,117054,121490,131030,135812),
(20,92392,106158,114795,119278,123799,133521,138392),
(21,94147,108175,116976,121544,126150,136057,141022),
(22,95936,110230,119199,123853,128547,138643,143703),
(23,97758,112324,121464,126207,130989,141277,146433),
(24,99616,114459,123771,128605,133478,143961,149216),
(25,101509,116633,126122,131048,136015,146696,152051),
(26,103438,118850,128519,133537,138599,149483,154939),
(27,105404,121108,130961,136075,141233,152323,157884),
(28,107406,123409,133450,138661,143916,155217,160883)],
"2025-26": [
(1,66551,76465,82688,85920,89174,96180,99689),
(2,67816,77918,84260,87552,90870,98007,101583),
(3,69104,79400,85861,89214,92595,99869,103512),
(4,70417,80909,87492,90910,94354,101766,105479),
(5,71755,82446,89155,92637,96148,103698,107483),
(6,73119,84014,90848,94397,97976,105669,109525),
(7,74508,85609,92575,96191,99837,107676,111606),
(8,75923,87237,94335,98019,101734,109722,113726),
(9,77366,88895,96126,99880,103666,111808,115887),
(10,78835,90584,97952,101778,105637,113931,118088),
(11,80335,92304,99812,103713,107644,116095,120333),
(12,81860,94059,101709,105682,109690,118302,122617),
(13,83416,95845,103643,107692,111775,120550,124947),
(14,85001,97667,105613,109738,113897,122840,127322),
(15,86615,99522,107619,111823,116060,125175,129742),
(16,88261,101413,109663,113947,118266,127552,132208),
(17,89938,103339,111747,116112,120513,129975,134720),
(18,91647,105303,113870,118318,122802,132445,137278),
(19,93389,107303,116034,120566,125135,134961,139886),
(20,95164,109343,118239,122856,127513,137527,142544),
(21,96971,111420,120485,125190,129935,140139,145253),
(22,98814,113537,122775,127569,132403,142802,148014),
(23,100691,115694,125108,129993,134919,145515,150826),
(24,102604,117893,127484,132463,137482,148280,153692),
(25,104554,120132,129906,134979,140095,151097,156613),
(26,106541,122416,132375,137543,142757,153967,159587),
(27,108566,124741,134890,140157,145470,156893,162621),
(28,110628,127111,137454,142821,148233,159874,165709)],
"2026-27": [
(1,68548,78759,85169,88498,91849,99065,102680),
(2,69850,80256,86788,90179,93596,100947,104630),
(3,71177,81782,88437,91890,95373,102865,106617),
(4,72530,83336,90117,93637,97185,104819,108643),
(5,73908,84919,91830,95416,99032,106809,110707),
(6,75313,86534,93573,97229,100915,108839,112811),
(7,76743,88177,95352,99077,102832,110906,114954),
(8,78201,89854,97165,100960,104786,113014,117138),
(9,79687,91562,99010,102876,106776,115162,119364),
(10,81200,93302,100891,104831,108806,117349,121631),
(11,82745,95073,102806,106824,110873,119578,123943),
(12,84316,96881,104760,108852,112981,121851,126296),
(13,85918,98720,106752,110923,115128,124167,128695),
(14,87551,100597,108781,113030,117314,126525,131142),
(15,89213,102508,110848,115178,119542,128930,133634),
(16,90909,104455,112953,117365,121814,131379,136174),
(17,92636,106439,115099,119595,124128,133874,138762),
(18,94396,108462,117286,121868,126486,136418,141396),
(19,96191,110522,119515,124183,128889,139010,144083),
(20,98019,112623,121786,126542,131338,141653,146820),
(21,99880,114763,124100,128946,133833,144343,149611),
(22,101778,116943,126458,131396,136375,147086,152454),
(23,103712,119165,128861,133893,138967,149880,155351),
(24,105682,121430,131309,136437,141606,152728,158303),
(25,107691,123736,133803,139028,144298,155630,161311),
(26,109737,126088,136346,141669,147040,158586,164375),
(27,111823,128483,138937,144362,149834,161600,167500),
(28,113947,130924,141578,147106,152680,164670,170680)],
}


def build_html() -> str:
    levels = ", ".join(f"{lane} = Level {lvl}" for lane, lvl in zip(LANES, LEVELS))
    out = [
        "<h1>Contract between the Port Chester-Rye Union Free School District "
        "Board of Education and the Port Chester Teachers Association</h1>",
        "<p>School years 2023-2024, 2024-2025, 2025-2026 and 2026-2027.</p>",
        "<p>Appendix A - Teacher and Teaching Assistant Salary Schedules. "
        "Transcribed from the scanned contract; teacher grids only.</p>",
    ]
    for year, grid in GRIDS.items():
        printed = PAGES[year]
        out.append(f"<h2>Teachers - {year.replace('-', '/')} School Year "
                   f"(Appendix A, printed page {printed})</h2>")
        out.append("<table>")
        out.append("<tr><th>Step</th>"
                   + "".join(f"<th>{lane}</th>" for lane in LANES) + "</tr>")
        for row in grid:
            cells = "".join(f"<td>{v:,}</td>" for v in row[1:])
            out.append(f"<tr><td>{row[0]}</td>{cells}</tr>")
        out.append("</table>")
        out.append(f"<p>Lane levels as printed: {levels}.</p>")
    return "\n".join(out) + "\n"


def main() -> None:
    html = build_html()
    rec = {
        "district": DISTRICT,
        "doc_type": "contract",
        "title": TITLE,
        "source_url": SOURCE_URL,
        "date": None,
        "source_pdf_sha256": SOURCE_PDF_SHA256,
        "transcription": "manual-vision-ocr",
        "html": html,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    raw = len(html.encode("utf-8"))
    print(f"{OUT}: 1 record, {raw:,} bytes html -> {OUT.stat().st_size:,} bytes gzipped")
    print(f"sha256(html) = {hashlib.sha256(html.encode()).hexdigest()[:16]}")


if __name__ == "__main__":
    main()
