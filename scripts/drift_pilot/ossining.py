"""Does Ossining attach no safety plan, or did the crawl fail silently?

plans.py scanned 444 Ossining meetings and found nothing at all — not even a
Policy 8130 reading, which every other district produced. Its `except
Exception: continue` makes a 403 indistinguishable from an empty agenda, so
this re-runs a small slice with the errors visible and prints every attachment
title rather than only the ones matching.
"""
from __future__ import annotations

import re

from herald.scrape.__main__ import BROWSER_USER_AGENT, _fetcher
from herald.scrape.boarddocs import BoardDocsClient
from herald.scrape.runner import load_targets

target = next(t for t in load_targets("data/targets/port_chester_peers.json")
              if t.slug == "oufsd")

with _fetcher(BROWSER_USER_AGENT, 2.0, respect_robots=False, browser=True) as f:
    client = BoardDocsClient(state=target.state, slug=target.slug, fetcher=f)
    committees = client.discover_committees()
    print(f"committees: {[(c.unique, c.name) for c in committees]}")

    pairs = [(m, c.unique) for c in committees
             for m in client.list_meetings(c.unique) if m.date]
    pairs.sort(key=lambda x: x[0].date, reverse=True)
    print(f"{len(pairs)} meetings, {pairs[-1][0].date} .. {pairs[0][0].date}\n")

    # One June-September meeting per year: when the annual plan is adopted.
    picked, seen_years = [], set()
    for m, cid in pairs:
        if m.date.month in (6, 7, 8, 9) and m.date.year not in seen_years:
            seen_years.add(m.date.year)
            picked.append((m, cid))
    picked = picked[:10]

    ok = err = 0
    for m, cid in picked:
        try:
            files = client.get_agenda_files(m, cid)
        except Exception as exc:
            err += 1
            print(f"{m.date}  !! {type(exc).__name__}: {str(exc)[:90]}")
            continue
        ok += 1
        titles = [fr.best_title or "" for fr in files]
        hits = [t for t in titles if re.search(r"safety|emergency|155\.7", t, re.I)]
        print(f"{m.date}  {len(files):>3} files   {m.name[:40]}")
        for t in hits:
            print(f"             ** {t[:80]}")
        for t in titles[:4]:
            print(f"                {t[:76]}")

    print(f"\n{ok} agendas read, {err} failed")
