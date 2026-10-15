#!/usr/bin/env python
"""Build the policy atlas: a baseline manual, and what each district did to it.

Feeds the web page at `/policy`. Writes a single static JSON so the page needs
no database and loads while Supabase is asleep.

**There is no published NYS model manual in this corpus, so the baseline is
derived.** The districts license their policy from the same vendors, so the
manual they hold *in common* is operationally the model: a policy number eight
districts all carry is the template, one that two carry is somebody's local
addition. That makes the baseline an empirical object with a stated definition
rather than a document we are pretending to have.

Four things happen to a template policy, and the page needs all four:

* **kept** — the district holds it and its text still matches its peers. The
  copy-paste case, and by far the most common.
* **rewritten** — holds it, but the text has moved away from the consensus.
  This is where the credit thresholds and the agenda-notice windows live.
* **added** — holds a policy few peers do. Local invention.
* **missing** — peers hold it and this district does not. White Plains has no
  credit threshold; Ossining has no provision at all.

Similarity is word-shingle Jaccard, not an embedding and not difflib. Embeddings
are actively wrong here — two policies differing only in a number sit at cosine
~0.999 (see `herald.policy_compare`) — and difflib on 2,555 documents of 5-40 KB
is quadratic in the wrong place. Shingles are linear, exact about verbatim
reuse, and that is the question: did they copy this, or write it?

    uv run scripts/policy_atlas.py
"""

from __future__ import annotations

import datetime as dt
import gzip
import itertools
import json
import pathlib
import re
import sys
from collections import defaultdict

import typer

from herald.html_text import extract_html_text
from herald.policy_compare import (
    COMPETING_CONSEQUENCE,
    Quantity,
    CREDIT_DENIAL,
    CREDIT_DISCRETION,
    CREDIT_UNITS,
    clauses,
    divergences,
    policy_number,
    rule_parameter,
)

app = typer.Typer(add_completion=False)

SNAPSHOTS = (
    pathlib.Path("data/snapshots/boarddocs-policies.jsonl.gz"),
    pathlib.Path("data/snapshots/policy-manuals.jsonl.gz"),
)
OUT = pathlib.Path("web/public/policy-atlas.json")

#: The NYSSBA / Erie 1 BOCES series, used to *group the display* only.
#:
#: It cannot align policies across districts, and the measurement that proved
#: it is worth keeping: aligning by number made Greenburgh look like it had
#: invented 224 policies and lost 102, which is not divergence but a different
#: filing scheme. Six districts file students as 5000 and personnel as 9000;
#: Greenburgh and Mount Vernon use the older scheme where students are 7000 and
#: the 9000 series is **empty** (0 policies each). Attendance is 5100 in one
#: group and 7110 in the other.
#:
#: So subjects are aligned by text — see ``align_subjects`` — and the series
#: label shown for a subject is the one its holders most commonly use.
SERIES = {
    "0000": "District philosophy & board bylaws",
    "1000": "Community, elections & public relations",
    "2000": "Board governance & the policy process",
    "3000": "Administration",
    "4000": "Instruction & curriculum",
    "5000": "Students",
    "6000": "Fiscal & business",
    "7000": "Student health, welfare & attendance",
    "8000": "Buildings, operations & safety",
    "9000": "Personnel",
}

#: A policy this many districts hold is template rather than local. Eight
#: districts, so five is a clear majority and leaves room for the two that file
#: a subject under a different number.
CORE_MIN = 5

#: Held by this many or fewer: a local addition rather than the vendor's.
LOCAL_MAX = 2

#: Shingle length in words. Five is long enough that ordinary phrases
#: ("the Board of Education shall") do not match by accident and short enough
#: to survive a district inserting a clause mid-paragraph.
SHINGLE = 5

#: Jaccard at or above this is verbatim reuse — the same document with edits.
#: Below it the district has rewritten the policy rather than adopted it.
KEPT_FLOOR = 0.55


#: ``subject_score`` at or above this is the same subject. Not comparable to
#: ``KEPT_FLOOR``: that one is exact-wording reuse, this one is a blend of title
#: and vocabulary overlap with a bonus for a shared policy number. A subject
#: group has to be able to hold a district's rewrite next to its peers'
#: original, so this floor cannot depend on the wording matching.
SUBJECT_FLOOR = 0.34

