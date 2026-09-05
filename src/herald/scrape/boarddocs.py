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
from urllib.parse import quote, unquote

from bs4 import BeautifulSoup

from herald.scrape.core import Fetcher
from herald.scrape.models import DocType, ScrapedDoc

logger = logging.getLogger(__name__)

# Endpoint path segments on Board.nsf. There is no public "list committees"
# endpoint (BD-GetCommittees 404s); the committee id is embedded in the
# /Public page HTML instead — see parse_committee_id.
EP_MEETINGS = "BD-GetMeetingsList"
# PRINT-AgendaDetailed returns the agenda HTML *with* the /$file/ attachment
# links; BD-GetAgenda returns only the bare category/item tree (no files).
EP_AGENDA = "PRINT-AgendaDetailed"
# Policy console (recovered from cdn.boarddocs.com/2021v02/js/policies.js).
# Some districts serve their whole adopted manual here; others answer with an
# empty book list because their manual lives on an external portal instead
# (see herald.scrape.policy_manual).
EP_POLICY_BOOKS = "BD-GetPolicyBooks"
EP_POLICIES = "BD-GetPolicies"
EP_POLICY_ITEM = "BD-GetPolicyItem"
# Attachments hang off a policy separately: the item HTML only carries an
# empty <div filetype="public" parentid="..."> that the UI fills by AJAX. Some
# policies have NO inline body at all and live entirely in the attachment —
# Mount Vernon's 0115 (DASA / bullying) is one — so skipping this endpoint
# loses whole policies. Takes ``id=<policy unique>``; ``parentid`` is ignored.
EP_POLICY_FILES = "BD-GetPublicFiles"
#: The ``status`` BD-GetPolicies expects. This one value matters: every other
#: spelling — ``""``, ``adopted``, ``published``, ``all`` — comes back with the
#: bare string ``No Access``, which reads like an authorization wall and is
#: not one. Anonymous callers get the full manual with ``active``.
POLICY_STATUS = "active"
POLICY_NO_ACCESS = "No Access"

_FILE_HREF = re.compile(r"/\$file/", re.IGNORECASE)
_DOC_EXT = re.compile(r"\.(pdf|docx?|rtf|txt|xlsx?|pptx?)(?:$|\?)", re.IGNORECASE)
_NUMBERDATE = re.compile(r"(\d{4})(\d{2})(\d{2})")
# BoardDocs' /Public page inlines the committee id in one of a few forms:
#   var current_committee_id = "A1B2C3D4E5";      (JS var)
#   "current_committee_id":"A1B2C3D4E5"           (JSON/config)
#   ...&current_committee_id=A1B2C3D4E5           (a URL/deep-link param)
_COMMITTEE_ID_RE = re.compile(
    r"""current_committee_id["']?\s*[:=]\s*["']?([A-Za-z0-9]{6,})["'&]?"""
)


class CommitteeNotFound(Exception):
    """Raised when a district's committee id can't be provided or discovered."""


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
    #: The agenda item this attachment hangs under, e.g. "Minutes
    #: Reorganization and Regular Meeting July 9, 2026". BoardDocs names the
    #: file itself "Min Org 7.9.26.pdf" — meaningless to a classifier and
    #: worse as a citation — while the item states plainly what it is. 898 of
    #: the corpus's 900 untyped documents are BoardDocs attachments, and this
    #: is the label that was being thrown away.
    item_title: str = ""

    @property
    def best_title(self) -> str:
        return self.item_title or self.title


@dataclass(frozen=True)
class PolicyRef:
    """One row of a policy book's index."""

    unique: str
    code: str
    title: str
    section: str = ""
    book: str = ""
    #: The index marks policies whose text is (or continues in) a file.
    has_attachment: bool = False

    @property
    def display_title(self) -> str:
        return f"{self.code} {self.title}".strip()


