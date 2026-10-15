"""Commit the policy manuals fetched from the BoardPolicyOnline portals.

``herald-scrape policy-fetch`` drives a real browser against a Blazor Server
app, which a GitHub runner cannot do cheaply and which needs the proxy CA in
Chromium's NSS store. So the fetch happens where the browser works and the
result is committed, the same arrangement as `data/snapshots/` already uses for
BoardDocs agendas.

**Text, not HTML.** The 1,500 fetched policies are 19 MB of portal markup
carrying Kendo widget wrappers and inline styling, and nothing downstream wants
any of it: `policy_divergence` immediately runs `extract_html_text` and throws
the markup away. Storing the extracted text instead is a fifth of the size and
removes the extraction from every later run. The portal's section permalink is
kept on each row, so a finding still cites the live manual.

    uv run scripts/drift_pilot/policy_snapshot.py --raw <policy-fetch out dir>
"""

from __future__ import annotations

import datetime as dt
import gzip
import json
import pathlib
import sys

import typer

from herald.html_text import extract_html_text

app = typer.Typer(add_completion=False)

OUT = pathlib.Path("data/snapshots/policy-manuals.jsonl.gz")

#: Below this a section is a divider or a stub, not a policy.
MIN_CHARS = 200


@app.command()
def main(
    raw: pathlib.Path = typer.Option(..., help="The policy-fetch --out directory."),
    out: pathlib.Path = typer.Option(OUT, help="Snapshot to write."),
) -> None:
    manifest_path = raw / "manifest.jsonl"
    if not manifest_path.exists():
        typer.echo(f"no manifest at {manifest_path}", err=True)
        raise typer.Exit(1)

    rows = []
    with manifest_path.open(encoding="utf-8") as fh:
        for line in fh:
            entry = json.loads(line)
            if entry.get("doc_type") != "policy":
                continue
            rows.append(entry)
    typer.echo(f"{len(rows)} policy entries in the manifest")

    out.parent.mkdir(parents=True, exist_ok=True)
    written = thin = missing = 0
    per_district: dict[str, int] = {}
    with gzip.open(out, "wt", encoding="utf-8") as fh:
        for entry in rows:
            path = pathlib.Path(entry["local_path"])
            if not path.is_absolute():
                path = raw / path
            if not path.exists():
                missing += 1
                continue
            text = extract_html_text(path.read_text(encoding="utf-8",
                                                    errors="replace")).text
            if len(text) < MIN_CHARS:
                thin += 1
                continue
            district = entry["district"]
            per_district[district] = per_district.get(district, 0) + 1
            fh.write(json.dumps({
                "district": district,
                "title": entry.get("title") or "",
                "source_url": entry.get("source_url") or "",
                "fetched_at": entry.get("fetched_at") or dt.datetime.now(
                    dt.timezone.utc).isoformat(),
                "vendor": "boardpolicyonline",
                "chars": len(text),
                "text": text,
            }, ensure_ascii=False) + "\n")
            written += 1

    size = out.stat().st_size / 1024
    typer.echo(f"\n{written} policies -> {out} ({size:.0f} KB gzipped)")
    if thin:
        typer.echo(f"{thin} skipped as too thin (< {MIN_CHARS} chars)")
    if missing:
        typer.echo(f"{missing} manifest entries had no file on disk", err=True)
    for district in sorted(per_district):
        typer.echo(f"   {district}: {per_district[district]}")


if __name__ == "__main__":
    sys.exit(app())
