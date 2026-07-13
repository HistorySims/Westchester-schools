"""Ingest orchestrator tests.

Boundaries (LOC, Voyage, Postgres) are stubbed; we verify that the
orchestrator skips already-processed pages, batches embedding calls
across pages, and writes the right number of chunks per page.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date
from uuid import UUID

import pytest

from herald.ingest import ingest_paper
from herald.loc import IssueRef, PageRef

# ---- fakes ----

class FakeLOC:
    def __init__(
        self,
        issues: list[IssueRef],
        pages: dict[str, list[PageRef]],   # keyed by issue.url
        ocr: dict[str, str],                # keyed by page.ocr_url
    ) -> None:
        self._issues = issues
        self._pages = pages
        self._ocr = ocr

    async def iter_issues_with_pages(
        self, lccn: str, *, date_from=None, date_to=None
    ) -> AsyncIterator[tuple[IssueRef, list[PageRef]]]:
        for i in self._issues:
            if date_from and i.date_issued < date_from:
                continue
            if date_to and i.date_issued > date_to:
                continue
            yield i, list(self._pages.get(i.url, []))

    async def fetch_ocr(self, page: PageRef) -> str:
        return self._ocr.get(page.ocr_url, "")


class FakeVoyage:
    """Records every embed_documents call and returns deterministic vectors."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(t))] * 1024 for t in texts]


class FakeCursor:
    """Cursor that drives the ingest by responding to specific queries."""

    def __init__(self, owner: FakeConn) -> None:
        self.owner = owner
        self._last: tuple[str, tuple | None] = ("", None)
        self._queued: object | None = None
        self._executemany_calls: list[tuple[str, list[tuple]]] = []

    def __enter__(self) -> FakeCursor:
        return self

    def __exit__(self, *exc) -> None:
        pass

    def execute(self, sql: str, params: tuple | None = None) -> None:
        s = " ".join(sql.split())
        self._last = (s, params)
        self.owner.executed.append((s, params))
        self._queued = self.owner.respond(s, params)

    def executemany(self, sql: str, params_seq) -> None:
        s = " ".join(sql.split())
        params_list = list(params_seq)
        self._executemany_calls.append((s, params_list))
        self.owner.chunks_written += len(params_list)

    def fetchone(self):
        return self._queued


class FakeTxn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """Lightweight stand-in for a psycopg.Connection.

    Tests configure ``existing_page_text`` to control whether
    ``_page_already_processed`` finds an existing processed page row.
    """

    def __init__(
        self,
        *,
        existing_page_text: dict[tuple[UUID, int], bool] | None = None,
    ) -> None:
        # (issue_id, sequence) -> True iff a row with non-null ocr_text exists
        self.existing_page_text = existing_page_text or {}
        self.executed: list[tuple[str, tuple | None]] = []
        self.chunks_written = 0
        # monotonic id mints
        self._next_id = 100
        # remembered insert outcomes
        self._page_ids: dict[tuple[UUID, int], UUID] = {}

    def cursor(self) -> FakeCursor:
        return FakeCursor(self)

    def transaction(self) -> FakeTxn:
        return FakeTxn()

    def close(self) -> None:
        pass

    # --- query router ---

    def respond(self, sql: str, params):
        if "insert into papers" in sql:
            return (self._mint_uuid(),)
        if "insert into issues" in sql:
            return (self._mint_uuid(),)
        if "select exists" in sql and "from chunks c" in sql:
            issue_id, seq = params
            return (self.existing_page_text.get((issue_id, seq), False),)
        if "insert into pages" in sql:
            issue_id = params[0]
            seq = params[1]
            key = (issue_id, seq)
            page_id = self._page_ids.setdefault(key, self._mint_uuid())
            return (page_id,)
        if "select id from pages where issue_id" in sql:
            issue_id, seq = params
            return (self._page_ids.get((issue_id, seq)),)
        if "update pages set ocr_text" in sql:
            return None
        return None

    def _mint_uuid(self) -> UUID:
        self._next_id += 1
        return UUID(int=self._next_id)


# ---- fixtures ----

def _issue(lccn: str, d: str, edition: int = 1) -> IssueRef:
    return IssueRef(
        lccn=lccn, date_issued=date.fromisoformat(d), edition=edition,
        url=f"https://x/{lccn}/{d}/ed-{edition}/",
    )


def _page(issue: IssueRef, seq: int) -> PageRef:
    base = f"https://x/{issue.lccn}/{issue.date_issued}/ed-{issue.edition}/seq-{seq}"
    return PageRef(
        lccn=issue.lccn, date_issued=issue.date_issued, edition=issue.edition,
        sequence=seq,
        image_url=f"{base}.jpg", jp2_url=f"{base}.jp2",
        pdf_url=f"{base}.pdf",
        resource_url=base, ocr_url=f"{base}/ocr.txt",
    )


# ---- tests ----

