"""GATE 1 — Confirm the American Stories answer key exists for our corpus.

Loads ONLY the 1845 subset of dell-research-harvard/AmericanStories
(content-regions mode, which carries legibility flags + bounding
boxes), lists the distinct LCCNs present, and checks whether our two
papers are there:

    sn83030213  New-York Daily Tribune
    sn83030313  New York Herald

Read-only everywhere: nothing is written to the database, and only
the 1845 subset is downloaded — never the full dataset.

Also pulls (read-only) the denominators Gate 2 will need: how many
distinct (date, sequence) pages our quarantined chunks span, per
LCCN — so the report can say how big the joinable universe could be
at best. The join itself is Gate 2 work and is NOT performed here.

Writes scripts/answer_key_check.md and prints it to stdout.

GATE RULE (from the driving prompt): if at least one of our LCCNs is
present in 1845 → GO for Gate 2. If neither is present or the dataset
is inaccessible → NO-GO; switch to Fallback A (manual answer key).
This script only reports; the human decides at the gate.

Usage (needs `datasets<3` for the HF loading script):
    uv run --with "datasets<3" python scripts/american_stories_gate1.py
"""

from __future__ import annotations

import json
import sys
import traceback
from collections import Counter
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
REPORT_PATH = SCRIPT_DIR / "answer_key_check.md"

TARGET_LCCNS = {
    "sn83030213": "New-York Daily Tribune",
    "sn83030313": "New York Herald",
}
SAMPLES_PER_PAPER = 5
SAMPLE_TEXT_TRIM = 500


def main() -> None:
    lines: list[str] = []
    lines.append("# Gate 1 — American Stories answer-key check")
    lines.append("")

    # ---- Step A: our own denominators (read-only; optional) ----------
    lines.extend(_our_corpus_context())

    # ---- Step B: load the 1845 subset --------------------------------
    records, load_note = _load_1845()
    lines.append("## Dataset load")
    lines.append("")
    lines.append(load_note)
    lines.append("")

    if records is None:
        lines.append("**VERDICT: NO-GO — dataset inaccessible.** Per the gate")
        lines.append("rule, switch to Fallback A (manual ~40-chunk answer key).")
        _finish(lines)
        return

    # ---- Step C: LCCN census ------------------------------------------
    lccn_counts: Counter[str] = Counter()
    samples: dict[str, list[dict]] = {k: [] for k in TARGET_LCCNS}
    structure_dump: str | None = None
    parse_failures = 0

    for rec in records:
        lccn, meta = _extract_lccn(rec)
        if lccn is None:
            parse_failures += 1
            continue
        lccn_counts[lccn] += 1
        if structure_dump is None:
            structure_dump = _describe_structure(rec, meta)
        if lccn in samples and len(samples[lccn]) < SAMPLES_PER_PAPER:
            samples[lccn].append(_trim_record(rec, meta))

    total = sum(lccn_counts.values())
    lines.append("## 1845 subset census")
    lines.append("")
    lines.append(f"- Total records: **{total:,}** "
                 f"({parse_failures:,} records where no LCCN could be parsed)")
    lines.append(f"- Distinct LCCNs: **{len(lccn_counts):,}**")
    lines.append("")
    lines.append("Top 20 LCCNs by record count:")
    lines.append("")
    lines.append("| lccn | records | ours? |")
    lines.append("| --- | ---: | --- |")
    for lccn, n in lccn_counts.most_common(20):
        ours = TARGET_LCCNS.get(lccn, "")
        lines.append(f"| {lccn} | {n:,} | {ours} |")
    lines.append("")

    # ---- Step D: the go/no-go question --------------------------------
    lines.append("## Our papers")
    lines.append("")
    present: list[str] = []
    for lccn, title in TARGET_LCCNS.items():
        n = lccn_counts.get(lccn, 0)
        status = "**PRESENT**" if n > 0 else "absent"
        if n > 0:
            present.append(lccn)
        lines.append(f"- `{lccn}` {title}: {status} — {n:,} records in 1845")
    lines.append("")

    if structure_dump:
        lines.append("## Record structure")
        lines.append("")
        lines.append(structure_dump)
        lines.append("")

    for lccn in present:
        lines.append(f"## Sample records — {lccn} ({TARGET_LCCNS[lccn]})")
        lines.append("")
        for i, s in enumerate(samples[lccn], 1):
            lines.append(f"### Sample {i}")
            lines.append("")
            lines.append("```json")
            lines.append(json.dumps(s, indent=2, default=str)[:2500])
            lines.append("```")
            lines.append("")

    lines.append("## Verdict")
    lines.append("")
    if present:
        names = ", ".join(f"{l} ({TARGET_LCCNS[l]})" for l in present)
        lines.append(f"**GO — answer key exists for: {names}.**")
        lines.append("Proceed to Gate 2 (join + label + baseline) with the")
        lines.append("paper(s) above, after human review of this report.")
    else:
        lines.append("**NO-GO — neither of our LCCNs appears in the 1845")
        lines.append("subset.** Per the gate rule: do not fabricate a")
        lines.append("workaround. Switch to Fallback A (manual ~40-chunk")
        lines.append("answer key from LoC page images).")

    _finish(lines)