#: A shared policy number adds this much, and only to a pair that already shows
#: content agreement. Small enough that two unrelated policies filed under one
#: number stay apart, large enough to pull a borderline true pair together.
NUMBER_BONUS = 0.15
NUMBER_BONUS_MIN = 0.18

#: A shingle in more than this share of all policies is boilerplate — "the
#: board of education shall", "in accordance with law" — and matches
#: everything. Dropping it from the index is what keeps candidate generation
#: cheap and stops unrelated policies looking similar.
MAX_SHINGLE_DF = 0.25


def shingles(text: str, n: int = SHINGLE) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    if len(words) < n:
        return {" ".join(words)} if words else set()
    return {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def text_of(row: dict) -> str:
    if row.get("text"):
        return row["text"]
    return extract_html_text(row["html"]).text if row.get("html") else ""


def load() -> list[dict]:
    rows: list[dict] = []
    for path in SNAPSHOTS:
        if not path.exists():
            typer.echo(f"note: missing {path}", err=True)
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh]
    return rows


def series_of(number: str) -> str:
    return (number[0] + "000") if number and number[0].isdigit() else "0000"


#: Words that carry no subject information in a policy title.
_TITLE_NOISE = re.compile(
    r"\b(?:policy|policies|regulation|regulations|exhibit|appendix|the|a|an|and|or|of|"
    r"for|to|in|on|at|by|with|district|board|education|school|schools|student|students)\b",
    re.I,
)


def title_tokens(title: str) -> set[str]:
    """Content words of a title, number and boilerplate stripped.

    "7110 Comprehensive Student Attendance Policy" and "5100 ATTENDANCE" reduce
    to {comprehensive, attendance} and {attendance}: overlapping, which is the
    point, since those are the same subject filed under two schemes.
    """
    body = re.sub(r"^\s*[\d.]+(?:-[A-Za-z.\d]+)?\s*", " ", title or "")
    return {w for w in re.findall(r"[a-z]+", _TITLE_NOISE.sub(" ", body).lower())
            if len(w) > 2}


def word_bag(text: str) -> set[str]:
    """Distinct words of four letters or more, before document-frequency filtering."""
    return {w for w in re.findall(r"[a-z]{4,}", (text or "").lower())}


#: A word this common across the manuals carries no subject information.
#: Policy prose is saturated with shared governance vocabulary — board,
#: district, education, shall, superintendent, meeting — and leaving it in the
#: bag inflated the overlap between any two governance policies enough to chain
#: "Purchasing Authority" onto "Board Member Authority" through the single
#: shared title word "authority".
MAX_WORD_DF = 0.20


def discriminating(bags: list[set[str]], *, max_df: float = MAX_WORD_DF) -> list[set[str]]:
    df: dict[str, int] = defaultdict(int)
    for bag in bags:
        for w in bag:
            df[w] += 1
    cap = max(2, int(len(bags) * max_df))
    return [{w for w in bag if df[w] <= cap} for bag in bags]


def subject_score(a: dict, b: dict) -> float:
    """How likely two policies are the same subject, in [0, 1].

    Deliberately *not* the 5-word shingle similarity used for copy-paste
    detection. Those two jobs pull in opposite directions and conflating them
    broke the first build: Greenburgh's 7110 and Mount Vernon's 7110 — same
    number, same title, same subject — scored under the shingle floor and
    landed in separate groups, because each district had written its own
    escalation procedure around the shared rule. Exact 5-grams measure reuse of
    wording, which is the thing we want to *report*, not the thing to group by.

    So: title overlap carries most of the weight (vendor titles survive
    renumbering) and vocabulary overlap carries the rest.

    **A shared policy number corroborates; it never substitutes.** Making it
    sufficient on its own merged three unrelated policies that happen to be
    numbered 1120 — Greenburgh's "Board of Education Members: Qualification",
    Mount Vernon's "Board of Education Authority" and White Plains' "School
    District Records" — into one subject held by all eight districts. Districts
    reuse numbers for different subjects as freely as they file one subject
    under different numbers, so the bonus only applies where the content
    already agrees somewhat, and it cannot clear the floor by itself.
    """
    titles = jaccard(a["title_tokens"], b["title_tokens"])
    words = jaccard(a["words"], b["words"])
    score = 0.6 * titles + 0.4 * words
    if a["number"] and a["number"] == b["number"] and score >= NUMBER_BONUS_MIN:
        score += NUMBER_BONUS
    return min(score, 1.0)


