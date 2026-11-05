#!/usr/bin/env python
"""Pilot: can a board action be told apart from the routine by its peers?

The policy comparison works because every district files the same documents, so
a clause has an obvious counterpart next door. A board *action* has no such
anchor — districts meet on different nights and buy different things — so the
question is whether a peer group can still say "every district does this every
month" against "only this one did this".

**Leave one district out.** For each item in district D, find its closest
counterpart among the items of the other districts and keep the distance. An
action the peers also take scores low however often D repeats it; an action
nobody else takes scores high. Scoring a district against its own history
instead would only measure repetition, and the earlier thread experiment showed
repetition is what that finds: 20 of 20 candidate threads came back "not a
story", most of them annual repeats.

**Furniture first.** Roll call, the pledge, adjournment and executive session
are 3,924 of 17,577 items, perfectly routine, and would pass any novelty test
by being identical everywhere.

Two arms, and the first one's failure is why the second exists. Phrase matching
on subject bigrams put 60% of items at the ceiling with no counterpart at all,
and topped its ranking with "Approval of Meal Prices for the 2026-2027" — an
annual item every district files, whose peer writes "lunch prices" and shares
no bigram with it. Subject-line embeddings, measured at 95.9% nearest-neighbour
accuracy against 74.7% for whole chunks, have no ceiling and put External
Auditor, Purchasing Agent and Official Newspaper at the bottom where they
belong. The phrase arm stays as the baseline the comparison needs.

The deliverable is an evaluation set, not a verdict: ``--sample`` writes a
blind, stratified set for a human to score.

    uv run scripts/action_novelty.py --probe
    uv run scripts/action_novelty.py --sample 30 --out eval_actions.json
"""

from __future__ import annotations

import collections
import gzip
import json
import pathlib
import random
import re
import sys

import typer

from herald.braids import split_agenda_items
from herald.html_text import extract_html_text

app = typer.Typer(add_completion=False)

SNAPSHOT = pathlib.Path("data/snapshots/boarddocs-agendas.jsonl.gz")

#: Procedural scaffolding. Every meeting has it, no meeting is about it.
#: Matched against the subject, not the body: a real action can mention
#: adjournment in passing.
FURNITURE = re.compile(
    r"^\s*(?:[a-z0-9.]+\s+)?(?:"
    r"pledge of allegiance|roll\s*call|adjourn|anticipated adjournment"
    r"|call (?:the )?meeting to order|opening|welcome"
    r"|(?:enter|adjourn)\w*\s+(?:in)?to executive session|executive session"
    r"|committee reports?|superintendent'?s? report|board (?:member )?comments?"
    r"|public (?:comment|participation|expression)|recess|reconvene"
    r"|approval of (?:the )?agenda|adoption of (?:the )?agenda"
    r"|a (?:new )?agenda item|a recording of attendance|attendance"
    r"|minutes?$|approval of minutes|acceptance of (?:the )?minutes"
    r"|old business|new business|future (?:meetings|agenda)|announcements"
    r"|next meeting|consent agenda|salute to the flag|moment of silence"
    r")\b",
    re.I,
)

#: Money, kept as written so a quote reads back the way the agenda stated it.
MONEY = re.compile(r"\$\s?([\d,]+(?:\.\d{2})?)")

#: A capitalised run ending in a company suffix is a plausible counterparty.
#: Crude on purpose: this pilot measures whether peer comparison works at all,
#: not entity extraction.
ORG = re.compile(
    r"\b([A-Z][A-Za-z&.\-']+(?:\s+[A-Z][A-Za-z&.\-']+){0,4}"
    r"\s*(?:,?\s*(?:Inc|LLC|LLP|L\.L\.C|Corp|Corporation|Company|Co|Ltd|"
    r"Associates|Partners|Group|Services|Solutions|Consulting|Architects|"
    r"Engineering|Technologies|Systems)\b\.?))"
)

