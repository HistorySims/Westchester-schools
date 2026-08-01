"use client";

import { useEffect, useRef, useState } from "react";

interface District {
  slug: string;
  name: string;
}
interface Citation {
  index: number;
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  section_path: string;
  heading: string | null;
  source_url: string;
  snippet: string;
}
interface SearchHit {
  chunk_id: string;
  district: string;
  meeting_date: string | null;
  doc_type: string | null;
  doc_title: string;
  heading: string | null;
  content: string;
  source_url: string;
}

const EXAMPLES = [
  "What's the normal cell phone policy across districts?",
  "Which districts are doing Middle States accreditation?",
  "Which schools pay coaches an unusual amount?",
];

function parseSSE(block: string): { event: string; data: Record<string, unknown> } | null {
  const lines = block.split("\n");
  let event = "message";
  let data = "";
  for (const line of lines) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return null;
  try {
    return { event, data: JSON.parse(data) };
  } catch {
    return null;
  }
}

// Render answer text with [N] markers turned into small superscript links.
function AnswerText({ text }: { text: string }) {
  const parts = text.split(/(\[\d+\])/g);
  return (
    <div className="whitespace-pre-wrap leading-relaxed text-[15px]">
      {parts.map((p, i) => {
        const m = p.match(/^\[(\d+)\]$/);
        if (m) {
          return (
            <a
              key={i}
              href={`#cite-${m[1]}`}
              className="mx-0.5 inline-block rounded bg-amber-200/70 px-1 text-[11px] font-semibold text-amber-900 no-underline align-super"
            >
              {m[1]}
            </a>
          );
        }
        return <span key={i}>{p}</span>;
      })}
    </div>
  );
}

