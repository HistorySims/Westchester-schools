#!/usr/bin/env python
"""Where districts' board policies share wording and disagree on a number.

Reads the committed policy snapshot. No database and no embeddings — the whole
point is that a number inside shared boilerplate is an exact-extraction
problem, not a geometric one (see `herald.policy_compare`).

    uv run scripts/policy_divergence.py
    uv run scripts/policy_divergence.py --policy 7110
    uv run scripts/policy_divergence.py --rule credit

``--rule`` runs the targeted probe instead of the sweep, for rules whose
wording the districts rewrote so far apart that no sentence alignment pairs
them. The attendance rule is exactly that case and is the reason the probe
exists: Greenburgh writes "any student with more than 18 absences in a course
may not receive credit", Mount Vernon a bulleted "20 absences for a one (1)
credit course".
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
import re
import sys
from collections import defaultdict
from collections.abc import Sequence

import typer

from herald.html_text import extract_html_text
from herald.policy_compare import (
    COMPETING_CONSEQUENCE,
    CREDIT_DENIAL,
    CREDIT_DISCRETION,
    CREDIT_UNITS,
    Divergence,
    clauses,
    divergences,
    policy_number,
    rule_parameter,
)

app = typer.Typer(add_completion=False)

#: Two snapshots, because the districts split across two publishers. Four serve
#: their manual through BoardDocs' policy console and four through the
#: BoardPolicyOnline portals, which are Blazor Server apps that need a real
#: browser (see `herald.scrape.policy_manual`). Rows from the first carry
#: `html`, from the second `text`; nothing else differs.
SNAPSHOTS = (
    pathlib.Path("data/snapshots/boarddocs-policies.jsonl.gz"),
    pathlib.Path("data/snapshots/policy-manuals.jsonl.gz"),
)

#: Named rules for the targeted probe: the consequence to look for, and the
#: units its parameter is allowed to carry. The units matter as much as the
#: phrase — without them White Plains' five-day appeal clock reads as a
#: credit threshold.
RULES: dict[str, dict] = {
    "credit": {
        "label": "Loss of course credit for absence",
        "consequence": CREDIT_DENIAL,
        "units": CREDIT_UNITS,
        "exclude": COMPETING_CONSEQUENCE,
        "discretion": CREDIT_DISCRETION,
        "title_hint": re.compile(r"attendance", re.I),
    },
}


def _sources() -> str:
    return ", ".join(f"`{s}`" for s in SNAPSHOTS if s.exists())


def load(snapshots: Sequence[pathlib.Path]) -> list[dict]:
    rows: list[dict] = []
    for path in snapshots:
        if not path.exists():
            typer.echo(f"note: no snapshot at {path}, skipping", err=True)
            continue
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            rows += [json.loads(line) for line in fh]
    return rows


def text_of(row: dict) -> str:
    """The policy's text, whichever form its snapshot stored.

    The portal snapshot holds extracted text — the markup is Kendo widget
    wrappers that `extract_html_text` would only throw away, and five times the
    bytes to commit.
    """
    if row.get("text"):
        return row["text"]
    return extract_html_text(row["html"]).text if row.get("html") else ""


def run_rule(rows: list[dict], name: str, out: pathlib.Path) -> int:
    spec = RULES[name]
    lines = [f"# {spec['label']}", "",
             f"Generated {dt.date.today()} from {_sources()}.", "",
             "| district | policy | parameter | standard |", "|---|---|---|---|"]
    detail: list[str] = []
    districts = sorted({r["district"] for r in rows})

    for district in districts:
        best = None
        discretionary: tuple[str, str] | None = None
        for r in rows:
            if r["district"] != district:
                continue
            body = text_of(r)
            if not body:
                continue
            found = rule_parameter(district, r["title"], body,
                                   consequence=spec["consequence"],
                                   units=spec["units"],
                                   exclude=spec.get("exclude"))
            if found and (best is None or (found.value or 0) > (best[0].value or 0)):
                best = (found, r["title"])
            if discretionary is None and spec["discretion"].search(
                    re.sub(r"\s+", " ", body)):
                discretionary = (r["title"], "administrator discretion")

        if best:
            param, title = best
            lines.append(f"| {district} | {title} | **{param}** | bright line |")
            detail += [f"### {district} — {title}", "", f"> …{param.quote[:500]}…", ""]
        elif discretionary:
            lines.append(f"| {district} | {discretionary[0]} | — | "
                         f"administrator discretion |")
            detail += [f"### {district} — {discretionary[0]}", "",
                       "No numeric threshold; the decision is an administrator's.", ""]
        else:
            titles = [r["title"] for r in rows
                      if r["district"] == district and spec["title_hint"].search(r["title"])]
            lines.append(f"| {district} | {titles[0] if titles else '—'} | — | "
                         f"no provision found |")

    lines += ["", "A threshold alone does not settle the rule: what gets counted "
              "differs too. Greenburgh tallies all absences excused and unexcused "
              "with listed exceptions, Tarrytowns only unexcused ones, so two "
              "districts at 18 are not applying the same rule.", "", "---", ""]
    out.write_text("\n".join(lines + detail))
    typer.echo(f"{len(districts)} districts -> {out}")
    return 0


@app.command()
def main(
    policy: str = typer.Option("", help="Restrict the sweep to one policy number."),
    rule: str = typer.Option("", help=f"Targeted probe instead: {sorted(RULES)}"),
    same_wording: float = typer.Option(0.86, help="Clause similarity floor."),
    out: pathlib.Path = typer.Option(
        pathlib.Path(f"scripts/policy_divergence_{dt.date.today():%Y%m%d}.md")),
) -> None:
    rows = load(SNAPSHOTS)
    if not rows:
        typer.echo(f"no policy snapshot found at any of {SNAPSHOTS}", err=True)
        raise typer.Exit(1)
    out.parent.mkdir(parents=True, exist_ok=True)

    if rule:
        if rule not in RULES:
            typer.echo(f"unknown rule {rule!r}; have {sorted(RULES)}", err=True)
            raise typer.Exit(1)
        raise typer.Exit(run_rule(rows, rule, out))

    held: dict[str, dict[str, tuple[str, list[str]]]] = defaultdict(dict)
    for r in rows:
        number = policy_number(r["title"])
        if not number or (policy and number != policy):
            continue
        body = text_of(r)
        if len(body) < 400:
            continue
        held[number][r["district"]] = (r["title"], clauses(body))

    shared = {n: v for n, v in held.items() if len(v) >= 2}
    typer.echo(f"{len(held)} policy numbers, {len(shared)} held by two or more districts")

    found: list[Divergence] = []
    for number, holders in sorted(shared.items()):
        names = sorted(holders)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                found += divergences(number, holders[a][0], a, holders[a][1],
                                     b, holders[b][1], same_wording=same_wording)

    value = [d for d in found if d.kind == "value"]
    unit = [d for d in found if d.kind == "unit"]
    lines = [
        "# Board policies: same wording, different number", "",
        f"Generated {dt.date.today()} from {_sources()} — "
        f"{len(shared)} policies held by two or more of "
        f"{len({r['district'] for r in rows})} districts.", "",
        "Districts license model policy from the same vendors, so a clause can "
        "run word-for-word identical and differ in the figure that decides "
        "something. Embedding distance cannot see this: two numbers inside "
        "identical text sit at cosine ~0.999.", "",
        f"**{len(value)} disagree on the value; {len(unit)} agree on the value and "
        "differ on the unit** — the second is the easier one to read past.", "",
    ]
    for heading, group in (("Different value", value), ("Different unit, same value", unit)):
        lines += [f"## {heading}", ""]
        if not group:
            lines += ["None found.", ""]
        for d in group:
            lines += [
                f"### Policy {d.policy} — {d.title}", "",
                f"*wording {d.wording:.3f} identical*", "",
                f"- **{d.district_a}**: {', '.join(str(q) for q in d.quantities_a)}",
                f"  > {d.clause_a[:420]}",
                f"- **{d.district_b}**: {', '.join(str(q) for q in d.quantities_b)}",
                f"  > {d.clause_b[:420]}", "",
            ]
    out.write_text("\n".join(lines))
    typer.echo(f"{len(value)} value divergences, {len(unit)} unit divergences -> {out}")


if __name__ == "__main__":
    sys.exit(app())