STOP = {
    "the", "and", "for", "with", "from", "that", "this", "board", "district",
    "school", "schools", "education", "approval", "approve", "approved",
    "resolution", "resolved", "recommend", "recommended", "superintendent",
    "year", "years", "meeting", "new", "york", "inc", "llc", "services",
    "service", "agreement", "contract", "students", "student", "be", "it",
    "of", "to", "a", "an", "in", "on", "at", "by", "is", "are", "as", "or",
}

#: The body runs to the next item's preamble, so it can trail into the
#: following section's scaffolding.
BODY_TAIL = re.compile(
    r"\s*(?:File Attachments|Subject\s+\d|CLOSING ITEMS|Category\s+\d).*$",
    re.S | re.I,
)


def content_words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z'\-]{2,}", (text or "").lower())
            if w not in STOP]


def phrases(subject: str, n: int = 2) -> set[str]:
    """Content bigrams of a subject line — the unit the phrase arm matches on."""
    words = content_words(subject)
    out = {" ".join(words[i:i + n]) for i in range(len(words) - n + 1)}
    return out or set(words)


def is_furniture(subject: str) -> bool:
    return bool(FURNITURE.match((subject or "").strip()))


def clean_body(body: str) -> str:
    return BODY_TAIL.sub("", re.sub(r"\s+", " ", body or "").strip()).strip()


def load_items() -> list[dict]:
    rows: list[dict] = []
    with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as fh:
        for line in fh:
            r = json.loads(line)
            text = extract_html_text(r["html"]).text
            for item in split_agenda_items(text):
                rows.append({
                    "district": r["district"],
                    "date": (r.get("date") or "")[:10],
                    "subject": item.subject,
                    "body": clean_body(item.body)[:1200],
                    "category": item.category,
                    "amounts": MONEY.findall(item.body)[:4],
                    "orgs": sorted({m.group(1).strip()
                                    for m in ORG.finditer(item.body)})[:4],
                    "source_url": r.get("source_url", ""),
                })
    return rows


def score(items: list[dict]) -> None:
    """Attach each item's distance to its nearest counterpart in another district.

    Jaccard over subject bigrams. An inverted index keeps it tractable: only
    items sharing a phrase are compared, and a phrase in a large share of items
    says nothing about who does what.
    """
    index: dict[str, list[int]] = collections.defaultdict(list)
    for i, it in enumerate(items):
        for p in it["phrases"]:
            index[p].append(i)
    common = {p for p, v in index.items() if len(v) > len(items) * 0.02}

    for it in items:
        best, who = 0.0, None
        seen: set[int] = set()
        for p in it["phrases"] - common:
            for j in index[p]:
                if j in seen or items[j]["district"] == it["district"]:
                    continue
                seen.add(j)
                other = items[j]["phrases"]
                sim = len(it["phrases"] & other) / len(it["phrases"] | other)
                if sim > best:
                    best, who = sim, j
        it["peer_similarity"] = round(best, 3)
        it["novelty"] = round(1.0 - best, 3)
        it["nearest"] = (
            {"district": items[who]["district"], "date": items[who]["date"],
             "subject": items[who]["subject"][:90]} if who is not None else None
        )