export default function Home() {
  const [districts, setDistricts] = useState<District[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [tab, setTab] = useState<"ask" | "search">("ask");

  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState("");
  const [citations, setCitations] = useState<Citation[]>([]);
  const [empty, setEmpty] = useState<string[]>([]);
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | null>(null);

  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<SearchHit[]>([]);
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  const answerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetch("/api/districts")
      .then((r) => r.json())
      .then((d) => setDistricts(d.districts ?? []))
      .catch(() => {});
  }, []);

  function toggleDistrict(slug: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(slug)) next.delete(slug);
      else next.add(slug);
      return next;
    });
  }

  async function ask(q: string) {
    if (!q.trim() || asking) return;
    setAsking(true);
    setAnswer("");
    setCitations([]);
    setEmpty([]);
    setAskError(null);
    try {
      const resp = await fetch("/api/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          question: q,
          districts: selected.size ? [...selected] : null,
        }),
      });
      if (!resp.ok || !resp.body) {
        const t = await resp.text().catch(() => "");
        throw new Error(t || `Request failed (${resp.status})`);
      }
      const reader = resp.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        const parts = buf.split("\n\n");
        buf = parts.pop() ?? "";
        for (const part of parts) {
          const ev = parseSSE(part);
          if (!ev) continue;
          if (ev.event === "token") {
            setAnswer((a) => a + (ev.data.text as string));
          } else if (ev.event === "done") {
            setCitations((ev.data.citations as Citation[]) ?? []);
            setEmpty((ev.data.empty_districts as string[]) ?? []);
            if (ev.data.text) setAnswer(ev.data.text as string);
          } else if (ev.event === "error") {
            setAskError(ev.data.error as string);
          }
        }
      }
    } catch (e) {
      setAskError(e instanceof Error ? e.message : "Failed to get an answer.");
    } finally {
      setAsking(false);
    }
  }

  async function runSearch(q: string) {
    if (!q.trim() || searching) return;
    setSearching(true);
    setSearchError(null);
    setSearched(true);
    try {
      const params = new URLSearchParams({ q });
      if (selected.size) params.set("districts", [...selected].join(","));
      const resp = await fetch(`/api/search?${params}`);
      const data = await resp.json();
      if (!resp.ok) throw new Error((data.error as string) ?? "Search failed");
      setHits(data.hits ?? []);
    } catch (e) {
      setSearchError(e instanceof Error ? e.message : "Search failed.");
      setHits([]);
    } finally {
      setSearching(false);
    }
  }

  return (
    <main className="mx-auto max-w-3xl px-4 py-8 sm:py-12">
      <header className="mb-6">
        <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-amber-800">
          Westchester Schools
        </div>
        <h1 className="font-serif text-3xl leading-tight sm:text-4xl">
          Ask the district record
        </h1>
        <p className="mt-2 text-sm text-stone-600">
          Board agendas, minutes, policies, handbooks, contracts, and budgets from
          eight Westchester districts. Ask a comparative question and get a
          district-by-district answer with citations — or search every mention of a
          term.
        </p>
      </header>

      <div className="mb-4 inline-flex rounded-lg border border-stone-300 bg-white p-1 text-sm">
        {(["ask", "search"] as const).map((t) => (
          <button
            key={t}
            onClick={() => setTab(t)}
            className={`rounded-md px-4 py-1.5 font-medium transition ${
              tab === t ? "bg-amber-700 text-white" : "text-stone-600 hover:text-stone-900"
            }`}
          >
            {t === "ask" ? "Ask" : "Full-text search"}
          </button>
        ))}
      </div>

      {districts.length > 0 && (
        <div className="mb-5 flex flex-wrap gap-2">
          {districts.map((d) => {
            const on = selected.has(d.slug);
            return (
              <button
                key={d.slug}
                onClick={() => toggleDistrict(d.slug)}
                title={d.name}
                className={`rounded-full border px-3 py-1 text-xs transition ${
                  on
                    ? "border-amber-700 bg-amber-100 text-amber-900"
                    : "border-stone-300 bg-white text-stone-600 hover:border-stone-400"
                }`}
              >
                {d.slug}
              </button>
            );
          })}
          {selected.size > 0 && (
            <button
              onClick={() => setSelected(new Set())}
              className="px-2 py-1 text-xs text-stone-500 underline"
            >
              clear
            </button>
          )}
        </div>
      )}

      {tab === "ask" ? (
        <section>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              ask(question);
            }}
          >
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  ask(question);
                }
              }}
              rows={3}
              maxLength={500}
              placeholder="e.g. What's the normal cell phone policy across districts?"
              className="w-full resize-none rounded-lg border border-stone-300 bg-white p-3 text-[15px] outline-none focus:border-amber-600"
            />
            <div className="mt-2 flex items-center justify-between gap-3">
              <div className="flex flex-wrap gap-1.5">
                {EXAMPLES.map((ex) => (
                  <button
                    key={ex}
                    type="button"
                    onClick={() => {
                      setQuestion(ex);
                      ask(ex);
                    }}
                    className="rounded-full border border-stone-300 bg-white px-2.5 py-1 text-[11px] text-stone-600 hover:border-amber-500"
                  >
                    {ex}
                  </button>
                ))}
              </div>
              <button
                type="submit"
                disabled={asking || !question.trim()}
                className="shrink-0 rounded-lg bg-amber-700 px-5 py-2 text-sm font-medium text-white disabled:opacity-40"
              >
                {asking ? "Thinking…" : "Ask"}
              </button>
            </div>
          </form>

          {askError && (
            <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
              {askError}
            </div>
          )}

          {(answer || asking) && (
            <div ref={answerRef} className="mt-6 rounded-lg border border-stone-200 bg-white p-5">
              <AnswerText text={answer} />
              {asking && !answer && (
                <div className="text-sm text-stone-400">Retrieving evidence…</div>
              )}
              {empty.length > 0 && (
                <p className="mt-4 text-xs text-stone-500">
                  No evidence retrieved for: {empty.join(", ")}
                </p>
              )}
            </div>
          )}

          {citations.length > 0 && (
            <div className="mt-6">
              <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-stone-500">
                Sources
              </h2>
              <ol className="space-y-2">
                {citations.map((c) => (
                  <li
                    key={c.index}
                    id={`cite-${c.index}`}
                    className="rounded-lg border border-stone-200 bg-white p-3 text-sm"
                  >
                    <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-stone-500">
                      <span className="rounded bg-amber-200/70 px-1.5 font-semibold text-amber-900">
                        {c.index}
                      </span>
                      <span className="font-medium text-stone-700">{c.district}</span>
                      <span>· {c.meeting_date ?? "undated"}</span>
                      <span>· {c.doc_type ?? "document"}</span>
                    </div>
                    <a
                      href={c.source_url}
                      target="_blank"
                      rel="noreferrer"
                      className="font-medium text-amber-800 hover:underline"
                    >
                      {c.doc_title}
                      {c.heading ? ` — ${c.heading}` : ""}
                    </a>
                    <p className="mt-1 text-stone-600">{c.snippet}…</p>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </section>
      ) : (
        <section>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              runSearch(query);
            }}
            className="flex gap-2"
          >
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search every mention — e.g. turf, valedictorian, Chromebook"
              className="w-full rounded-lg border border-stone-300 bg-white px-3 py-2 text-[15px] outline-none focus:border-amber-600"
            />
            <button
              type="submit"
              disabled={searching || !query.trim()}
              className="shrink-0 rounded-lg bg-amber-700 px-5 py-2 text-sm font-medium text-white disabled:opacity-40"
            >
              {searching ? "…" : "Find"}
            </button>
          </form>

          {searchError && (
            <div className="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-800">
              {searchError}
            </div>
          )}

          {searched && !searching && (
            <p className="mt-4 text-xs text-stone-500">
              {hits.length} match{hits.length === 1 ? "" : "es"}
            </p>
          )}

          <ul className="mt-2 space-y-2">
            {hits.map((h) => (
              <li key={h.chunk_id} className="rounded-lg border border-stone-200 bg-white p-3 text-sm">
                <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-stone-500">
                  <span className="font-medium text-stone-700">{h.district}</span>
                  <span>· {h.meeting_date ?? "undated"}</span>
                  <span>· {h.doc_type ?? "document"}</span>
                </div>
                <a
                  href={h.source_url}
                  target="_blank"
                  rel="noreferrer"
                  className="font-medium text-amber-800 hover:underline"
                >
                  {h.doc_title}
                  {h.heading ? ` — ${h.heading}` : ""}
                </a>
                <p className="mt-1 text-stone-600">{h.content.slice(0, 280)}…</p>
              </li>
            ))}
          </ul>
        </section>
      )}

      <footer className="mt-12 border-t border-stone-200 pt-4 text-xs text-stone-400">
        Answers are generated from retrieved documents and cite their sources.
        Absence of evidence is reported honestly, not treated as proof.
      </footer>
    </main>
  );
}
