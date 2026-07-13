#!/usr/bin/env bash
#
# bootstrap_fork.sh — mechanically fork the Herald engine to a new corpus.
#
# Run this INSIDE a fresh clone of Herald whose .git has been removed
# (see docs/ENGINE_EXTRACTION.md). It does only the SAFE mechanical
# parts of a clean fork:
#
#   1. deletes newspaper-specific + shelved-recovery files (STRIP list)
#   2. renames the Python package  herald -> <newpkg>
#   3. rewrites obvious "herald" references in packaging metadata
#   4. drops a TODO banner on every REWRITE-list file so you can't miss
#      the domain work that still has to be done by hand
#
# It does NOT rewrite any application logic, schema, or prompts — those
# are judgment calls you review yourself. It is deliberately loud and
# refuses to run against the real Herald checkout (presence of .git),
# so you can't strip your source repo by accident.
#
# Usage:
#   bash scripts/bootstrap_fork.sh <newpkg> "<project description>"
# Example:
#   bash scripts/bootstrap_fork.sh schoolsengine "Westchester schools research"

set -euo pipefail

NEWPKG="${1:-}"
DESC="${2:-A semantic research engine}"

if [[ -z "$NEWPKG" ]]; then
  echo "usage: bash scripts/bootstrap_fork.sh <newpkg> \"<description>\"" >&2
  exit 2
fi
if [[ ! "$NEWPKG" =~ ^[a-z][a-z0-9_]*$ ]]; then
  echo "error: <newpkg> must be a valid lowercase python package name " \
       "(letters, digits, underscore; no leading digit)." >&2
  exit 2
fi
if [[ -d .git ]]; then
  echo "refusing to run: a .git directory is present." >&2
  echo "run this in a FRESH clone with .git removed, so you don't strip" >&2
  echo "the real Herald repo. See docs/ENGINE_EXTRACTION.md." >&2
  exit 1
fi
if [[ ! -d src/herald ]]; then
  echo "error: src/herald not found — are you in a Herald clone root?" >&2
  exit 1
fi

echo "==> Forking Herald engine -> package '$NEWPKG'"
echo

# --- 1. STRIP: newspaper-specific + shelved recovery -------------------

STRIP_FILES=(
  src/herald/loc.py
  db/migrations/0010_quarantine_recovery.sql
  scripts/american_stories_gate1.py
  scripts/american_stories_gate2.py
  scripts/recovery_score.py
  scripts/recovery_tuning.py
  scripts/quarantine_by_cluster_refusal.py
  scripts/quarantine_probe.py
  scripts/revert_cluster_refused.py
  scripts/bertopic_diagnostic.py
  .github/workflows/answer-key-gate1.yml
  .github/workflows/answer-key-gate2.yml
  .github/workflows/recovery-score.yml
  .github/workflows/recovery-tuning.yml
  .github/workflows/quarantine-cluster-refusal.yml
  .github/workflows/quarantine-probe.yml
  .github/workflows/revert-cluster-refused.yml
  .github/workflows/bertopic.yml
)
STRIP_DIRS=(
  data/recovery_eval
)

echo "-- Stripping newspaper/recovery-specific files"
for f in "${STRIP_FILES[@]}"; do
  if [[ -e "$f" ]]; then rm -f "$f" && echo "   removed $f"; fi
done
for d in "${STRIP_DIRS[@]}"; do
  if [[ -d "$d" ]]; then rm -rf "$d" && echo "   removed $d/"; fi
done
echo

# --- 2. Rename the Python package --------------------------------------

echo "-- Renaming package herald -> $NEWPKG"
git_mv() { mkdir -p "$(dirname "$2")"; mv "$1" "$2"; }
if [[ -d "src/herald" ]]; then
  git_mv "src/herald" "src/$NEWPKG"
  echo "   src/herald -> src/$NEWPKG"
fi

# Update every `herald` import / reference inside python + scripts.
# Word-boundary match so we don't clobber substrings.
echo "-- Rewriting 'herald' package references in .py"
while IFS= read -r -d '' file; do
  # `from herald`, `import herald`, `herald.` — package refs only.
  sed -i -E "s/\bfrom herald\b/from $NEWPKG/g; \
             s/\bimport herald\b/import $NEWPKG/g; \
             s/\bherald\./$NEWPKG./g" "$file"
