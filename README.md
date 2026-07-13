# Herald

Semantic research over historical American newspapers from the Library of Congress's [Chronicling America](https://chroniclingamerica.loc.gov/) collection.

Ask natural-language questions, get synthesized answers with citations linking back to high-resolution page images. See [`docs/PLAN.md`](docs/PLAN.md) for the full design.

## Status

Phase 1 in progress: data ingestion pipeline, Supabase schema, hybrid retrieval, and a CLI proving the retrieval-and-synthesis loop works against a set of Anti-Rent Wars validation questions.

## Development

Requires Python 3.11+ and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                                  # install deps + dev tools
uv run pytest                            # run tests
uv run ruff check                        # lint
uv run pyright                           # type-check
uv run herald --help                     # CLI entry
```

## Configuration

Copy the `.env` template and fill in real values:

```
SUPABASE_DB_URL=postgresql://postgres:<your-password>@db.<project>.supabase.co:5432/postgres
SUPABASE_URL=
SUPABASE_SERVICE_KEY=
ANTHROPIC_API_KEY=
VOYAGE_API_KEY=
```

`.env` is gitignored — never commit it.

## Database

Apply the initial schema with:

```bash
set -a; source .env; set +a
psql "$SUPABASE_DB_URL" -f db/migrations/0001_init.sql
```