@dataclass(frozen=True)
class PolicyItem:
    """One policy, with the console's own metadata.

    BoardDocs states ``adopted`` and ``last_reviewed`` as fields, which the
    portal manuals only ever bury in prose — so a policy fetched here can be
    dated exactly.
    """

    unique: str
    code: str
    title: str
    book: str = ""
    section: str = ""
    status: str = ""
    adopted: str = ""
    last_reviewed: str = ""
    body_html: str = ""

    @property
    def display_title(self) -> str:
        return f"{self.code} {self.title}".strip()

    @property
    def has_body(self) -> bool:
        """Whether the inline body holds any text.

        BoardDocs returns an empty ``<div id="forcopy">`` for a policy whose
        text lives entirely in an attachment, and storing that shell as a
        document would put an empty "policy" in the corpus.
        """
        return bool(BeautifulSoup(self.body_html or "", "html.parser").get_text(strip=True))


class PolicyAccessDenied(Exception):
    """BD-GetPolicies answered ``No Access`` (usually a bad ``status``)."""


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


def parse_committees(html: str) -> list[Committee]:
    """Extract the committee id(s) + names from the /Public page.

    BoardDocs lists them in the board/committee menu, e.g.::

        <a class="committee-trigger" committeeid="A4EP6J588C05"
           aria-label="Board of Education">Board of Education</a>
        <select name="committeeid"><option value="A4EP6J588C05">…</option></select>

    (The ``bd.current_committee_id`` JS var is empty — "not used".) Returns one
    ``Committee`` per distinct id, names preferred from the anchor's label.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    found: dict[str, str] = {}
    for a in soup.select("a.committee-trigger"):
        cid = a.get("committeeid")
        if cid:
            label = a.get("aria-label") or a.get_text(strip=True) or str(cid)
            found.setdefault(str(cid), str(label))
    for sel in soup.find_all("select", attrs={"name": "committeeid"}):
        for opt in sel.find_all("option"):
            cid = opt.get("value")
            if cid:
                found.setdefault(str(cid), opt.get_text(strip=True) or str(cid))
    return [Committee(unique=cid, name=name) for cid, name in found.items()]


def parse_committee_id(html: str) -> str | None:
    """Fallback: a bare ``current_committee_id = "…"`` var, if present."""
    m = _COMMITTEE_ID_RE.search(html or "")
    return m.group(1) if m else None


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


def parse_policy_books(html: str) -> list[str]:
    """Book names from the BD-GetPolicyBooks dropdown.

    An empty list is a real answer, not a failure: districts whose manual is
    on an external portal return an empty body here.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[str] = []
    for a in soup.select("#policy-book-select a, .dropdown-menu a"):
        name = (a.get("aria-label") or a.get_text(" ", strip=True) or "").strip()
        if name and name not in out:
            out.append(name)
    return out


# Books that are not the adopted manual. "In Progress" holds drafts under
# amendment and "Renumbering" a migration scratchpad — interesting later for
# tracking how a policy changed, but they are not what a district is bound by,
# and mixing them into the corpus would answer questions with drafts.
_DRAFT_BOOK = re.compile(r"progress|renumber|draft|archive|template", re.I)
_ADOPTED_BOOK = re.compile(r"polic|manual|bylaw", re.I)
# A UI marker BoardDocs appends to the title of a policy with attached files.
_ATTACHMENT_NOTE = re.compile(r"\s*This Policy Contains an Attachment\.?\s*$", re.I)


def choose_policy_books(books: list[str]) -> list[str]:
    """The adopted manual(s) among a district's books.

    Falls back to everything non-draft, then to everything, so a district with
    an unusually named book is never silently skipped.
    """
    live = [b for b in books if not _DRAFT_BOOK.search(b)]
    adopted = [b for b in live if _ADOPTED_BOOK.search(b)]
    return adopted or live or books