def _our_corpus_context() -> list[str]:
    """Read-only denominators from our DB, for Gate-2 sizing. Optional —
    if the DB is unreachable the gate still answers its core question."""
    out: list[str] = []
    out.append("## Our corpus context (read-only)")
    out.append("")
    try:
        import psycopg
        from herald import settings

        cfg = settings.load()
        if not cfg.supabase_db_url:
            out.append("_SUPABASE_DB_URL not set — skipping corpus context._")
            out.append("")
            return out
        conn = psycopg.connect(cfg.supabase_db_url, autocommit=True,
                               prepare_threshold=None)
        try:
            cur = conn.execute("SELECT lccn, title FROM papers ORDER BY lccn")
            out.append("Papers in our corpus:")
            out.append("")
            for lccn, title in cur.fetchall():
                out.append(f"- `{lccn}` — {title}")
            out.append("")

            cur = conn.execute(
                """
                SELECT papers.lccn,
                       COUNT(*) AS chunks,
                       COUNT(DISTINCT (issues.date_issued, pages.sequence)) AS pages
                  FROM chunks
                  JOIN pages  ON pages.id = chunks.page_id
                  JOIN issues ON issues.id = pages.issue_id
                  JOIN papers ON papers.id = issues.paper_id
                 WHERE chunks.status = 'quarantined'
                   AND chunks.is_current = true
                 GROUP BY papers.lccn
                 ORDER BY papers.lccn
                """
            )
            out.append("Quarantined chunks (the population Gate 2 would join):")
            out.append("")
            out.append("| lccn | quarantined chunks | distinct pages |")
            out.append("| --- | ---: | ---: |")
            for lccn, chunks, pages in cur.fetchall():
                out.append(f"| {lccn} | {int(chunks):,} | {int(pages):,} |")
            out.append("")
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001 — context is optional
        out.append(f"_Corpus context unavailable: {type(e).__name__}: {e}_")
        out.append("")
    return out


def _load_1845():
    """Load the 1845 content-regions subset. Returns (iterable, note).

    Primary: the datasets loading script, exactly as the dataset card
    documents. Fallback: none — if the primary fails, the gate reports
    NO-GO (inaccessible) with the error, per the no-workaround rule.
    """
    try:
        from datasets import load_dataset

        ds = load_dataset(
            "dell-research-harvard/AmericanStories",
            "subset_years_content_regions",
            year_list=["1845"],
            trust_remote_code=True,
        )
        # DatasetDict keyed by year.
        key = "1845" if "1845" in ds else list(ds.keys())[0]
        note = (
            f"Loaded via `datasets.load_dataset(..., "
            f"'subset_years_content_regions', year_list=['1845'])`. "
            f"Splits: {list(ds.keys())} — using '{key}' "
            f"({len(ds[key]):,} records)."
        )
        return ds[key], note
    except Exception as e:  # noqa: BLE001
        tb = traceback.format_exc(limit=3)
        note = (
            "**Failed to load the 1845 subset.**\n\n"
            f"```\n{type(e).__name__}: {e}\n{tb}\n```"
        )
        return None, note


