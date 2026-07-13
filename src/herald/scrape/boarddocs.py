"""BoardDocs adapter.

BoardDocs (go.boarddocs.com) is a Diligent product that most Westchester
districts use to publish board agendas, minutes, and policy manuals. Public
sites live at::

    https://go.boarddocs.com/<state>/<slug>/Board.nsf

The public UI is a single-page app backed by a handful of AJAX endpoints on
that ``Board.nsf`` application. This adapter drives those endpoints directly.

    ┌──────────────────────────────────────────────────────────────────┐
    │ VERIFY ON FIRST LIVE RUN                                          │
    │ These endpoint paths + payload shapes are the documented public   │
    │ BoardDocs contract, but they cannot be exercised from the build   │
    │ environment (no outbound network). Run                            │
    │   python -m herald.scrape committees --state ny --slug <slug>     │
    │ first: if the shapes differ for your district, the parse helpers  │
    │ (`parse_committees`, `parse_meetings`, `parse_agenda_files`) are   │
    │ pure and isolated — adjust them, not the plumbing.                │
    └──────────────────────────────────────────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date

from bs4 import BeautifulSoup

from herald.scrape.core import Fetcher
from herald.scrape.models import DocType, ScrapedDoc

logger = logging.getLogger(__name__)

# Endpoint path segments on Board.nsf. Overridable for districts that differ.
EP_COMMITTEES = "BD-GetCommittees"
EP_MEETINGS = "BD-GetMeetingsList"
EP_AGENDA = "BD-GetAgenda"

_FILE_HREF = re.compile(r"/\$file/", re.IGNORECASE)
_DOC_EXT = re.compile(r"\.(pdf|docx?|rtf|txt)(?:$|\?)", re.IGNORECASE)
_NUMBERDATE = re.compile(r"(\d{4})(\d{2})(\d{2})")


@dataclass(frozen=True)
class Committee:
    unique: str
    name: str


@dataclass(frozen=True)
class Meeting:
    unique: str
    name: str
    date: date | None


@dataclass(frozen=True)
class FileRef:
    url: str
    title: str


# ---- pure parsers (unit-tested against fixtures) ----------------------


def _coerce_list(payload: object) -> list[dict]:
    """BoardDocs sometimes wraps the array, sometimes returns it bare."""
    if isinstance(payload, str):
        payload = json.loads(payload)
    if isinstance(payload, dict):
        for key in ("data", "items", "results"):
            if isinstance(payload.get(key), list):
                return payload[key]
        return []
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    return []


def _parse_numberdate(value: object) -> date | None:
    if not value:
        return None
    m = _NUMBERDATE.search(str(value))
    if not m:
        return None
    y, mo, d = (int(g) for g in m.groups())
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def parse_committees(payload: object) -> list[Committee]:
    out: list[Committee] = []
    for row in _coerce_list(payload):
        uid = row.get("unique") or row.get("id")
        name = row.get("name") or row.get("title") or ""
        if uid:
            out.append(Committee(unique=str(uid), name=str(name)))
    return out


def parse_meetings(payload: object) -> list[Meeting]:
    out: list[Meeting] = []
    for row in _coerce_list(payload):
        uid = row.get("unique") or row.get("id")
        if not uid:
            continue
        name = row.get("name") or row.get("title") or ""
        mdate = _parse_numberdate(row.get("numberdate") or row.get("date"))
        out.append(Meeting(unique=str(uid), name=str(name), date=mdate))
    return out


def parse_agenda_files(agenda_html: str, *, base_url: str) -> list[FileRef]:
    """Extract downloadable attachments from an agenda HTML blob.

    Picks anchors that either point at a BoardDocs ``/$file/`` resource or end
    in a document extension. Relative hrefs are resolved against ``base_url``.
    """
    soup = BeautifulSoup(agenda_html, "html.parser")
    seen: set[str] = set()
    out: list[FileRef] = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not (_FILE_HREF.search(href) or _DOC_EXT.search(href)):
            continue
        url = href if href.startswith("http") else _join(base_url, href)
        if url in seen:
            continue
        seen.add(url)
        title = a.get_text(strip=True) or a.get("title") or _filename_of(url)
        out.append(FileRef(url=url, title=title))
    return out


def _join(base_url: str, href: str) -> str:
    if href.startswith("/"):
        # base_url is the Board.nsf app URL; site root is its scheme+host.
        m = re.match(r"(https?://[^/]+)", base_url)
        root = m.group(1) if m else base_url
        return root + href
    return base_url.rstrip("/") + "/" + href


def _filename_of(url: str) -> str:
    tail = url.split("/$file/")[-1] if "/$file/" in url else url.rsplit("/", 1)[-1]
    return tail.split("?")[0] or "attachment"


def select_committees(
    committees: list[Committee],
    *,
    match: str | None = None,
    explicit_ids: list[str] | None = None,
) -> list[Committee]:
    """Pick which committees to crawl.

    Explicit ids win if given; otherwise ``match`` is a case-insensitive
    regex tested against the committee name (e.g. ``"board|polic"`` to grab
    the board-meeting library and the policy manual). ``None`` match returns
    all committees.
    """
    if explicit_ids:
        want = set(explicit_ids)
        return [c for c in committees if c.unique in want]
    if match:
        rx = re.compile(match, re.IGNORECASE)
        return [c for c in committees if rx.search(c.name)]
    return list(committees)


def classify_filename(name: str) -> DocType:
    low = name.lower()
    if "minute" in low:
        return DocType.minutes
    if "policy" in low or "policies" in low or "regulation" in low:
        return DocType.policy
    if "handbook" in low:
        return DocType.handbook
    if "agenda" in low:
        return DocType.agenda
    return DocType.other


# ---- network client ---------------------------------------------------


class BoardDocsClient:
    def __init__(
        self,
        *,
        state: str,
        slug: str,
        fetcher: Fetcher,
        base_url: str | None = None,
        prime_session: bool = True,
    ) -> None:
        self.state = state
        self.slug = slug
        self.fetcher = fetcher
        self.base_url = (base_url or f"https://go.boarddocs.com/{state}/{slug}/Board.nsf").rstrip(
            "/"
        )
        m = re.match(r"(https?://[^/]+)", self.base_url)
        self.origin = m.group(1) if m else self.base_url
        self.public_url = f"{self.base_url}/Public"
        self.prime_session = prime_session
        self._primed = False

    def _prime(self) -> None:
        """Load the public board page once so the AJAX calls carry a session.

        BoardDocs' bot filter 403s a cold XHR; a browser gets there by first
        rendering the Public page (which sets cookies). Best-effort: a failed
        prime shouldn't abort the crawl — the POST may still succeed.
        """
        if not self.prime_session or self._primed:
            return
        self._primed = True
        try:
            self.fetcher.get(self.public_url)
        except Exception as exc:  # priming is advisory; a failure shouldn't abort
            logger.debug("session prime for %s failed: %s", self.public_url, exc)

    def _post(self, endpoint: str, data: dict[str, str]) -> str:
        self._prime()
        url = f"{self.base_url}/{endpoint}?open"
        resp = self.fetcher.post(
            url,
            data=data,
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": self.public_url,
                "Origin": self.origin,
            },
        )
        return resp.text

    def list_committees(self) -> list[Committee]:
        return parse_committees(self._post(EP_COMMITTEES, {}))

    def list_meetings(self, committee: str) -> list[Meeting]:
        return parse_meetings(self._post(EP_MEETINGS, {"current_committee": committee}))

    def get_agenda_files(self, meeting: Meeting, committee: str) -> list[FileRef]:
        html = self._post(EP_AGENDA, {"id": meeting.unique, "current_committee": committee})
        return parse_agenda_files(html, base_url=self.base_url)


# ---- adapter: discover ScrapedDocs ------------------------------------


def iter_documents(
    client: BoardDocsClient,
    *,
    district: str,
    committee: str,
    committee_name: str | None = None,
    since: date | None = None,
    limit: int | None = None,
) -> Iterator[ScrapedDoc]:
    """Yield a ``ScrapedDoc`` for every attachment in a committee's meetings.

    Newest meetings first (BoardDocs returns them that way); ``since`` drops
    older meetings, ``limit`` caps how many meetings are walked.
    """
    meetings = client.list_meetings(committee)
    if since is not None:
        meetings = [m for m in meetings if m.date is None or m.date >= since]
    if limit is not None:
        meetings = meetings[:limit]

    for meeting in meetings:
        try:
            files = client.get_agenda_files(meeting, committee)
        except Exception as exc:  # one bad agenda shouldn't kill the crawl
            logger.warning("agenda fetch failed for %s (%s): %s", meeting.name, meeting.unique, exc)
            continue
        for ref in files:
            fname = _filename_of(ref.url)
            yield ScrapedDoc(
                district=district,
                doc_type=classify_filename(f"{ref.title} {fname}"),
                title=ref.title,
                source_url=ref.url,
                date=meeting.date,
                meeting_id=meeting.unique,
                committee=committee_name or committee,
                suggested_filename=fname,
            )