def score_embedded(items: list[dict], cache: pathlib.Path) -> None:
    """The same leave-one-out question, asked with embeddings.

    Only distinct subjects are embedded — 8,175 of 13,653 items — because a
    district repeating an item monthly does not change what it means.
    """
    import numpy as np

    uniq: dict[str, int] = {}
    for it in items:
        uniq.setdefault(it["subject"].strip(), len(uniq))
    texts = list(uniq)

    vecs = None
    if cache.exists():
        held = np.load(cache, allow_pickle=True)
        if list(held["texts"]) == texts:
            vecs = held["vecs"]
    if vecs is None:
        import asyncio
        import os

        from herald.embed import VoyageEmbedder

        typer.echo(f"embedding {len(texts):,} distinct subjects")

        async def run():
            async with VoyageEmbedder(os.environ["VOYAGE_API_KEY"]) as v:
                return await v.embed_documents(texts)

        vecs = np.array(asyncio.run(run()), dtype=np.float32)
        vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.savez(cache, vecs=vecs, texts=np.array(texts, dtype=object))

    holders: list[set[str]] = [set() for _ in texts]
    for it in items:
        holders[uniq[it["subject"].strip()]].add(it["district"])

    districts = sorted({it["district"] for it in items})
    best_other = np.zeros(len(texts), dtype=np.float32)
    best_idx = np.full(len(texts), -1, dtype=np.int64)
    for d in districts:
        mine = np.array([i for i, h in enumerate(holders) if d in h])
        # A subject some *other* district also uses is a counterpart even when
        # this district uses it too, so the comparison set is "held by anyone
        # but D", not "not held by D".
        theirs = np.array([i for i, h in enumerate(holders) if h - {d}])
        if not len(mine) or not len(theirs):
            continue
        for start in range(0, len(mine), 512):
            block = mine[start:start + 512]
            sims = vecs[block] @ vecs[theirs].T
            top = sims.argmax(axis=1)
            vals = sims[np.arange(len(block)), top]
            better = vals > best_other[block]
            best_other[block[better]] = vals[better]
            best_idx[block[better]] = theirs[top[better]]

    for it in items:
        k = uniq[it["subject"].strip()]
        it["peer_similarity_embed"] = round(float(best_other[k]), 3)
        it["novelty_embed"] = round(1.0 - float(best_other[k]), 3)
        j = int(best_idx[k])
        it["nearest_embed"] = (
            {"subject": texts[j][:90], "districts": sorted(holders[j])}
            if j >= 0 else None
        )


def normalise_by_district(items: list[dict], key: str = "novelty_embed") -> None:
    """Rank each item within its own district's distribution.

    The raw ranking is substantially a measure of house style. Peekskill names
    the person in the subject line — "Contract - Roy Escobar (DJ Esco)" — where
    its peers write "Approval of a Professional Services Agreement", so its
    items look unlike anybody else's by construction: Peekskill is 24% of the
    corpus and 56% of the raw top 200, a 2.3x over-representation, against
    Greenburgh's 0.4x.

    A within-district percentile removes that offset while keeping the
    leave-one-out comparison, which is still made against *other* districts.
    The assumption it carries, and it should be said out loud, is that no
    district is a priori likelier than another to do something unusual: the
    correction forces the top of the ranking to be proportional to district
    size rather than discovering that it is.
    """
    by_district: dict[str, list[dict]] = collections.defaultdict(list)
    for it in items:
        by_district[it["district"]].append(it)
    for group in by_district.values():
        group.sort(key=lambda r: r[key])
        last = max(len(group) - 1, 1)
        for rank, it in enumerate(group):
            it["novelty_rank"] = round(rank / last, 4)


def prepare() -> list[dict]:
    raw = load_items()
    kept = [r for r in raw if not is_furniture(r["subject"])
            and len(r["subject"]) > 14]
    removed = len(raw) - len(kept)
    typer.echo(f"{len(raw):,} items -> {len(kept):,} after furniture "
               f"({removed:,} removed, {removed / max(len(raw), 1):.0%})")
    for r in kept:
        r["phrases"] = phrases(r["subject"])
    kept = [r for r in kept if r["phrases"]]
    score(kept)
    return kept


def distribution(items: list[dict], key: str, label: str) -> None:
    buckets = collections.Counter(round(r[key], 1) for r in items)
    typer.echo(f"\n{label} (1.0 = no counterpart in any other district)")
    for b in sorted(buckets):
        bar = "#" * int(60 * buckets[b] / max(len(items), 1))
        typer.echo(f"   {b:>4}  {buckets[b]:>6,}  {bar}")
    ceiling = sum(1 for r in items if r[key] >= 0.995)
    typer.echo(f"   at the ceiling: {ceiling:,} ({ceiling / len(items):.0%})")


