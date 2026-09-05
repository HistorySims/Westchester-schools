"""Panopto: meeting video, and the captions that make it searchable.

Port Chester publishes **no minutes** on BoardDocs — no attachment, no minutes
view, and zero occurrences of the word in 133,751 bytes of agenda HTML. Its
agendas say what was *proposed* and carry no outcome (zero occurrences of
motion / carried / ayes / vote). It does host meeting video at
``portchester.hosted.panopto.com``, and a caption track is the only artifact
we have found that records what was actually said and decided.

This module is deliberately a **probe plus pure parsers**, not a finished
adapter. What Panopto serves without a login depends on per-folder
permissions, and that cannot be settled by reasoning — the egress proxy here
denies the host, so the probe runs on an Actions runner (the same reason
``herald-scrape probe`` exists for BoardDocs). The adapter gets written once
the probe says which endpoints actually answer.

A transcript is **not** minutes, and whatever consumes these must not pretend
otherwise: speech recognition mangles proper nouns, resolution numbers and
dollar amounts, and a three-hour meeting is ~30,000 words of mostly
procedural talk. It is evidence of what was said, not the official record.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

#: The web UI's own session-listing endpoint (POST, JSON).
EP_SESSIONS = "/Panopto/Services/Data.svc/GetSessions"
#: Caption track for one session. language=1 is English; for a public folder
#: this answers without a login, and for a restricted one it returns an empty
#: body with a 200 — so an empty response means "not public", not "no captions".
EP_CAPTIONS = "/Panopto/Pages/Transcription/GenerateSRT.ashx"
#: Public-folder RSS, which needs no session cookie where it is enabled.
EP_PODCAST = "/Panopto/Podcast/Podcast.ashx"

#: Pages worth fetching to see whether anything is public at all.
PROBE_PAGES = (
    "/Panopto/Pages/Home.aspx",
    "/Panopto/Pages/Sessions/List.aspx",
)


@dataclass(frozen=True)
class Session:
    """One Panopto recording."""

    id: str
    name: str
    date: str | None = None
    folder: str | None = None
    url: str | None = None


@dataclass
class ProbeResult:
    """What a host actually answered, for the report."""

    host: str
    steps: list[tuple[str, str]] = field(default_factory=list)

    def record(self, what: str, outcome: str) -> None:
        self.steps.append((what, outcome))

    def as_markdown(self) -> str:
        lines = [f"## Panopto probe — `{self.host}`", "", "| step | outcome |", "|---|---|"]
        lines += [f"| {w} | {o} |" for w, o in self.steps]
        return "\n".join(lines) + "\n"


def sessions_query(folder_id: str | None = None, *, page: int = 0,
                   max_results: int = 50) -> dict:
    """The body the Panopto web UI posts to list sessions."""
    return {
        "queryParameters": {
            "query": None,
            "sortColumn": 1,
            "sortAscending": False,
            "maxResults": max_results,
            "page": page,
            "startDate": None,
            "endDate": None,
            "folderID": folder_id,
            "bookmarked": False,
            "getFolderData": True,
            "isSharedWithMe": False,
            "includePlaylists": True,
        }
    }


def parse_sessions(payload: object) -> list[Session]:
    """Sessions out of a ``GetSessions`` response.

    Tolerant on purpose: the response has been seen wrapped in ``d`` and
    unwrapped, and a probe that raises tells us less than one that reports
    an empty list.
    """
    data = json.loads(payload) if isinstance(payload, (str, bytes)) else payload
    if isinstance(data, dict):
        data = data.get("d", data)
    if isinstance(data, dict):
        data = data.get("Results") or data.get("results") or []
    if not isinstance(data, list):
        return []
    out: list[Session] = []
    for row in data:
        if not isinstance(row, dict):
            continue
        sid = row.get("DeliveryID") or row.get("SessionID") or row.get("Id")
        if not sid:
            continue
        out.append(Session(
            id=str(sid),
            name=str(row.get("SessionName") or row.get("Name") or "").strip(),
            date=(str(row.get("StartTime")) if row.get("StartTime") else None),
            folder=(str(row.get("FolderName")) if row.get("FolderName") else None),
            url=(str(row.get("ViewerUrl")) if row.get("ViewerUrl") else None),
        ))
    return out


def caption_url(host: str, session_id: str, *, language: int = 1) -> str:
    """The SRT caption track for one session."""
    return f"https://{host}{EP_CAPTIONS}?id={session_id}&language={language}"


_SRT_INDEX = re.compile(r"^\d+$")
# MULTILINE so `looks_like_srt` can search a whole document — the first line of
# a caption track is a cue number, so an anchored search would never match.
# `srt_to_text` uses .match() per line, which is unaffected.
_SRT_TIME = re.compile(r"^\d{2}:\d{2}:\d{2}[,.]\d{3}\s*-->", re.M)
_TAG = re.compile(r"<[^>]+>")


def srt_to_text(srt: str) -> str:
    """Plain prose from an SRT caption track.

    Drops the cue numbers and timecodes and collapses the repeated lines
    Panopto emits when a caption is held across cues — without that, a
    transcript is mostly duplicated sentences and every chunk of it looks
    like every other chunk to an embedder.
    """
    lines: list[str] = []
    for raw in (srt or "").splitlines():
        line = _TAG.sub("", raw).strip()
        if not line or _SRT_INDEX.match(line) or _SRT_TIME.match(line):
            continue
        if lines and line == lines[-1]:
            continue
        lines.append(line)
    return re.sub(r"[ \t]+", " ", "\n".join(lines)).strip()


def looks_like_srt(body: str) -> bool:
    """Is this actually a caption track, or a login page / empty body?

    An unauthenticated request against a restricted folder returns 200 with an
    empty body, so status alone cannot tell success from refusal.
    """
    return bool(body and _SRT_TIME.search(body))