def align_subjects(docs: list[dict]) -> list[list[int]]:
    """Group policies that are the same subject across districts.

    Text, not policy number, because the number means different things in
    different districts. The premise that makes this work is the same one that
    makes the whole comparison interesting: these are licensed templates, so
    two districts' copies of one policy share most of their wording even when
    one has edited it.

    **Mutual best match per district pair, then union-find.** Taking every edge
    above a similarity floor over-merges badly — a policy manual is full of
    cross-references and shared recitals, so "Student Attendance" links to
    "Attendance Accounting" links to "Student Records" and a third of the manual
    collapses into one blob. Requiring each side to be the other's *best* match
    within that district pair keeps it a one-to-one alignment, which is what a
    subject is.

    Returns groups as lists of indices into ``docs``; singletons included.
    """
    # Candidates: any cross-district pair sharing a title word or a number.
    # An inverted index on title tokens rather than text shingles, because the
    # title is what survives a district renumbering its whole manual.
    by_token: dict[str, list[int]] = defaultdict(list)
    for i, doc in enumerate(docs):
        for token in doc["title_tokens"]:
            by_token[token].append(i)
        if doc["number"]:
            by_token["#" + doc["number"]].append(i)

    pairs: set[tuple[int, int]] = set()
    for token, holders in by_token.items():
        # A title word shared by hundreds of policies ("services", "programs")
        # generates candidates without discriminating between them. A policy
        # *number* is never dropped, however many districts file it.
        if len(holders) > 60 and not token.startswith("#"):
            continue
        for a_i, a in enumerate(holders):
            for b in holders[a_i + 1:]:
                if docs[a]["district"] != docs[b]["district"]:
                    pairs.add((a, b) if a < b else (b, a))

    best: dict[tuple[int, str], tuple[float, int]] = {}
    for a, b in pairs:
        score = subject_score(docs[a], docs[b])
        if score < SUBJECT_FLOOR:
            continue
        for x, y in ((a, b), (b, a)):
            key = (x, docs[y]["district"])
            if key not in best or score > best[key][0]:
                best[key] = (score, y)

    parent = list(range(len(docs)))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    # Average-link, strongest edge first: a candidate joins a group only if it
    # agrees with the group as a whole. Plain union-find on mutual-best edges
    # chains — one weak edge merged five "Purchasing Authority" policies with
    # three "Board Member Authority" ones into a single subject held by all
    # eight districts, because for the districts lacking a board-authority
    # policy the only candidate left in a peer district was a purchasing one.
    members: dict[int, list[int]] = {i: [i] for i in range(len(docs))}

    def group_score(x: int, y: int) -> float:
        a, b = members[find(x)], members[find(y)]
        return sum(subject_score(docs[i], docs[j]) for i in a for j in b) / (len(a) * len(b))

    edges = []
    for (x, _other), (score, y) in best.items():
        back = best.get((y, docs[x]["district"]))
        if back and back[1] == x:
            edges.append((score, min(x, y), max(x, y)))
    for _score, x, y in sorted(edges, reverse=True):
        rx, ry = find(x), find(y)
        if rx == ry:
            continue
        # One policy per district per subject.
        if {docs[i]["district"] for i in members[rx]} & {
                docs[i]["district"] for i in members[ry]}:
            continue
        if group_score(x, y) < SUBJECT_FLOOR:
            continue
        merged = members[rx] + members[ry]
        union(x, y)
        members[find(x)] = merged


    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(docs)):
        groups[find(i)].append(i)
    return list(groups.values())


