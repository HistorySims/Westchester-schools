"""Apply the SQL migrations in ``db/migrations`` — in order, once each.

Migrations were applied by hand with ``psql``, which does not exist on a
phone. That is why ``0005_bargaining_unit.sql`` sat unapplied for weeks while
the code that needed it was already merged: not a decision, just a step nobody
could take from the only device available. A migration that cannot be run is
not really shipped.

What this does is deliberately small — no rollbacks, no branching, no DSL:

* filename order is apply order (``0001`` … ``0006``);
* each file runs inside one transaction, and is recorded in
  ``schema_migrations`` in that same transaction, so a failure leaves neither
  a half-applied schema nor a false record of success;
* already-applied files are skipped, so re-running is safe;
* a file whose contents changed after being applied is reported and NOT
  re-run, because silently re-executing edited DDL is how a schema drifts
  away from what the migration files say it is.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

app = typer.Typer(help="Apply database migrations.", no_args_is_help=True)
console = Console()

DEFAULT_DIR = "db/migrations"

_CREATE_TABLE = """
create table if not exists schema_migrations (
  filename    text primary key,
  sha256      text not null,
  applied_at  timestamptz not null default now()
)
"""


@dataclass(frozen=True)
class Migration:
    path: Path
    sha256: str

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def sql(self) -> str:
        return self.path.read_text(encoding="utf-8")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def discover(directory: str | Path = DEFAULT_DIR) -> list[Migration]:
    """Every ``*.sql`` in the directory, in filename order."""
    d = Path(directory)
    return [
        Migration(path=p, sha256=_digest(p.read_text(encoding="utf-8")))
        for p in sorted(d.glob("*.sql"))
    ]


def plan(
    found: list[Migration], applied: dict[str, str]
) -> tuple[list[Migration], list[Migration]]:
    """Split into (pending, changed).

    ``changed`` is the dangerous case: a file recorded as applied whose text no
    longer matches what was applied. Those are surfaced, never re-run — the
    database and the repository disagree and a human has to say which is right.
    """
    pending = [m for m in found if m.name not in applied]
    changed = [m for m in found if m.name in applied and applied[m.name] != m.sha256]
    return pending, changed


def _applied(cur) -> dict[str, str]:
    cur.execute(_CREATE_TABLE)
    cur.execute("select filename, sha256 from schema_migrations")
    return {row[0]: row[1] for row in cur.fetchall()}


def _db_url() -> str:
    url = os.environ.get("SUPABASE_DB_URL", "")
    if not url:
        raise typer.BadParameter("SUPABASE_DB_URL is not set.")
    return url


def _report(pending: list[Migration], changed: list[Migration]) -> None:
    if changed:
        console.print(
            "[bold red]WARNING[/bold red] — applied migrations whose text has "
            "since changed. These will NOT be re-run:"
        )
        for m in changed:
            console.print(f"  {m.name}")
        console.print(
            "  The database and the repository disagree about what was applied. "
            "Reconcile by hand.\n"
        )
    if not pending:
        console.print("Nothing pending — the schema is up to date.")
        return
    table = Table(title=f"{len(pending)} pending migration(s)")
    table.add_column("file")
    table.add_column("bytes", justify="right")
    for m in pending:
        table.add_row(m.name, str(len(m.sql)))
    console.print(table)


@app.command()
def status(directory: str = typer.Option(DEFAULT_DIR, help="Migrations directory.")) -> None:
    """List migrations that have not been applied yet."""
    from herald import schools_db

    conn = schools_db.connect(_db_url())
    try:
        with conn.transaction():
            applied = _applied(conn.cursor())
    finally:
        conn.close()
    _report(*plan(discover(directory), applied))


@app.command()
def apply(
    directory: str = typer.Option(DEFAULT_DIR, help="Migrations directory."),
    dry_run: bool = typer.Option(True, help="Show what would run; change nothing."),
) -> None:
    """Apply every pending migration, oldest first."""
    from herald import schools_db

    found = discover(directory)
    if not found:
        raise typer.BadParameter(f"No .sql files in {directory}.")

    conn = schools_db.connect(_db_url())
    try:
        with conn.transaction():
            applied = _applied(conn.cursor())
        pending, changed = plan(found, applied)
        _report(pending, changed)
        if dry_run:
            if pending:
                console.print("\n[bold]DRY RUN[/bold] — nothing was applied.")
            return

        for m in pending:
            console.print(f"applying {m.name} …")
            # One transaction per migration: the DDL and the record that it
            # ran commit together or not at all.
            with conn.transaction():
                cur = conn.cursor()
                cur.execute(m.sql)
                cur.execute(
                    "insert into schema_migrations (filename, sha256) values (%s, %s)",
                    (m.name, m.sha256),
                )
            console.print(f"  [green]applied[/green] {m.name}")
        if pending:
            console.print(f"\n{len(pending)} migration(s) applied.")
    finally:
        conn.close()


if __name__ == "__main__":
    app()