@app.command()
def main(
    probe: bool = typer.Option(False, help="Print the distributions and examples."),
    embed: bool = typer.Option(True, help="Also run the embedding arm."),
    cache: pathlib.Path = typer.Option(
        pathlib.Path("scripts/.action_subjects.npz"), help="Embedding cache."),
    sample: int = typer.Option(0, help="Write a blind stratified evaluation set."),
    out: pathlib.Path = typer.Option(
        pathlib.Path("eval_actions.json"), help="Where to write the sample."),
    seed: int = typer.Option(11, help="Sampling seed, so the set is reproducible."),
) -> None:
    if not SNAPSHOT.exists():
        typer.echo(f"no agenda snapshot at {SNAPSHOT}", err=True)
        raise typer.Exit(1)
    items = prepare()

    by_district = collections.Counter(r["district"] for r in items)
    typer.echo(f"{len(items):,} scored items across {len(by_district)} districts")
    for d, c in by_district.most_common():
        typer.echo(f"   {d:<20}{c:>7,}")

    distribution(items, "novelty", "PHRASE arm")
    if embed:
        score_embedded(items, cache)
        distribution(items, "novelty_embed", "EMBEDDING arm")
        normalise_by_district(items)
        skew = collections.Counter(
            r["district"] for r in
            sorted(items, key=lambda r: -r["novelty_embed"])[:200])
        typer.echo("\ndistrict share of the raw top 200 against the corpus "
                   "(house style, not divergence)")
        for d, c in by_district.most_common():
            share = c / len(items)
            typer.echo(f"   {d:<20}{share:>6.0%} corpus "
                       f"{skew[d] / 200:>6.0%} top   "
                       f"{skew[d] / 200 / share:>5.1f}x")

    if probe:
        rank_key = "novelty_embed" if embed else "novelty"
        typer.echo(f"\n--- most novel by {rank_key} (no peer does this) ---")
        for r in sorted(items, key=lambda r: -r[rank_key])[:12]:
            money = (f"  [{', '.join('$' + a for a in r['amounts'][:2])}]"
                     if r["amounts"] else "")
            typer.echo(f"   {r[rank_key]:.2f} {r['district']:<18}{r['date']}  "
                       f"{r['subject'][:62]}{money}")
        typer.echo(f"\n--- least novel by {rank_key} (every district does this) ---")
        for r in sorted(items, key=lambda r: r[rank_key])[:12]:
            typer.echo(f"   {r[rank_key]:.2f} {r['district']:<18}{r['date']}  "
                       f"{r['subject'][:62]}")
            near = r.get("nearest_embed") if embed else r.get("nearest")
            if near:
                who = near.get("districts") or [near.get("district")]
                typer.echo(f"          ~ {'/'.join(who)}: {near['subject'][:58]}")

    if sample:
        rng = random.Random(seed)
        # Sampled on the district-normalised rank so one district's naming
        # convention does not fill the evaluation set.
        ranked = sorted(items, key=lambda r: -r.get(
            "novelty_rank", r.get("novelty_embed", r["novelty"])))
        third = max(1, sample // 3)
        # Three strata, shuffled together and stripped of their scores. The
        # previous round drew only from the top, which selects for exactly the
        # repetition the method is meant to avoid and cannot show whether the
        # ranking carries signal at all.
        chosen = (
            [dict(r, stratum="top") for r in rng.sample(ranked[:200], third)]
            + [dict(r, stratum="middle")
               for r in rng.sample(ranked[len(ranked) // 2 - 100:
                                          len(ranked) // 2 + 100], third)]
            + [dict(r, stratum="random")
               for r in rng.sample(ranked, sample - 2 * third)]
        )
        rng.shuffle(chosen)
        payload = [
            {"id": i, "district": c["district"], "date": c["date"],
             "subject": c["subject"], "body": c["body"][:600],
             "amounts": c["amounts"], "orgs": c["orgs"],
             "source_url": c["source_url"],
             # Kept for scoring afterwards; never shown to the reader.
             "_stratum": c["stratum"], "_novelty": c["novelty"],
             "_novelty_embed": c.get("novelty_embed"),
             "_novelty_rank": c.get("novelty_rank"),
             "_nearest": c["nearest"], "_nearest_embed": c.get("nearest_embed")}
            for i, c in enumerate(chosen)
        ]
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=1))
        typer.echo(f"\nwrote {len(payload)} items to {out} "
                   f"({third} top, {third} middle, {sample - 2 * third} random)")


if __name__ == "__main__":
    sys.exit(app())