def parse_policy_list(html: str, *, book: str = "") -> list[PolicyRef]:
    """The policy index for one book.

    The response is an accordion: ``<section>`` headings name the series
    ("0000 Mission, Vision, Goals") and each ``a.policy`` under them is one
    policy, with its code in a leading ``<b>``.
    """
    body = (html or "").strip()
    if body == POLICY_NO_ACCESS:
        raise PolicyAccessDenied(
            f"BD-GetPolicies returned {POLICY_NO_ACCESS!r} for book {book!r} "
            f"— check the status parameter (only {POLICY_STATUS!r} works)"
        )
    soup = BeautifulSoup(body, "html.parser")
    out: list[PolicyRef] = []
    section = ""
    # Walk in document order so each policy keeps the heading above it.
    for el in soup.find_all(["section", "a"]):
        if el.name == "section":
            section = el.get_text(" ", strip=True)
            continue
        if "policy" not in (el.get("class") or []):
            continue
        # NB: the attribute is emitted as `unique= "..."` — bs4 normalizes it,
        # but a regex over the raw HTML would not.
        uid = (el.get("unique") or "").strip()
        if not uid:
            continue
        code_el = el.find("b")
        code = code_el.get_text(" ", strip=True) if code_el else ""
        if code_el:
            code_el.extract()
        raw_title = el.get_text(" ", strip=True)
        title = _ATTACHMENT_NOTE.sub("", raw_title)
        out.append(PolicyRef(
            unique=uid, code=code, title=title, section=section, book=book,
            has_attachment=title != raw_title,
        ))
    return out


def parse_policy_files(html: str, *, base_url: str) -> list[FileRef]:
    """Attachment links for one policy (``a.public-file`` rows)."""
    soup = BeautifulSoup(html or "", "html.parser")
    out: list[FileRef] = []
    for a in soup.select("a.public-file, a[href]"):
        href = a.get("href") or ""
        if "/pfiles/" not in href and "/$file/" not in href.lower():
            continue
        url = href if href.startswith("http") else f"{_origin_of(base_url)}{href}"
        name = unquote(href.rsplit("/", 1)[-1])
        out.append(FileRef(url=url, title=a.get_text(" ", strip=True) or name))
    return out


def _origin_of(base_url: str) -> str:
    m = re.match(r"(https?://[^/]+)", base_url)
    return m.group(1) if m else base_url


def parse_policy_item(
    html: str, *, unique: str, fallback: PolicyRef | None = None
) -> PolicyItem:
    """One policy: the console's label/value metadata rows plus its body.

    The body lives in ``#forcopy``; the metadata is a stack of
    ``.row > .leftcol`` (label) / ``.rightcol`` (value) pairs — including
    ``Adopted`` and ``Last Reviewed``, which the portal manuals never state
    as fields.
    """
    soup = BeautifulSoup(html or "", "html.parser")
    meta: dict[str, str] = {}
    for row in soup.select("div.row"):
        label = row.select_one(".leftcol")
        value = row.select_one(".rightcol")
        if label and value:
            meta[label.get_text(" ", strip=True).lower()] = value.get_text(" ", strip=True)
    body = soup.select_one("#forcopy")
    return PolicyItem(
        unique=unique,
        code=meta.get("code") or (fallback.code if fallback else ""),
        title=meta.get("title") or (fallback.title if fallback else ""),
        book=meta.get("book") or (fallback.book if fallback else ""),
        section=meta.get("section") or (fallback.section if fallback else ""),
        status=meta.get("status", ""),
        adopted=meta.get("adopted", ""),
        last_reviewed=meta.get("last reviewed", ""),
        body_html=str(body) if body else "",
    )


def parse_agenda_files(agenda_body: object, *, base_url: str) -> list[FileRef]:
    """Extract downloadable attachments from an agenda response.

    BoardDocs' ``BD-GetAgenda`` returns JSON (agenda items with a ``files``
    array of ``{unique, name, description}``) on current instances, but older
    ones return HTML. Handle both: parse JSON if the body looks like JSON,
    else scan HTML anchors.
    """
    data: object | None = None
    if isinstance(agenda_body, list | dict):
        data = agenda_body
    elif isinstance(agenda_body, str) and agenda_body.lstrip()[:1] in ("[", "{"):
        try:
            data = json.loads(agenda_body)
        except ValueError:
            data = None
    if data is not None:
        return _files_from_json(data, base_url=base_url)
    return _files_from_html(str(agenda_body), base_url=base_url)


