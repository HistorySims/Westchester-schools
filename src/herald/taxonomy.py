"""Locked taxonomies for structured extraction (docs/STRUCTURED.md, decision #4).

Normalization is deterministic and lives here — *not* in the model's head — so it
is auditable and stable across runs. ``herald-extract`` asks Claude only for the
raw labels it reads off a table; the canonical lane/category is computed here,
with a hand-maintained per-district crosswalk (``data/lane_crosswalk.csv``) taking
precedence over the pattern rules for opaque labels ("Column V", "Level 4").
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

# Canonical salary lanes, ordered least→most credited. Order is meaningful: the
# audit checks that a higher lane pays ≥ the lane to its left at the same step.
CANONICAL_LANES: list[str] = [
    "BA", "BA+15", "BA+30",
    "MA", "MA+15", "MA+30", "MA+45", "MA+60", "MA+75",
    "Doctorate",
]
_LANE_RANK = {lane: i for i, lane in enumerate(CANONICAL_LANES)}

STIPEND_CATEGORIES = ("athletics", "cocurricular", "extra_duty")

_WS = re.compile(r"\s+")


def _clean(s: str) -> str:
    return _WS.sub(" ", (s or "").strip().lower())


def lane_rank(lane: str) -> int:
    """Position in the canonical ordering, or -1 for 'other'/unknown."""
    return _LANE_RANK.get(lane, -1)


# ---- lane crosswalk ----------------------------------------------------

def load_crosswalk(path: str | Path) -> dict[tuple[str | None, str], str]:
    """Load ``data/lane_crosswalk.csv`` → {(district_slug|None, lane_raw_clean): canonical}.

    A blank ``district_slug`` cell means the mapping applies to every district
    (keyed under None). Missing file → empty crosswalk (pattern rules only).
    """
    p = Path(path)
    out: dict[tuple[str | None, str], str] = {}
    if not p.exists():
        return out
    # Drop leading '#' comment lines so the header row is the real one.
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines()
             if not ln.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
            raw = _clean(row.get("lane_raw", ""))
            canonical = (row.get("lane_canonical") or "").strip()
            if not raw or not canonical:
                continue
            slug = (row.get("district_slug") or "").strip() or None
            out[(slug, raw)] = canonical
    return out


# ---- lane normalization ------------------------------------------------

_DOCTORATE = re.compile(r"doctor|ph\.?\s*d|ed\.?\s*d", re.I)
_MASTER = re.compile(r"master|m\.?\s*a\b|\bms\b|\bm\b|m\s*\+", re.I)
_BACHELOR = re.compile(r"bachelor|b\.?\s*a\b|\bbs\b|\bb\b|b\s*\+", re.I)
_CREDITS = re.compile(r"(\d{2})")


def normalize_lane(
    raw: str,
    *,
    district_slug: str | None = None,
    crosswalk: dict[tuple[str | None, str], str] | None = None,
) -> str:
    """Map a printed lane label to a canonical lane, else 'other'.

    Crosswalk (district-specific first, then global) wins over the pattern rules;
    an unrecognized label is returned as ``'other'`` (the raw is always kept by
    the caller). Deliberately conservative — a wrong canonical lane silently
    corrupts cross-district rankings, so we prefer 'other' to a guess.
    """
    raw = (raw or "").strip()
    if not raw:
        return "other"
    if crosswalk:
        key = _clean(raw)
        for k in ((district_slug, key), (None, key)):
            if k in crosswalk:
                return crosswalk[k]
    if _DOCTORATE.search(raw):
        return "Doctorate"
    if _MASTER.search(raw):
        base = "MA"
    elif _BACHELOR.search(raw):
        base = "BA"
    else:
        return "other"
    m = _CREDITS.search(raw)
    lane = f"{base}+{int(m.group(1))}" if m else base
    return lane if lane in _LANE_RANK else "other"


# ---- stipend category / position --------------------------------------

_ATHLETIC = re.compile(
    r"coach|athletic|basketball|football|soccer|baseball|softball|volleyball"
    r"|track|wrestl|tennis|lacrosse|swim|cross.?country|golf|cheer|hockey|"
    r"gymnastic|fitness|intramural",
    re.I,
)
_COCURRIC = re.compile(
    r"advisor|adviser|club|yearbook|newspaper|musical|drama|theat|band"
    r"|chorus|orchestra|honor society|student council|robotics|debate|mock trial"
    r"|forensic|model un|nhs|dei|gsa",
    re.I,
)


def infer_category(position_raw: str) -> str:
    """Best-effort athletics / cocurricular / extra_duty from a position label."""
    if _ATHLETIC.search(position_raw or ""):
        return "athletics"
    if _COCURRIC.search(position_raw or ""):
        return "cocurricular"
    return "extra_duty"


def normalize_position(raw: str) -> str:
    """Light cleanup of a position label — collapse whitespace, keep wording."""
    return _WS.sub(" ", (raw or "").strip())
