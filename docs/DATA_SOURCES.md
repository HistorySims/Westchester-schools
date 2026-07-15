# Data sources & allow-list roadmap

BoardDocs (`go.boarddocs.com`) is allowlisted and working — board agendas,
minutes, and per-committee attachments. This plans the *next* sources
(meeting recordings, teacher contracts, student handbooks, policy manuals,
official data) and the hosts to allowlist for each.

Domains marked **✓** were observed directly in the captured BoardDocs
`/Public` pages, so they're verified, not guessed.

## Two allow-lists to keep straight

1. **This dev environment** — adding a host here lets me *build and test* an
   adapter against the live source. That is exactly what unblocked BoardDocs
   (from blind guessing to a working scraper in minutes). Add hosts
   incrementally, one per adapter.
2. **The crawl runtime (GitHub Actions)** — already has open network. Its
   constraints are per-source ToS and rate-limiting, not an allow-list.

Each new source becomes a new adapter alongside `boarddocs.py`, feeding the
same `manifest → ingest` contract. Build them one at a time.

## 1. District websites (handbooks, teacher contracts, budgets, misc PDFs)

These live on each district's own site (and its CMS CDN), not BoardDocs.
All eight domains are verified from the BoardDocs pages:

| district | website |
|---|---|
| Port Chester-Rye | portchesterschools.org ✓ |
| Ossining | ossiningufsd.org ✓ |
| Peekskill | peekskillcsd.org ✓ |
| Tarrytowns | tufsd.org ✓ |
| Elmsford | eufsd.org ✓ |
| Mount Vernon | mtvernoncsd.org ✓ |
| Greenburgh Central | greenburghcsd.org ✓ |
| White Plains | whiteplainspublicschools.org ✓ |
| Yonkers | yonkerspublicschools.org |

Adapter: a "district site" crawler that follows the site to linked PDFs
(handbook, CBA/teacher contract, budget…). More heuristic than BoardDocs.
The PDFs often sit on a CMS CDN, so allowlist that too once we see which each
district runs: Finalsite (`*.finalsite.com`), Apptegy/Thrillshare
(`*.thrillshare.com`), Google (`storage.googleapis.com`, `drive.google.com`),
or a WordPress/Squarespace host.

## 2. Policy manuals (found the real vendors)

Two policy-publishing platforms show up right in the BoardDocs pages — NY
districts commonly host the *full* board-policy manual on one of these,
separate from BoardDocs:

- **policy.microscribepub.com** ✓ (MicroScribe)
- **boardpolicyonline.com** ✓

Plus: several districts expose a **"Policies" committee inside BoardDocs**
(White Plains has a "Policy Committee") which the existing adapter can already
crawl. So policy docs = the BoardDocs policy committee **+** these vendor
sites. Allowlist: `policy.microscribepub.com`, `boardpolicyonline.com`.

## 3. Board-meeting recordings → transcription

Recordings aren't linked from BoardDocs; they're on district sites/channels:

- **YouTube** — most common (e.g. "Ossining UFSD TV"). Allowlist
  `youtube.com`, `youtu.be`, `googlevideo.com`, `i.ytimg.com`. Tool: yt-dlp.
- **Vimeo** — some districts: `vimeo.com`, `player.vimeo.com`, `*.vimeocdn.com`.
- **Municipal streamers** — BoxCast (`boxcast.com`), Granicus, Swagit, or
  Zoom (`zoom.us`) for a few.

**Big shortcut — captions first.** Many meeting videos already carry
(auto-)captions; yt-dlp pulls them directly = free text, zero transcription
compute. Only fall back to audio + ASR when captions are missing.

Transcription (when needed):
- **Local Whisper** (faster-whisper / whisper.cpp): no API cost, no
  per-request allowlist; model weights download once from **huggingface.co**
  (allowlist that). CPU is ~1x realtime — the real constraint is *compute*,
  not network.
- **API alternative**: `api.openai.com` (Whisper), `api.deepgram.com`,
  `api.assemblyai.com` — faster/managed but per-minute cost + those hosts.

⚠️ This is the heaviest pipeline (large downloads + ASR compute). Treat it as
its own milestone: captions-first, audio/ASR second, and plan storage.

## 4. Yonkers & other non-BoardDocs minutes

Yonkers uses iCompass / IC-Board (Diligent Community):
`yonkerspublic.ic-board.com` — allowlist `*.ic-board.com` when we build that
adapter. It publishes agenda *abstracts*, thinner than BoardDocs packets.

## 5. Official / aggregate context (enrichment)

Useful for the peer-comparison framing and grounding analysis:

- **NYSED report cards / data** — `data.nysed.gov`, `nysed.gov` (enrollment,
  demographics, finances, outcomes).
- **NYS Comptroller OpenBook** — `openbooknewyork.com`, `osc.ny.gov` (district
  budgets, audits).
- **SeeThroughNY** (Empire Center) — `seethroughny.net` (teacher salaries +
  some contracts).
- **NCES / Urban Institute** — `nces.ed.gov`, `educationdata.urban.org`.
- **Census** — `data.census.gov`, `api.census.gov` (town demographics).

## Recommended allow-list order (this env, as we build each adapter)

1. `youtube.com`, `youtu.be`, `googlevideo.com`, `i.ytimg.com` +
   `huggingface.co` — recordings/captions + local Whisper. *(Highest new value.)*
2. The **8 district domains** (+ CMS CDN once identified) — handbooks, contracts.
3. `policy.microscribepub.com`, `boardpolicyonline.com` — policy manuals.
4. `seethroughny.net`, `data.nysed.gov`, `openbooknewyork.com` — contracts,
   salaries, finances.
5. `*.ic-board.com` — Yonkers.
6. `vimeo.com`, `player.vimeo.com` — if a target district uses Vimeo.

## Caveats

- **ToS / ethics.** Public records and public-meeting content are fair game at
  polite rates. Downloading video from YouTube is technically against its ToS
  (pulling captions is lighter-touch); salaries/contracts on SeeThroughNY are
  already public. Judgment per source.
- **Compute, not network, is the recordings bottleneck.** Plan where ASR runs
  and where big audio lands.
- **Incremental.** One adapter at a time: allowlist the host here, reverse-
  engineer/test live, then run it in Actions.
