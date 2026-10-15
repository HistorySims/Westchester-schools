"""Which passages a district rewrote, by magnitude rather than by threshold.

``novelty.py`` counted sections with no near-neighbour in the previous year and
found nothing in 2024-25 — zero for Peekskill, one for Port Chester — in the
very year those districts tripled their trauma-informed drill language. The
count was measuring the wrong thing. The mandate did not arrive as new
sections; it arrived as sentences inserted into sections that already existed,
so each rewritten section's nearest neighbour is still its own previous
version at around 0.85 similarity, far under any threshold that template
reformatting does not also trip.

Magnitude works where a count does not. Pair every section with its closest
counterpart in the district's previous plan and keep the distance. Untouched
template sections land near 0.01, so a section at 0.15 stands out by an order
of magnitude without any threshold having to be chosen in advance.

The peer test is unchanged and still does the real work: a rewrite that peers
made the same year is the mandate, a rewrite nobody else made is a decision.
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from analyze import PLANS, load, unit  # noqa: E402,F401
from novelty import embed  # noqa: E402

import asyncio  # noqa: E402

#: The vocabulary Chapter 155.7 brought in for 2024-25 — a ban on simulated
#: drills using props or actors, and trauma-informed notice to parents. Used
#: only to label what the geometry finds, never to find it.
MANDATE_WORDS = re.compile(r"trauma|simulat|\bprops\b|\bactors\b|155\.7", re.I)


def main() -> int:
    texts, keys, ocred = load()
    vecs = asyncio.run(embed(texts))

    idx: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        idx[k].append(i)
    years = sorted({y for _, y in idx})

    rows = []
    for d in sorted({dd for dd, _ in idx}):
        mine = sorted(y for y in years if (d, y) in idx)
        for prev, yr in zip(mine, mine[1:]):
            cur_i = np.array(idx[(d, yr)])
            prev_i = np.array(idx[(d, prev)])
            dist = 1 - (vecs[cur_i] @ vecs[prev_i].T).max(axis=1)
            peer_i = np.array([i for (dd, yy), ii in idx.items()
                               if yy == yr and dd != d for i in ii])
            order = np.argsort(-dist)
            top = []
            for j in order[:8]:
                best_peer = (float((vecs[cur_i[j]] @ vecs[peer_i].T).max())
                             if len(peer_i) else 0.0)
                top.append({
                    "changed": round(float(dist[j]), 4),
                    "best_peer": round(best_peer, 3),
                    "mandate_words": sorted(set(w.lower() for w in
                                                MANDATE_WORDS.findall(texts[cur_i[j]]))),
                    "text": texts[cur_i[j]][:800],
                })
            rows.append({
                "district": d, "prev": prev, "year": yr,
                "sections": len(cur_i),
                "median": round(float(np.median(dist)), 4),
                "p90": round(float(np.percentile(dist, 90)), 4),
                "max": round(float(dist.max()), 4),
                "ocr": f"{d}__{yr}" in ocred or f"{d}__{prev}" in ocred,
                "gap_years": int(yr[:4]) - int(prev[:4]),
                "top": top,
            })

    (PLANS / "edits.json").write_text(json.dumps(rows, indent=1))

    print(f"\n{'=' * 90}")
    print("HOW MUCH EACH PLAN WAS REWRITTEN  (per-section distance to its closest prior version)")
    print(f"{'=' * 90}")
    print(f"{'district':<20}{'transition':<20}{'secs':>5}{'median':>9}{'p90':>8}{'max':>8}"
          f"{'  mandate words in top 8':<26}note")
    for r in rows:
        words = {w for t in r["top"] for w in t["mandate_words"]}
        note = []
        if r["gap_years"] > 1:
            note.append(f"{r['gap_years']}-yr gap")
        if r["ocr"]:
            note.append("OCR")
        print(f"{r['district']:<20}{r['prev'] + ' -> ' + r['year']:<20}{r['sections']:>5}"
              f"{r['median']:>9.4f}{r['p90']:>8.4f}{r['max']:>8.4f}  "
              f"{','.join(sorted(words))[:24]:<24}{', '.join(note)}")

    print(f"\n{'=' * 90}\nTHE 2024-25 TRANSITION — is the mandate the thing that moved?\n{'=' * 90}")
    for r in rows:
        if r["year"] != "2024-25":
            continue
        print(f"\n{r['district']}  ({r['prev']} -> 2024-25)  "
              f"median {r['median']:.4f}, p90 {r['p90']:.4f}")
        for t in r["top"][:3]:
            tag = ("MANDATE VOCAB: " + ",".join(t["mandate_words"])
                   if t["mandate_words"] else "no mandate vocab")
            shared = "peer has it" if t["best_peer"] > 0.80 else "no peer match"
            print(f"   [changed {t['changed']:.3f} | {shared} {t['best_peer']:.2f} | {tag}]")
            print(f"     {t['text'][:300]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