done < <(find src scripts -name '*.py' -print0 2>/dev/null)

# --- 3. Packaging metadata --------------------------------------------

echo "-- Updating packaging metadata"
if [[ -f pyproject.toml ]]; then
  sed -i -E "s/^name = \"herald\"/name = \"$NEWPKG\"/" pyproject.toml
  sed -i -E "s/^description = \".*\"/description = \"$DESC\"/" pyproject.toml
  # tool tables / script entrypoints that name the package
  sed -i -E "s/\bherald\b/$NEWPKG/g" pyproject.toml
  echo "   pyproject.toml"
fi

# --- 4. Banner the REWRITE-list files ----------------------------------

REWRITE_FILES=(
  "src/$NEWPKG/models.py|schema: rename Paper/Issue/Page -> District/Document; decide collapse vs keep pages"
  "src/$NEWPKG/ingest.py|ingest adapter: replace the loc.gov fetch with your PDF/transcript parser (MILESTONE 1)"
  "src/$NEWPKG/cli.py|CLI: rework commands (ingest --lccn/--from/--to -> --district/--year)"
  "src/$NEWPKG/classify.py|keep quality sub-scores; DROP the ad/legal/bad-ocr newspaper content-type classifier"
  "src/$NEWPKG/synth.py|rewrite persona/attribution prompt for district (not paper) attribution"
  "db/migrations/0001_init.sql|schema: papers/issues/pages -> districts/documents; drop LoC-specific columns"
  "db/migrations/0003_explore_rpcs.sql|RPCs reference the schema — update table/column names to match 0001"
  ".github/workflows/ingest.yml|inputs: lccn/date -> district/year for the new ingest adapter"
)

echo "-- Bannering files that still need hand rewrites"
banner_py() {
  local file="$1" note="$2"
  [[ -f "$file" ]] || return 0
  local tmp; tmp="$(mktemp)"
  {
    echo "# ============================================================"
    echo "# FORK TODO ($NEWPKG): $note"
    echo "# Inherited from Herald; rewrite for this corpus before use."
    echo "# See docs/ENGINE_EXTRACTION.md."
    echo "# ============================================================"
    cat "$file"
  } > "$tmp"
  mv "$tmp" "$file"
  echo "   bannered $file"
}
banner_sql() {
  local file="$1" note="$2"
  [[ -f "$file" ]] || return 0
  local tmp; tmp="$(mktemp)"
  {
    echo "-- ============================================================"
    echo "-- FORK TODO ($NEWPKG): $note"
    echo "-- Inherited from Herald; rewrite for this corpus before use."
    echo "-- See docs/ENGINE_EXTRACTION.md."
    echo "-- ============================================================"
    cat "$file"
  } > "$tmp"
  mv "$tmp" "$file"
  echo "   bannered $file"
}
banner_yml() {
  local file="$1" note="$2"
  [[ -f "$file" ]] || return 0
  local tmp; tmp="$(mktemp)"
  {
    echo "# FORK TODO ($NEWPKG): $note — see docs/ENGINE_EXTRACTION.md"
    cat "$file"
  } > "$tmp"
  mv "$tmp" "$file"
  echo "   bannered $file"
}
for entry in "${REWRITE_FILES[@]}"; do
  file="${entry%%|*}"; note="${entry#*|}"
  case "$file" in
    *.py)  banner_py  "$file" "$note" ;;
    *.sql) banner_sql "$file" "$note" ;;
    *.yml) banner_yml "$file" "$note" ;;
  esac
done

echo
echo "==> Done. Next:"
echo "   1. grep -rn 'FORK TODO' src db .github   # your worklist"
echo "   2. Review web/ for 'paper' copy to relabel as 'district'"
echo "   3. Provision a NEW Supabase project (do not reuse Herald's)"
echo "   4. git init && git add -A && git commit && gh repo create ..."
echo "   5. Build the ingest adapter once you have sample documents."
echo
echo "   Full guide: docs/ENGINE_EXTRACTION.md"