@app.command()
def main(
    out: pathlib.Path = typer.Option(OUT, help="JSON the web page reads."),
    kept_floor: float = typer.Option(KEPT_FLOOR, help="Jaccard floor for 'kept'."),
) -> None:
    rows = load()
    if not rows:
        typer.echo("no policy snapshots found", err=True)
        raise typer.Exit(1)

    districts = sorted({r["district"] for r in rows})
    docs: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        body = text_of(r)
        number = policy_number(r["title"])
        if len(body) < 200:
            continue
        # A district occasionally files one number twice (a policy and its
        # regulation sharing a stem); keep the longer, which is the policy.
        key = (r["district"], number or r["title"])
        if key in seen:
            continue
        seen.add(key)
        docs.append({
            "district": r["district"], "number": number, "title": r["title"],
            "text": body, "url": r.get("source_url") or "",
            "shingles": shingles(body),
            "title_tokens": title_tokens(r["title"]),
            "words": word_bag(body),
        })

    # Document-frequency filtering needs the whole corpus, so it happens once
    # the docs are collected rather than per policy.
    for doc, bag in zip(docs, discriminating([d["words"] for d in docs])):
        doc["words"] = bag

    typer.echo(f"{len(rows)} policies, {len(docs)} after de-duplication, "
               f"{len(districts)} districts")
    groups = align_subjects(docs)
    typer.echo(f"{len(groups)} subject groups")

    subjects: list[dict] = []
    tally: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {"kept": 0, "rewritten": 0, "added": 0, "missing": 0}
    )

    for members in groups:
        by_district: dict[str, int] = {}
        for i in members:
            d = docs[i]["district"]
            # One policy per district per subject: the longest, if a district's
            # policy and its regulation both landed in the group.
            if d not in by_district or len(docs[i]["text"]) > len(docs[by_district[d]]["text"]):
                by_district[d] = i
        n_held = len(by_district)
        kind = ("core" if n_held >= CORE_MIN
                else "local" if n_held <= LOCAL_MAX else "common")

        # The series the holders most commonly file this subject under, and the
        # clearest title among them. Both are display labels, not keys.
        series_votes = defaultdict(int)
        for i in by_district.values():
            series_votes[series_of(docs[i]["number"])] += 1
        series = max(series_votes.items(), key=lambda kv: (kv[1], kv[0]))[0]
        canonical = max((docs[i]["title"] for i in by_district.values()), key=len)

        # Worst pairwise grouping score in the group: average-link accepts a
        # merge on the average, so one member can sit well below the floor.
        # Published rather than hidden, so the page can mark a loose group and
        # a reader can judge it. Across 148 core subjects the median is 0.62
        # and 21 fall under the 0.34 floor.
        ms = list(by_district.values())
        confidence = min((subject_score(docs[i], docs[j])
                          for i, j in itertools.combinations(ms, 2)), default=1.0)

        per_district: dict[str, dict] = {}
        for d, i in by_district.items():
            peers = [docs[j]["shingles"] for pd, j in by_district.items() if pd != d]
            best = max((jaccard(docs[i]["shingles"], p) for p in peers), default=0.0)
            state = "added" if n_held <= LOCAL_MAX else (
                "kept" if best >= kept_floor else "rewritten")
            per_district[d] = {
                "state": state, "similarity": round(best, 3),
                "chars": len(docs[i]["text"]), "number": docs[i]["number"],
                "title": docs[i]["title"], "url": docs[i]["url"],
            }
            tally[(d, series)][state] += 1

        # Only a core subject's absence is a statement; nobody is "missing" a
        # policy two districts invented.
        absent = [d for d in districts if d not in by_district] if kind == "core" else []
        for d in absent:
            tally[(d, series)]["missing"] += 1

        subjects.append({
            "id": len(subjects), "series": series, "title": canonical,
            "kind": kind, "held_by": sorted(by_district), "absent": absent,
            "confidence": round(confidence, 3),
            "numbers": sorted({docs[i]["number"] for i in by_district.values()
                               if docs[i]["number"]}),
            "districts": per_district,
        })

    # Parameter divergence, reusing the comparison that is already tested.
    params: dict[str, list[dict]] = defaultdict(list)
    for subject in subjects:
        if len(subject["held_by"]) < 2:
            continue
        members = {d: next(i for i in range(len(docs))
                           if docs[i]["district"] == d
                           and docs[i]["title"] == subject["districts"][d]["title"])
                   for d in subject["held_by"]}
        cl = {d: clauses(docs[i]["text"]) for d, i in members.items()}
        names = sorted(members)
        for a_i, a in enumerate(names):
            for b in names[a_i + 1:]:
                for div in divergences(str(subject["id"]), subject["title"],
                                       a, cl[a], b, cl[b]):
                    params[str(subject["id"])].append({
                        "kind": div.kind, "wording": round(div.wording, 3),
                        "a": a, "b": b,
                        "a_values": [str(q) for q in div.quantities_a],
                        "b_values": [str(q) for q in div.quantities_b],
                        "clause_a": div.clause_a[:400],
                        "clause_b": div.clause_b[:400],
                    })

    # The worked example the page opens on, because it is the one a reader can
    # check against their own experience of school.
    credit: list[dict] = []
    for d in districts:
        found = None
        discretion = None
        for doc in docs:
            if doc["district"] != d or "attendance" not in doc["title"].lower():
                continue
            got = rule_parameter(d, doc["title"], doc["text"],
                                 consequence=CREDIT_DENIAL, units=CREDIT_UNITS,
                                 exclude=COMPETING_CONSEQUENCE)
            if got and (found is None or (got.value or 0) > (found["value"] or 0)):
                found = {"value": got.value, "unit": got.unit,
                         "policy": doc["title"], "quote": got.quote[:420],
                         "url": doc["url"]}
            if discretion is None and CREDIT_DISCRETION.search(
                    re.sub(r"\s+", " ", doc["text"])):
                discretion = {"policy": doc["title"], "url": doc["url"]}
        credit.append({
            "district": d,
            "value": found["value"] if found else None,
            "unit": found["unit"] if found else "",
            # Preformatted, because Quantity knows ATED is an acronym and the
            # page naively pluralising the raw unit printed "18 ateds".
            "label": str(Quantity(found["value"], found["unit"])) if found else "",
            "policy": (found or discretion or {}).get("policy", ""),
            "quote": (found or {}).get("quote", ""),
            "url": (found or discretion or {}).get("url", ""),
            "standard": ("bright line" if found
                         else "administrator discretion" if discretion
                         else "no provision"),
        })

    atlas = {
        "generated": dt.date.today().isoformat(),
        "sources": [str(s) for s in SNAPSHOTS if s.exists()],
        "as_of": {"boarddocs-policies": "2026-08-20", "policy-manuals": "2026-10-12"},
        "districts": districts,
        # Two districts number their manual on an older scheme: students are
        # 7000 rather than 5000 and the 9000 personnel series is empty. The
        # series a subject is shown under follows the majority of its holders,
        # so for these two a category label can disagree with their own
        # filing. An empirical crosswalk was tried and is only 53-80% reliable
        # per series, which would trade a visible problem for an invisible
        # one, so the page marks them instead.
        "old_scheme": ["greenburgh-central", "mount-vernon"],
        "series": SERIES,
        "thresholds": {"core_min": CORE_MIN, "local_max": LOCAL_MAX,
                       "kept_floor": kept_floor, "shingle": SHINGLE},
        "counts": {
            "policies": len(rows), "policies_used": len(docs),
            "subjects": len(subjects),
            "core": sum(1 for n in subjects if n["kind"] == "core"),
            "common": sum(1 for n in subjects if n["kind"] == "common"),
            "local": sum(1 for n in subjects if n["kind"] == "local"),
        },
        "tally": [{"district": d, "series": s, **v} for (d, s), v in sorted(tally.items())],
        "subjects": subjects,
        "parameters": params,
        "credit_example": credit,
    }
    # The shingle sets are working data, not output.
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(atlas, separators=(",", ":")))

    typer.echo(f"\ncore {atlas['counts']['core']}, common {atlas['counts']['common']}, "
               f"local {atlas['counts']['local']}")
    typer.echo(f"{sum(len(v) for v in params.values())} parameter divergences")
    typer.echo(f"-> {out} ({out.stat().st_size / 1024:.0f} KB)")
    typer.echo(f"\n{'district':<20}{'kept':>7}{'rewritten':>11}{'added':>7}{'missing':>9}")
    for d in districts:
        agg = {k: sum(tally[(d, s)][k] for s in SERIES) for k in
               ("kept", "rewritten", "added", "missing")}
        typer.echo(f"{d:<20}{agg['kept']:>7}{agg['rewritten']:>11}"
                   f"{agg['added']:>7}{agg['missing']:>9}")


if __name__ == "__main__":
    sys.exit(app())