def _extract_lccn(rec: dict) -> tuple[str | None, dict | None]:
    """Find the LCCN in a record, wherever the config put it.

    Content-regions records are scan-level and may carry metadata as
    top-level fields OR inside a JSON string (raw_data_string-style).
    Returns (lccn, parsed_metadata_dict_or_None).
    """
    # Direct fields first.
    for key in ("lccn", "LCCN"):
        v = rec.get(key)
        if isinstance(v, str) and v.startswith("sn"):
            return v, None

    # Scan for a JSON-bearing string field.
    for key, v in rec.items():
        if not isinstance(v, str):
            continue
        s = v.strip()
        if s.startswith("{"):
            try:
                parsed = json.loads(s)
            except (ValueError, RecursionError):
                continue
            found = _find_lccn_in(parsed)
            if found:
                return found, parsed

    # Last resort: substring scan for an sn######## token in any field.
    for v in rec.values():
        if isinstance(v, str):
            idx = v.find("sn")
            while idx != -1:
                cand = v[idx:idx + 10]
                if len(cand) == 10 and cand[2:].isdigit():
                    return cand, None
                idx = v.find("sn", idx + 1)
    return None, None


def _find_lccn_in(obj) -> str | None:
    """Recursively hunt an 'lccn' key (or sn-prefixed value) in parsed JSON."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == "lccn" and isinstance(v, str):
                return v
        for v in obj.values():
            found = _find_lccn_in(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj[:20]:
            found = _find_lccn_in(v)
            if found:
                return found
    elif isinstance(obj, str) and obj.startswith("sn") and obj[2:12].isdigit():
        return obj[:12] if len(obj) >= 12 else obj
    return None


def _describe_structure(rec: dict, meta: dict | None) -> str:
    lines = ["Top-level fields of one record:", "", "```"]
    for k, v in rec.items():
        preview = str(v)[:120].replace("\n", " ")
        lines.append(f"{k}: {type(v).__name__} = {preview}")
    lines.append("```")
    if meta is not None:
        lines.append("")
        lines.append("Parsed embedded-JSON metadata keys:")
        lines.append("")
        lines.append("```")
        lines.append(json.dumps(_shape_of(meta), indent=2)[:2000])
        lines.append("```")
    return "\n".join(lines)


def _shape_of(obj, depth: int = 0):
    """Types-only skeleton of nested JSON, for the structure dump."""
    if depth > 3:
        return "..."
    if isinstance(obj, dict):
        return {k: _shape_of(v, depth + 1) for k, v in list(obj.items())[:15]}
    if isinstance(obj, list):
        return [_shape_of(obj[0], depth + 1)] if obj else []
    return type(obj).__name__


def _trim_record(rec: dict, meta: dict | None) -> dict:
    """Sample record with long strings trimmed for the report."""
    out = {}
    for k, v in rec.items():
        if isinstance(v, str) and len(v) > SAMPLE_TEXT_TRIM:
            out[k] = v[:SAMPLE_TEXT_TRIM] + f"… [{len(v):,} chars total]"
        else:
            out[k] = v
    if meta is not None:
        # Surface likely join-key fields from the parsed metadata.
        for k in ("lccn", "edition", "date", "page", "page_number",
                  "sequence", "scan_id", "id", "batch", "newspaper_name"):
            v = _find_key(meta, k)
            if v is not None:
                out[f"meta.{k}"] = str(v)[:200]
    return out


def _find_key(obj, wanted: str):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() == wanted:
                return v
        for v in obj.values():
            found = _find_key(v, wanted)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj[:5]:
            found = _find_key(v, wanted)
            if found is not None:
                return found
    return None


def _finish(lines: list[str]) -> None:
    report = "\n".join(lines)
    REPORT_PATH.write_text(report)
    print(report)
    print(f"\nWrote {REPORT_PATH}", file=sys.stderr)


if __name__ == "__main__":
    main()