def _files_from_json(data: object, *, base_url: str) -> list[FileRef]:
    """Recursively collect file records (``unique`` + a filename-ish ``name``)."""
    out: list[FileRef] = []
    seen: set[str] = set()

    def walk(obj: object) -> None:
        if isinstance(obj, dict):
            uid = obj.get("unique")
            name = obj.get("name")
            if uid and name and _DOC_EXT.search(str(name)):
                url = f"{base_url}/files/{uid}/$file/{quote(str(name))}"
                if url not in seen:
                    seen.add(url)
                    out.append(FileRef(url=url, title=str(obj.get("description") or name)))
            for v in obj.values():
                walk(v)
        elif isinstance(obj, list):
            for v in obj:
                walk(v)

    walk(data)
    return out


#: BoardDocs prefixes every agenda item heading with "Subject 9.2 " — its own
#: numbering chrome, not part of the subject.
_SUBJECT_PREFIX = re.compile(r"^\s*subject\s+[\d.]+\s*", re.I)


def _item_title_for(anchor) -> str:
    """The agenda item an attachment sits under, if it can be found.

    Attachments live in ``div.print-files`` inside
    ``div.container.item.agendaorder``; the item's own heading is the first
    bold block in that container.
    """
    item = anchor.find_parent("div", class_="agendaorder")
    if item is None:
        return ""
    head = item.find(
        lambda t: t.name == "div" and "font-weight: bold" in (t.get("style") or "")
    )
    if head is None:
        return ""
    # Real headings wrap across source lines, so the inner newline and indent
    # survive get_text() and would land in the document title verbatim.
    text = re.sub(r"\s+", " ", head.get_text(" ", strip=True))
    return _SUBJECT_PREFIX.sub("", text).strip()


def _files_from_html(agenda_html: str, *, base_url: str) -> list[FileRef]:
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
        title = a.get_text(strip=True) or a.get("title") or filename_of(url)
        out.append(FileRef(url=url, title=title, item_title=_item_title_for(a)))
    return out


def _join(base_url: str, href: str) -> str:
    if href.startswith("/"):
        # base_url is the Board.nsf app URL; site root is its scheme+host.
        m = re.match(r"(https?://[^/]+)", base_url)
        root = m.group(1) if m else base_url
        return root + href
    return base_url.rstrip("/") + "/" + href


def filename_of(url: str) -> str:
    """The real filename from a BoardDocs file URL.

    More trustworthy than the link text, which often reads "Regulation
    5830.docx (22 KB)" — a name whose "extension" is ``.docx (22 kb)``.
    """
    tail = url.split("/$file/")[-1] if "/$file/" in url else url.rsplit("/", 1)[-1]
    return unquote(tail.split("?")[0]) or "attachment"


@dataclass(frozen=True)
class PublicPageInfo:
    """What we can glean from a BoardDocs public page (for reverse-engineering)."""

    status: int
    length: int
    script_srcs: list[str]
    committee_hints: list[str]


def analyze_public_html(html: str, *, status: int = 200) -> PublicPageInfo:
    """Pull script URLs + any 'committee'-adjacent tokens out of a public page.

    The public SPA embeds the committee id and loads a JS bundle that names
    the real AJAX endpoints; this surfaces both so we can read the actual API.
    """
    soup = BeautifulSoup(html, "html.parser")
    scripts = [s["src"] for s in soup.find_all("script", src=True)]
    hints: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r".{0,30}committee.{0,50}", html, re.IGNORECASE):
        frag = " ".join(m.group(0).split())
        if frag not in seen:
            seen.add(frag)
            hints.append(frag)
        if len(hints) >= 25:
            break
    return PublicPageInfo(
        status=status, length=len(html), script_srcs=scripts, committee_hints=hints
    )