@pytest.mark.asyncio
async def test_happy_path_writes_pages_and_chunks():
    issue = _issue("sn83030213", "1845-08-09")
    pages = [_page(issue, 1), _page(issue, 2)]
    long_text = "anti rent " * 250  # ~500 words -> 2 chunks at 400/50
    loc = FakeLOC(
        issues=[issue],
        pages={issue.url: pages},
        ocr={pages[0].ocr_url: long_text, pages[1].ocr_url: long_text},
    )
    voyage = FakeVoyage()
    conn = FakeConn()

    stats = await ingest_paper(
        loc=loc, voyage=voyage, conn=conn,  # type: ignore[arg-type]
        lccn="sn83030213", title="New-York Daily Tribune",
    )
    assert stats.issues_seen == 1
    assert stats.pages_seen == 2
    assert stats.pages_written == 2
    assert stats.pages_skipped == 0
    assert stats.chunks_written > 0
    # one batched embedding call across both pages
    assert len(voyage.calls) == 1
    assert len(voyage.calls[0]) == stats.chunks_written


@pytest.mark.asyncio
async def test_resume_skips_already_processed_pages():
    issue = _issue("sn83030213", "1845-08-09")
    pages = [_page(issue, 1), _page(issue, 2), _page(issue, 3)]
    loc = FakeLOC(
        issues=[issue], pages={issue.url: pages},
        ocr={p.ocr_url: "rent " * 200 for p in pages},
    )
    voyage = FakeVoyage()
    conn = FakeConn()
    # Pre-populate page 1 as processed (the existence check uses issue_id from
    # the insert-into-issues query; we don't know it ahead of time, so we
    # configure the FakeConn after issuing one ingest to capture issue_id —
    # cleaner: monkeypatch respond() to mark seq=1 processed)
    orig_respond = conn.respond

    def patched(sql, params):
        if "select exists" in sql and "from chunks c" in sql:
            _issue_id, seq = params
            return (seq == 1,)
        return orig_respond(sql, params)
    conn.respond = patched  # type: ignore[method-assign]

    stats = await ingest_paper(
        loc=loc, voyage=voyage, conn=conn,  # type: ignore[arg-type]
        lccn="sn83030213", title="x",
    )
    assert stats.pages_seen == 3
    assert stats.pages_skipped == 1
    assert stats.pages_written == 2


@pytest.mark.asyncio
async def test_empty_ocr_page_writes_row_but_no_chunks():
    issue = _issue("sn83030213", "1845-08-09")
    pages = [_page(issue, 1)]
    loc = FakeLOC(
        issues=[issue], pages={issue.url: pages},
        ocr={pages[0].ocr_url: ""},
    )
    voyage = FakeVoyage()
    conn = FakeConn()
    stats = await ingest_paper(
        loc=loc, voyage=voyage, conn=conn,  # type: ignore[arg-type]
        lccn="sn83030213", title="x",
    )
    assert stats.pages_written == 1
    assert stats.chunks_written == 0
    # Voyage was called with an empty list of inputs (filtered to nothing)
    # OR not called at all — both are fine
    assert all(c == [] or all(s.strip() for s in c) for c in voyage.calls)


@pytest.mark.asyncio
async def test_date_filter_excludes_out_of_range_issues():
    in_range = _issue("sn83030213", "1845-08-09")
    out_of_range = _issue("sn83030213", "1847-01-01")
    loc = FakeLOC(
        issues=[in_range, out_of_range],
        pages={in_range.url: [_page(in_range, 1)], out_of_range.url: [_page(out_of_range, 1)]},
        ocr={_page(in_range, 1).ocr_url: "rent " * 200},
    )
    voyage = FakeVoyage()
    conn = FakeConn()
    stats = await ingest_paper(
        loc=loc, voyage=voyage, conn=conn,  # type: ignore[arg-type]
        lccn="sn83030213", title="x",
        date_from=date(1842, 1, 1), date_to=date(1846, 12, 31),
    )
    assert stats.issues_seen == 1
    assert stats.pages_seen == 1


@pytest.mark.asyncio
async def test_on_page_callback_invoked_with_status_per_page():
    issue = _issue("sn83030213", "1845-08-09")
    pages = [_page(issue, 1), _page(issue, 2)]
    loc = FakeLOC(
        issues=[issue], pages={issue.url: pages},
        ocr={pages[0].ocr_url: "rent " * 200, pages[1].ocr_url: ""},
    )
    voyage = FakeVoyage()
    conn = FakeConn()
    seen: list[tuple[int, str]] = []

    def on_page(p: PageRef, status: str) -> None:
        seen.append((p.sequence, status))

    await ingest_paper(
        loc=loc, voyage=voyage, conn=conn,  # type: ignore[arg-type]
        lccn="sn83030213", title="x",
        on_page=on_page,
    )
    statuses = dict(seen)
    assert statuses[1] == "written"
    assert statuses[2] == "empty"
