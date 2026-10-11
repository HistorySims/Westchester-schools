"""Hot-spot detection on compliance documents, at the section level.

``analyze.py`` tested the obvious thing — one centroid per district-year, then
distance — and it failed the validation test. Chapter 155.7 was amended for
2024-25, and three districts rewrote their drill sections to match: the word
"trauma" goes from 2 occurrences to 10, "props"/"actors" from 0 to 10,
"simulat*" from 0 to 5, all in the same year. Centroid distance for that same
transition came out at 0.0008, the second *smallest* movement in the series.

The reason is arithmetic, not embedding quality. These plans are mandated in
content and order, so roughly 95% of any two is the same template text. A
change confined to three sections of an eighty-section document moves the
mean-pooled vector by about a fortieth of its own size, which is well inside
the distance between two districts' boilerplate. Mean-pooling is the wrong
operator: it measures what the documents have in common, and what they have in
common is the part Albany wrote.

So compare sections, and keep the comparison local to the change:

  novelty   a section with no near-neighbour in the same district's previous
            plan is new text, regardless of how much identical text surrounds
            it. Immune to boilerplate by construction.
  peer      a new section that a peer also added that year is the mandate
            arriving; a new section nobody else has is a local decision — the
            hot spot. This is the discrimination a single district's history
            cannot make.
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import sys
from collections import defaultdict

import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from analyze import PLANS, load, unit  # noqa: E402

from herald.embed import VoyageEmbedder  # noqa: E402

CACHE = PLANS / "vecs2.npz"

#: A section is "new" when nothing in the district's previous plan is this
#: close. Template sections re-extract near-identically year to year and score
#: under 0.05, so the gap between reformatting and rewriting is wide.
NOVEL = 0.25

#: A new section is the mandate rather than a local choice when some peer's
#: plan that same year says nearly the same thing.
PEER_MATCH = 0.80


async def embed(texts: list[str]) -> np.ndarray:
    if CACHE.exists():
        z = np.load(CACHE, allow_pickle=True)
        if list(z["texts"]) == texts:
            print(f"\n{len(texts)} sections from cache")
            return z["vecs"]
    print(f"\nembedding {len(texts)} sections")
    async with VoyageEmbedder(os.environ["VOYAGE_API_KEY"]) as v:
        vecs = np.array(await v.embed_documents(texts), dtype=np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-9
    np.savez(CACHE, vecs=vecs, texts=np.array(texts, dtype=object))
    return vecs


def main() -> int:
    texts, keys, ocred = load()
    vecs = asyncio.run(embed(texts))

    idx: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, k in enumerate(keys):
        idx[k].append(i)
    years = sorted({y for _, y in idx})
    districts = sorted({d for d, _ in idx})

    print(f"\n{'=' * 78}\nWHAT EACH DISTRICT REWROTE, AND WHETHER ANYONE ELSE DID\n{'=' * 78}")
    print(f"{'district':<20}{'year':<9}{'vs':<9}{'secs':>6}{'new':>6}"
          f"{'mandate':>9}{'local':>7}   note")

    report: list[dict] = []
    for d in districts:
        mine = sorted(y for y in years if (d, y) in idx)
        for prev, yr in zip(mine, mine[1:]):
            cur_i = np.array(idx[(d, yr)])
            prev_i = np.array(idx[(d, prev)])
            novelty = 1 - (vecs[cur_i] @ vecs[prev_i].T).max(axis=1)

            # Peers: every other district's sections in the same school year.
            peer_i = np.array([i for (dd, yy), ii in idx.items()
                               if yy == yr and dd != d for i in ii])
            new = [int(j) for j in np.where(novelty > NOVEL)[0]]
            mandate, local = [], []
            for j in new:
                best = float((vecs[cur_i[j]] @ vecs[peer_i].T).max()) if len(peer_i) else 0.0
                (mandate if best > PEER_MATCH else local).append((j, best))

            gap = int(yr[:4]) - int(prev[:4])
            note = []
            if gap > 1:
                note.append(f"{gap}-year gap")
            if f"{d}__{yr}" in ocred or f"{d}__{prev}" in ocred:
                note.append("OCR — novelty inflated")
            if not len(peer_i):
                note.append("no peers this year")
            print(f"{d:<20}{yr:<9}{prev:<9}{len(cur_i):>6}{len(new):>6}"
                  f"{len(mandate):>9}{len(local):>7}   {', '.join(note)}")
            report.append({
                "district": d, "year": yr, "prev": prev, "sections": len(cur_i),
                "new": len(new), "mandate": len(mandate), "local": len(local),
                "ocr": f"{d}__{yr}" in ocred or f"{d}__{prev}" in ocred,
                "gap_years": gap,
                "local_sections": [
                    {"novelty": round(float(novelty[j]), 3),
                     "best_peer": round(b, 3), "text": texts[cur_i[j]][:700]}
                    for j, b in sorted(local, key=lambda x: -novelty[x[0]])[:6]
                ],
                "mandate_sections": [
                    {"novelty": round(float(novelty[j]), 3),
                     "best_peer": round(b, 3), "text": texts[cur_i[j]][:400]}
                    for j, b in sorted(mandate, key=lambda x: -x[1])[:4]
                ],
            })

    (PLANS / "hotspots.json").write_text(json.dumps(report, indent=1))

    print(f"\n{'=' * 78}\nVALIDATION: does 2024-25 come back as a shared change?\n{'=' * 78}")
    for r in report:
        if r["year"] == "2024-25":
            print(f"\n{r['district']} ({r['prev']} -> 2024-25): {r['new']} new sections, "
                  f"{r['mandate']} matched by a peer")
            for s in r["mandate_sections"][:2]:
                print(f"   [novel {s['novelty']}, peer {s['best_peer']}] {s['text'][:230]}")

    print(f"\n{'=' * 78}\nHOT SPOTS: new, and no peer wrote anything like it\n{'=' * 78}")
    for r in sorted(report, key=lambda r: -r["local"])[:6]:
        if not r["local"]:
            continue
        flag = "  (OCR, treat with suspicion)" if r["ocr"] else ""
        print(f"\n{r['district']}  {r['prev']} -> {r['year']}  "
              f"{r['local']} local of {r['new']} new{flag}")
        for s in r["local_sections"][:3]:
            print(f"   [novel {s['novelty']}, best peer {s['best_peer']}] {s['text'][:260]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
