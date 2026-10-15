"""Cross-district drift on one class of compliance document.

The question the corpus cannot answer one district at a time: did this district
move because Albany moved, or because it chose to? That ambiguity is what made
the 2024 "simulations, props, actors" language read as a local decision when it
was a state ban on simulated drills. Seven districts under one state can tell
them apart — what everybody absorbs in the same year is the mandate, what one
district does alone is a decision.

Three outputs:

  position   how far a district sits from its peers that year
  velocity   how far it moved against how far the consensus moved; moving when
             nobody else did is a choice, standing still when everybody moved
             is a compliance lag
  what moved for the largest consensus shift, the passages that have no
             near-neighbour in the prior year — the vocabulary that entered

**The validation test is the velocity table, not the position table.** Chapter
155.7 was amended for 2024-25 and Peekskill named its file "2024-25 ... w/
155.7 updates", so 2023-24 -> 2024-25 must come out as the largest consensus
movement in the series. A method that cannot recover a mandate it was told
about will not find a quiet local choice.

Sections, not whole documents: Port Chester's plan runs 69 pages and
Elmsford's 2022-23 runs about 10, so a whole-document distance measures format.

OCR provenance is carried through and printed. Three plans exist only as page
scans, and OCR noise inflates distance exactly like real change does — an
untagged OCR'd document is the most likely source of a false "moved alone".
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import sys
from collections import defaultdict

import numpy as np

from herald.embed import VoyageEmbedder

SCRATCH = pathlib.Path(os.environ.get("DRIFT_PILOT_DIR", "/tmp/drift-pilot"))
PLANS = SCRATCH / "plans"
CACHE = PLANS / "vecs.npz"

#: Page furniture that would otherwise dominate short sections.
FURNITURE = re.compile(
    r"Page\s+\d+\s+of\s+\d+|^\s*\d+\s*$|District[- ]?wide School Safety Plan\s*\d*",
    re.I | re.M,
)


def sections(text: str, target: int = 1400) -> list[str]:
    """Split a plan into comparable passages of roughly ``target`` characters.

    Sentence boundaries alone are not enough. A plan that is mostly tables,
    checklists and rosters carries almost no terminal punctuation, so one
    "sentence" can run for thousands of characters: Peekskill's 2020-21 plan
    produced 26 sections from 87,000 characters where its own 2021-22 produced
    63. Those sections are not comparable to anybody else's, and mean-pooling a
    handful of enormous vectors is not the same centroid as pooling sixty
    ordinary ones. So an overlong run is cut on whitespace at the target.
    """
    body = FURNITURE.sub(" ", text)
    body = re.sub(r"\s+", " ", body).strip()
    pieces: list[str] = []
    for sent in re.split(r"(?<=[.;:])\s+", body):
        while len(sent) > target * 1.6:
            cut = sent.rfind(" ", 0, target)
            pieces.append(sent[:cut if cut > target // 2 else target])
            sent = sent[cut if cut > target // 2 else target:].lstrip()
        pieces.append(sent)

    out: list[str] = []
    cur = ""
    for piece in pieces:
        if len(cur) + len(piece) > target and cur:
            out.append(cur.strip())
            cur = ""
        cur += piece + " "
    if cur.strip():
        out.append(cur.strip())
    # A fragment under 300 chars is a heading or a stray table row, not a passage.
    return [s for s in out if len(s) > 300]


def load() -> tuple[list[str], list[tuple[str, str]], set[str]]:
    # Provenance is recorded by whatever produced the text, not inferred. File
    # size does not tell them apart: Peekskill's 2026-27 plan is a 5.8 MB PDF
    # with a perfectly good text layer, and guessing by size labelled it OCR.
    ocr_list = PLANS / "ocr.json"
    ocred: set[str] = set(json.loads(ocr_list.read_text())) if ocr_list.exists() else set()
    texts: list[str] = []
    keys: list[tuple[str, str]] = []
    print(f"{'district':<20}{'year':<9}{'chars':>9}{'sections':>10}  source")
    for p in sorted(PLANS.glob("*__*.txt")):
        stem = p.stem
        if "__" not in stem:
            continue
        district, yr = stem.rsplit("__", 1)
        if not re.fullmatch(r"20\d\d-\d\d", yr):
            continue
        text = p.read_text(errors="replace")
        secs = sections(text)
        if len(secs) < 5:
            print(f"{district:<20}{yr:<9}{len(text):>9,}{len(secs):>10}  SKIPPED (too thin)")
            continue
        print(f"{district:<20}{yr:<9}{len(text):>9,}{len(secs):>10}  "
              f"{'OCR' if stem in ocred else 'text'}")
        texts.extend(secs)
        keys.extend([(district, yr)] * len(secs))
    return texts, keys, ocred


async def embed(texts: list[str]) -> np.ndarray:
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        if len(z["texts"]) == len(texts) and list(z["texts"]) == texts:
            print(f"\n{len(texts)} sections from cache")
            return z["vecs"]
    print(f"\nembedding {len(texts)} sections")
    async with VoyageEmbedder(os.environ["VOYAGE_API_KEY"]) as v:
        vecs = np.array(await v.embed_documents(texts), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    np.savez(CACHE, vecs=vecs, texts=np.array(texts, dtype=object))
    return vecs


def unit(v: np.ndarray) -> np.ndarray:
    return v / (np.linalg.norm(v) + 1e-9)


def main() -> int:
    texts, keys, ocred = load()
    if not texts:
        print("nothing to analyse")
        return 1
    vecs = asyncio.run(embed(texts))

    rows = defaultdict(list)
    for i, k in enumerate(keys):
        rows[k].append(i)
    cent = {k: unit(vecs[idx].mean(axis=0)) for k, idx in rows.items()}
    years = sorted({yr for _, yr in cent})

    print("\n=== POSITION: distance from that year's peer consensus ===")
    consensus: dict[str, np.ndarray] = {}
    for yr in years:
        members = [(d, c) for (d, y), c in cent.items() if y == yr]
        if len(members) < 3:
            print(f"\n{yr}: {len(members)} district(s) — too few for a consensus")
            continue
        consensus[yr] = unit(np.mean([c for _, c in members], axis=0))
        print(f"\n{yr}  ({len(members)} districts)")
        for d, c in sorted(members, key=lambda x: -float(np.dot(x[1], consensus[yr]))):
            tag = "  [OCR]" if f"{d}__{yr}" in ocred else ""
            print(f"   {1 - float(np.dot(c, consensus[yr])):.4f}  {d}{tag}")

    print("\n=== VELOCITY: moved with the consensus, or alone? ===")
    moves: list[tuple[float, str, str]] = []
    for a, b in zip(years, years[1:]):
        if a not in consensus or b not in consensus:
            continue
        base = 1 - float(np.dot(consensus[a], consensus[b]))
        moves.append((base, a, b))
        print(f"\n{a} -> {b}    consensus moved {base:.4f}")
        for d in sorted({d for d, _ in cent}):
            if (d, a) in cent and (d, b) in cent:
                mv = 1 - float(np.dot(cent[(d, a)], cent[(d, b)]))
                flag = ("  <= MOVED ALONE" if mv > base * 1.8 else
                        "  <= stood still" if mv < base * 0.4 else "")
                tag = " [OCR]" if f"{d}__{b}" in ocred or f"{d}__{a}" in ocred else ""
                print(f"   {mv:.4f}  {d}{tag}{flag}")

    if not moves:
        print("\nno adjacent year pair has a peer set at both ends")
        return 0

    print("\n=== MANDATE TEST: largest consensus movement in the series ===")
    for base, a, b in sorted(moves, reverse=True):
        print(f"   {base:.4f}  {a} -> {b}")
    top = max(moves)
    print(f"\nlargest: {top[1]} -> {top[2]}  ({top[0]:.4f})")
    print("expected 2023-24 -> 2024-25 if the method recovers the 155.7 amendments")

    # What entered the consensus: passages in the later year with no close
    # counterpart in the earlier one, ranked by how far they sit from anything
    # said before. Shows whether the movement is new policy language or noise.
    _, a, b = top
    ai = [i for i, k in enumerate(keys) if k[1] == a]
    bi = [i for i, k in enumerate(keys) if k[1] == b]
    sim = vecs[bi] @ vecs[ai].T
    novelty = 1 - sim.max(axis=1)
    print(f"\n=== WHAT ENTERED in {b} (no near-neighbour in {a}) ===")
    for j in np.argsort(-novelty)[:12]:
        d, _ = keys[bi[j]]
        print(f"\n  [{novelty[j]:.3f}] {d}")
        print(f"    {texts[bi[j]][:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