#: A meeting that is really a container for a year's minutes.
#
# Peekskill files its whole archive this way: a pseudo-meeting named
# "2026 Minutes", dated 2026-12-31, holding eighteen attachments called
# "Subject B. Business Meeting July 28, 2026". Read alone, each of those says
# "business meeting" and nothing about minutes — so the file classifier types
# it `other` and the ingest classifier then upgrades it to `agenda`, because
# "business meeting" is an agenda keyword. Peekskill ended up with 16 agendas
# and zero minutes while its complete minutes archive sat right there.
#
# The only thing that says "minutes" is the PARENT meeting's name, which the
# file never sees. This is that context.
_MINUTES_MEETING = re.compile(r"\bminutes\b", re.I)


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


def _committee_params(committee_id: str) -> dict[str, str]:
    """The committee id under every field name BoardDocs variants have used.

    The /Public form input is ``current_committee``, the selector is
    ``committeeid``, and civic-scraper documents ``current_committee_id``.
    Sending all three lets the backend read whichever it expects; extras are
    ignored.
    """
    return {
        "current_committee_id": committee_id,
        "current_committee": committee_id,
        "committeeid": committee_id,
    }


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
        self._public_html: str | None = None
        self._committees: list[Committee] | None = None
        self.public_status: int | None = None
        self.public_error: str | None = None

    @property
    def public_html(self) -> str:
        return self._public_html or ""

    def _load_public(self) -> str:
        """GET the /Public page once (sets the session cookie), cache the HTML.

        Serves double duty: priming the session past BoardDocs' bot filter and
        supplying the HTML we scrape the committee id out of. Best-effort — a
        failure records status/error and returns "" rather than aborting, so
        the caller can diagnose a discovery miss.
        """
        if self._public_html is None:
            try:
                resp = self.fetcher.get(self.public_url)
                self.public_status = resp.status_code
                self._public_html = resp.text
            except Exception as exc:  # advisory; the POST may still work
                self.public_error = f"{type(exc).__name__}: {exc}"
                logger.warning("could not load %s: %s", self.public_url, exc)
                self._public_html = ""
        return self._public_html

    def discover_committees(self) -> list[Committee]:
        """The district's committees, scraped from the /Public page menu."""
        if self._committees is None:
            html = self._load_public()
            found = parse_committees(html)
            if not found:  # fall back to a bare current_committee_id var
                cid = parse_committee_id(html)
                found = [Committee(unique=cid, name=cid)] if cid else []
            self._committees = found
        return self._committees

    def _post(self, endpoint: str, data: dict[str, str]) -> str:
        if self.prime_session:
            self._load_public()
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

    # ---- policy console ------------------------------------------------

    def list_policy_books(self) -> list[str]:
        """The district's policy books, or ``[]`` if it serves none here."""
        return parse_policy_books(self._post(EP_POLICY_BOOKS, {}))

    def list_policies(self, book: str) -> list[PolicyRef]:
        """Every policy in one book's index."""
        return parse_policy_list(
            self._post(EP_POLICIES, {"status": POLICY_STATUS, "book": book}), book=book
        )

    def get_policy(self, ref: PolicyRef) -> PolicyItem:
        """One policy's metadata + body."""
        return parse_policy_item(
            self._post(EP_POLICY_ITEM, {"id": ref.unique}), unique=ref.unique, fallback=ref
        )

    def get_policy_files(self, ref: PolicyRef) -> list[FileRef]:
        """Files attached to a policy — sometimes the policy's entire text."""
        return parse_policy_files(
            self._post(EP_POLICY_FILES, {"id": ref.unique}), base_url=self.base_url
        )

    def policy_url(self, unique: str) -> str:
        """The district's own permalink for a policy — what an answer cites."""
        return f"{self.base_url}/goto?open&id={unique}"

    def list_meetings(self, committee: str) -> list[Meeting]:
        return parse_meetings(self._post(EP_MEETINGS, _committee_params(committee)))

    def get_agenda_files(self, meeting: Meeting, committee: str) -> list[FileRef]:
        body = self._post(EP_AGENDA, {"id": meeting.unique, **_committee_params(committee)})
        return parse_agenda_files(body, base_url=self.base_url)

    def agenda_url(self, meeting: Meeting, committee: str) -> str:
        """A plain GET URL for the rendered agenda.

        The crawl reaches the agenda by POST, which the runner cannot repeat —
        it downloads by GET. BoardDocs serves the same document either way
        (verified against pcru: 322,136 bytes, byte-identical text), so the
        agenda can be downloaded, hashed, deduped and resumed like any other
        artifact instead of needing a special path.
        """
        return (f"{self.base_url}/{EP_AGENDA}?open&id={meeting.unique}"
                f"&current_committee_id={committee}")


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
        # A minutes-collection meeting makes its attachments minutes, whatever
        # their own titles claim. Only `other` is upgraded: a file that names
        # itself a policy or an agenda is taken at its word, so a stray
        # "Personnel Agenda" inside the collection stays an agenda.
        minutes_meeting = bool(_MINUTES_MEETING.search(meeting.name or ""))

        # The agenda ITSELF, not only the files hanging off it. This was
        # fetched and thrown away: the body was parsed for attachment links
        # and the ~19,500 characters of itemised meeting content discarded.
        # That is why Mount Vernon and Greenburgh show zero agendas despite
        # hundreds of meetings — the crawler never saved one.
        #
        # It matters most where minutes are unreachable. Port Chester
        # publishes no minutes on BoardDocs at all, so the agenda is the only
        # record of what its board took up. Caveat for whatever reads these:
        # an agenda says what was PROPOSED. It carries no outcome — pcru's
        # agendas contain zero occurrences of motion/carried/ayes/vote — so
        # "the board approved X" cannot be sourced from one.
        #
        # Skipped for a minutes collection, whose "agenda" is just an index
        # of the attachments and would add a phantom meeting.
        #
        # Yielded BEFORE the attachment listing on purpose. Discovering the
        # agenda needs no network call at all — its URL is built from the
        # meeting id — while listing attachments needs a POST that BoardDocs
        # 403s once the runner's IP is rate-limited. Live on 2026-09-05, six
        # Port Chester meetings lost their agenda that way: the listing
        # failed, the loop moved on, and the one document that did not depend
        # on that call went with it.
        if not minutes_meeting:
            when = f" ({meeting.date.isoformat()})" if meeting.date else ""
            yield ScrapedDoc(
                district=district,
                doc_type=DocType.agenda,
                title=f"{meeting.name} — Agenda{when}",
                source_url=client.agenda_url(meeting, committee),
                date=meeting.date,
                meeting_id=meeting.unique,
                committee=committee_name or committee,
                # .html so ingest dispatches to extract_html rather than PyMuPDF
                suggested_filename=f"agenda-{meeting.unique}.html",
            )

        try:
            files = client.get_agenda_files(meeting, committee)
        except Exception as exc:  # one bad agenda shouldn't kill the crawl
            logger.warning("agenda fetch failed for %s (%s): %s", meeting.name, meeting.unique, exc)
            continue

        for ref in files:
            fname = filename_of(ref.url)
            # The agenda item leads: it says "Minutes Reorganization and
            # Regular Meeting July 9, 2026" where the file says
            # "Min Org 7.9.26.pdf". Better for the classifier and far better
            # as the label a citation shows. The filename still rides along
            # for classification, and still names the file on disk.
            doc_type = classify_filename(f"{ref.item_title} {ref.title} {fname}")
            if minutes_meeting and doc_type is DocType.other:
                doc_type = DocType.minutes
            yield ScrapedDoc(
                district=district,
                doc_type=doc_type,
                title=ref.best_title,
                source_url=ref.url,
                date=meeting.date,
                meeting_id=meeting.unique,
                committee=committee_name or committee,
                suggested_filename=fname,
            )
