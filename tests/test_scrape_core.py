"""Tests for the source-agnostic scrape plumbing."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from herald.scrape.core import (
    Fetcher,
    Manifest,
    RawStore,
    make_manifest_entry,
    sha256_bytes,
    slugify,
)
from herald.scrape.models import DocType, ManifestEntry, ScrapedDoc


def _doc(**kw) -> ScrapedDoc:
    base = dict(
        district="scarsdale",
        doc_type=DocType.minutes,
        title="January 2024 Minutes",
        source_url="https://example.test/min.pdf",
    )
    base.update(kw)
    return ScrapedDoc(**base)


def test_slugify_makes_fs_safe_names():
    assert slugify("Policy 5030: Student Wellness!") == "Policy-5030-Student-Wellness"
    assert slugify("") == "untitled"
    assert slugify("a" * 200, maxlen=10) == "a" * 10


def test_sha256_is_stable():
    assert sha256_bytes(b"hello") == sha256_bytes(b"hello")
    assert sha256_bytes(b"hello") != sha256_bytes(b"world")


def test_rawstore_path_layout(tmp_path):
    store = RawStore(tmp_path)
    doc = _doc(suggested_filename="Minutes-January-2024.pdf")
    sha = "deadbeefcafef00d"
    path = store.path_for(doc, sha, default_ext=".pdf")
    assert path.parent == tmp_path / "scarsdale" / "minutes"
    assert path.name == "deadbeef_Minutes-January-2024.pdf"


def test_rawstore_write_creates_file(tmp_path):
    store = RawStore(tmp_path)
    data = b"%PDF-1.4 fake"
    path = store.write(_doc(), data, default_ext=".pdf")
    assert path.read_bytes() == data
    assert path.parent.is_dir()


def _entry(url: str, sha: str, path: str = "/x") -> ManifestEntry:
    return make_manifest_entry(
        _doc(source_url=url),
        local_path=path,  # type: ignore[arg-type]
        sha256=sha,
        size_bytes=3,
        content_type="application/pdf",
    )


def test_manifest_append_and_dedupe(tmp_path):
    mpath = tmp_path / "manifest.jsonl"
    m = Manifest(mpath)
    assert not m.has_url("https://a.test/1.pdf")

    m.append(_entry("https://a.test/1.pdf", "hash1"))
    assert m.has_url("https://a.test/1.pdf")
    assert m.has_hash("hash1")
    assert len(m.entries()) == 1


def test_manifest_reloads_seen_state_from_disk(tmp_path):
    mpath = tmp_path / "manifest.jsonl"
    Manifest(mpath).append(_entry("https://a.test/1.pdf", "hash1"))

    reopened = Manifest(mpath)  # fresh instance reads existing file
    assert reopened.has_url("https://a.test/1.pdf")
    assert reopened.has_hash("hash1")


def test_make_manifest_entry_captures_provenance():
    doc = _doc(meeting_id="MEET1", committee="Board of Education")
    entry = make_manifest_entry(
        doc, local_path="/tmp/x.pdf", sha256="h", size_bytes=10, content_type="application/pdf"  # type: ignore[arg-type]
    )
    assert entry.meeting_id == "MEET1"
    assert entry.committee == "Board of Education"
    assert entry.doc_type is DocType.minutes
    assert isinstance(entry.fetched_at, datetime)
    assert entry.fetched_at.tzinfo is UTC


# ---- Fetcher --------------------------------------------------------------


def _fast_fetcher(**kw) -> Fetcher:
    return Fetcher(min_request_interval=0.0, retry_base_delay=0.0, **kw)


def test_fetcher_sends_user_agent(httpx_mock):
    httpx_mock.add_response(url="https://x.test/a", text="ok")
    with _fast_fetcher(user_agent="herald-test/1.0") as f:
        f.get("https://x.test/a")
    req = httpx_mock.get_requests()[0]
    assert req.headers["User-Agent"] == "herald-test/1.0"


def test_fetcher_retries_then_succeeds(httpx_mock):
    httpx_mock.add_response(url="https://x.test/a", status_code=503)
    httpx_mock.add_response(url="https://x.test/a", status_code=200, text="ok")
    with _fast_fetcher() as f:
        resp = f.get("https://x.test/a")
    assert resp.text == "ok"
    assert len(httpx_mock.get_requests()) == 2


def test_fetcher_raises_on_client_error(httpx_mock):
    httpx_mock.add_response(url="https://x.test/missing", status_code=404)
    with _fast_fetcher() as f, pytest.raises(httpx.HTTPStatusError):
        f.get("https://x.test/missing")
