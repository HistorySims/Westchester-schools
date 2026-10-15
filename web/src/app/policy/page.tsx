"use client";

/**
 * The policy atlas: a derived baseline manual, and what each district did to it.
 *
 * Reads a static JSON built by `scripts/policy_atlas.py`, so the page needs no
 * database and renders while Supabase is asleep.
 *
 * Two things the visual has to be honest about, both of them hard-won:
 *
 * 1. There is no published NYS model manual in this corpus. The baseline is
 *    *derived* — a subject five or more of the eight districts carry is
 *    operationally the template. Stated in the header rather than implied.
 * 2. Subjects are aligned by text, not by policy number, because the number
 *    means different things in different districts: attendance is 5100 in six
 *    districts and 7110 in Greenburgh and Mount Vernon, and 1120 is three
 *    unrelated subjects. Alignment confidence is published per subject so a
 *    loose group can be marked instead of hidden.
 */

import { useEffect, useMemo, useState } from "react";

type State = "kept" | "rewritten" | "added" | "missing";

interface PerDistrict {
  state: State;
  similarity: number;
  chars: number;
  number: string;
  title: string;
  url: string;
}

interface Subject {
  id: number;
  series: string;
  title: string;
  kind: "core" | "common" | "local";
  held_by: string[];
  absent: string[];
  confidence: number;
  numbers: string[];
  districts: Record<string, PerDistrict>;
}

interface Divergence {
  kind: "value" | "unit";
  wording: number;
  a: string;
  b: string;
  a_values: string[];
  b_values: string[];
  clause_a: string;
  clause_b: string;
}

interface CreditRow {
  district: string;
  value: number | null;
  unit: string;
  label: string;
  policy: string;
  quote: string;
  url: string;
  standard: string;
}

interface Tally {
  district: string;
  series: string;
  kept: number;
  rewritten: number;
  added: number;
  missing: number;
}

interface Atlas {
  generated: string;
  as_of: Record<string, string>;
  districts: string[];
  series: Record<string, string>;
  thresholds: Record<string, number>;
  counts: Record<string, number>;
  old_scheme: string[];
  tally: Tally[];
  subjects: Subject[];
  parameters: Record<string, Divergence[]>;
  credit_example: CreditRow[];
}

const NAMES: Record<string, string> = {
  "elmsford": "Elmsford",
  "greenburgh-central": "Greenburgh Central",
  "mount-vernon": "Mount Vernon",
  "ossining": "Ossining",
  "peekskill": "Peekskill",
  "port-chester-rye": "Port Chester-Rye",
  "tarrytowns": "Tarrytowns",
  "white-plains": "White Plains",
};

/** Additions grow up in green, absences down in red, the baseline carries reuse. */
const UP = "#2f6b4f";
const DOWN = "#9b3025";
const KEPT = "#c9bda4";
const REWRITTEN = "#b5761f";
const INK = "#2c1810";

const LANE_H = 86;
const MID = 43;
const MAX_BAR = 34;
const LABEL_W = 150;

export default function PolicyAtlas() {
  const [atlas, setAtlas] = useState<Atlas | null>(null);
  const [error, setError] = useState("");
  const [picked, setPicked] = useState<{ district: string; series: string } | null>(null);
  const [subject, setSubject] = useState<Subject | null>(null);

  useEffect(() => {
    fetch("/policy-atlas.json")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then(setAtlas)
      .catch((e) => setError(String(e)));
  }, []);

  const seriesKeys = useMemo(
    () => (atlas ? Object.keys(atlas.series).sort() : []),
    [atlas],
  );

  // One lookup for the lanes, and the scale the bars share so lanes are
  // comparable to each other rather than each normalised to itself.
  const { byKey, scale } = useMemo(() => {
    const map = new Map<string, Tally>();
    let max = 1;
    for (const t of atlas?.tally ?? []) {
      map.set(`${t.district}|${t.series}`, t);
      max = Math.max(max, t.added, t.missing, t.kept + t.rewritten);
    }
    return { byKey: map, scale: max };
  }, [atlas]);

  if (error) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-16">
        <h1 className="font-serif text-3xl">Policy atlas</h1>
        <p className="mt-4 text-[#9b3025]">
          Could not load <code>/policy-atlas.json</code>: {error}
        </p>
        <p className="mt-2 text-sm opacity-70">
          Build it with <code>uv run scripts/policy_atlas.py</code>.
        </p>
      </main>
    );
  }
  if (!atlas) {
    return (
      <main className="mx-auto max-w-5xl px-4 py-16">
        <p className="opacity-60">Loading the atlas…</p>
      </main>
    );
  }

  const bandW = 76;
  const chartW = LABEL_W + seriesKeys.length * bandW;

  return (
    <main className="mx-auto max-w-6xl px-4 py-10">
      <header>
        <h1 className="font-serif text-4xl tracking-tight">Policy atlas</h1>
        <p className="mt-3 max-w-3xl leading-relaxed">
          Eight Westchester districts license their board policy from the same
          handful of vendors, so most of any two manuals is the same words. This
          shows what each district did to that shared template: what it{" "}
          <strong>added</strong>, what it <strong>does not carry</strong>, and
          where it rewrote the text rather than adopting it.
        </p>
        <p className="mt-3 max-w-3xl text-sm leading-relaxed opacity-75">
          There is no published model manual in this corpus, so the baseline is
          derived: a subject at least {atlas.thresholds.core_min} of the 8
          districts carry counts as template. Subjects are matched by their{" "}
          <em>text and title</em>, never by policy number — attendance is 5100
          in six districts and 7110 in two, and 1120 is three different
          subjects. {atlas.counts.policies.toLocaleString()} policies,{" "}
          {atlas.counts.subjects.toLocaleString()} subjects,{" "}
          {atlas.counts.core} of them template. Policy as published{" "}
          {Object.values(atlas.as_of).sort().join(" and ")}.
        </p>
      </header>

      <section className="mt-10">
        <div className="flex flex-wrap items-center gap-x-5 gap-y-2 text-xs">
          <Key color={UP} label="added — few peers carry it" />
          <Key color={DOWN} label="missing — most peers carry it" />
          <Key color={KEPT} label="kept — text still matches peers" />
          <Key color={REWRITTEN} label="rewritten — same subject, own words" />
        </div>

        <div className="mt-4 overflow-x-auto">
          <svg
            width={chartW}
            height={atlas.districts.length * LANE_H + 46}
            className="min-w-full"
            role="img"
            aria-label="Policy divergence by district and category"
          >
            {seriesKeys.map((s, i) => (
              <text
                key={s}
                x={LABEL_W + i * bandW + bandW / 2}
                y={14}
                textAnchor="middle"
                fontSize={10}
                fill={INK}
                opacity={0.7}
              >
                {s.replace("000", "k")}
              </text>
            ))}

            {atlas.districts.map((d, row) => {
              const top = 26 + row * LANE_H;
              return (
                <g key={d}>
                  <text
                    x={LABEL_W - 10}
                    y={top + MID + 4}
                    textAnchor="end"
                    fontSize={12}
                    fill={INK}
                    className="font-sans"
                  >
                    {NAMES[d] ?? d}
                    {atlas.old_scheme?.includes(d) ? " †" : ""}
                  </text>
                  {/* the baseline this district is measured against */}
                  <line
                    x1={LABEL_W}
                    y1={top + MID}
                    x2={chartW}
                    y2={top + MID}
                    stroke={INK}
                    strokeOpacity={0.25}
                  />
                  {seriesKeys.map((s, i) => {
                    const t = byKey.get(`${d}|${s}`);
                    const x = LABEL_W + i * bandW + 8;
                    const w = bandW - 16;
                    const up = t ? (t.added / scale) * MAX_BAR : 0;
                    const down = t ? (t.missing / scale) * MAX_BAR : 0;
                    const shared = t ? t.kept + t.rewritten : 0;
                    const reuse = shared ? (t as Tally).kept / shared : 0;
                    const active =
                      picked?.district === d && picked?.series === s;
                    return (
                      <g
                        key={s}
                        onClick={() => setPicked(active ? null : { district: d, series: s })}
                        className="cursor-pointer"
                      >
                        <rect
                          x={LABEL_W + i * bandW}
                          y={top}
                          width={bandW}
                          height={LANE_H - 6}
                          fill={active ? INK : "transparent"}
                          fillOpacity={active ? 0.06 : 0}
                        />
                        {/* reuse bar sits ON the baseline: pale where the
                            district copied, amber where it rewrote */}
                        {shared > 0 && (
                          <rect
                            x={x}
                            y={top + MID - 3}
                            width={w}
                            height={6}
                            fill={reuse > 0.5 ? KEPT : REWRITTEN}
                            fillOpacity={0.35 + 0.65 * Math.abs(reuse - 0.5) * 2}
                          />
                        )}
                        {up > 0.5 && (
                          <rect
                            x={x}
                            y={top + MID - 4 - up}
                            width={w}
                            height={up}
                            fill={UP}
                            fillOpacity={0.85}
                          />
                        )}
                        {down > 0.5 && (
                          <rect
                            x={x}
                            y={top + MID + 4}
                            width={w}
                            height={down}
                            fill={DOWN}
                            fillOpacity={0.85}
                          />
                        )}
                        <title>
                          {`${NAMES[d] ?? d} · ${atlas.series[s]}\n`}
                          {`${t?.added ?? 0} added, ${t?.missing ?? 0} missing\n`}
                          {`${t?.kept ?? 0} kept, ${t?.rewritten ?? 0} rewritten`}
                        </title>
                      </g>
                    );
                  })}
                </g>
              );
            })}
          </svg>
        </div>
        <p className="mt-2 max-w-3xl text-xs leading-relaxed opacity-60">
          Bars share one scale across all lanes, so districts are comparable.
          Click a cell for the policies behind it.{" "}
          {atlas.old_scheme?.length > 0 && (
            <>
              <strong>†</strong> These districts number their manual on an older
              scheme — students are 7000 rather than 5000 and the 9000 personnel
              series is empty — so a category heading follows the majority of a
              subject&rsquo;s holders and can disagree with how they file it. A
              crosswalk was measured and is only 53–80% reliable per series, so
              it is not applied; each district&rsquo;s own number is shown
              throughout.
            </>
          )}
        </p>
      </section>

      {picked && (
        <CellDetail
          atlas={atlas}
          district={picked.district}
          series={picked.series}
          onPickSubject={setSubject}
        />
      )}

      {subject && (
        <SubjectDetail
          atlas={atlas}
          subject={subject}
          onClose={() => setSubject(null)}
        />
      )}

      <CreditExample rows={atlas.credit_example} />

      <footer className="mt-16 border-t border-[#2c1810]/15 pt-4 text-xs opacity-60">
        Generated {atlas.generated} by <code>scripts/policy_atlas.py</code>.
        Reuse is measured on 5-word shingles, not embeddings: two policies
        differing only in a number sit at cosine ~0.999, so distance cannot see
        the thing that matters here.
      </footer>
    </main>
  );
}

function Key({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-2">
      <span
        className="inline-block h-3 w-3 rounded-sm"
        style={{ background: color }}
      />
      {label}
    </span>
  );
}

function CellDetail({
  atlas,
  district,
  series,
  onPickSubject,
}: {
  atlas: Atlas;
  district: string;
  series: string;
  onPickSubject: (s: Subject) => void;
}) {
  const groups = useMemo(() => {
    const out: Record<string, Subject[]> = {
      added: [],
      missing: [],
      rewritten: [],
      kept: [],
    };
    for (const s of atlas.subjects) {
      if (s.series !== series) continue;
      if (s.absent.includes(district)) out.missing.push(s);
      const mine = s.districts[district];
      if (mine) out[mine.state].push(s);
    }
    for (const k of Object.keys(out)) {
      out[k].sort((a, b) => a.title.localeCompare(b.title));
    }
    return out;
  }, [atlas, district, series]);

  const order: { key: keyof typeof groups; label: string; color: string }[] = [
    { key: "added", label: "Added — few peers carry it", color: UP },
    { key: "missing", label: "Missing — most peers carry it", color: DOWN },
    { key: "rewritten", label: "Rewritten in its own words", color: REWRITTEN },
    { key: "kept", label: "Kept as the template reads", color: KEPT },
  ];

  return (
    <section className="mt-10 rounded-lg border border-[#2c1810]/15 bg-white/50 p-5">
      <h2 className="font-serif text-2xl">
        {NAMES[district] ?? district} · {atlas.series[series]}
      </h2>
      {atlas.old_scheme?.includes(district) && (
        <p className="mt-1 text-xs opacity-70">
          This district uses the older numbering, where this series does not
          mean what the heading says — read the policy numbers below.
        </p>
      )}
      <div className="mt-4 grid gap-6 md:grid-cols-2">
        {order.map(({ key, label, color }) => (
          <div key={key}>
            <h3
              className="text-xs font-semibold uppercase tracking-wide"
              style={{ color }}
            >
              {label} ({groups[key].length})
            </h3>
            <ul className="mt-2 space-y-1 text-sm">
              {groups[key].slice(0, 14).map((s) => (
                <li key={s.id}>
                  <button
                    className="text-left underline decoration-dotted underline-offset-2 hover:opacity-70"
                    onClick={() => onPickSubject(s)}
                  >
                    {s.districts[district]?.number ? (
                      <span className="font-mono text-xs opacity-70">
                        {s.districts[district].number}{" "}
                      </span>
                    ) : null}
                    {s.title.replace(/^\s*[\d.\-A-Za-z]*\s+/, "")}
                  </button>
                  {s.confidence < 0.34 && (
                    <span
                      className="ml-1 text-xs opacity-50"
                      title={`Loose match (confidence ${s.confidence}); the grouping may be wrong`}
                    >
                      ?
                    </span>
                  )}
                </li>
              ))}
              {groups[key].length > 14 && (
                <li className="text-xs opacity-60">
                  + {groups[key].length - 14} more
                </li>
              )}
              {groups[key].length === 0 && (
                <li className="text-xs opacity-50">none</li>
              )}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}

function SubjectDetail({
  atlas,
  subject,
  onClose,
}: {
  atlas: Atlas;
  subject: Subject;
  onClose: () => void;
}) {
  const divs = atlas.parameters[String(subject.id)] ?? [];
  return (
    <section className="mt-6 rounded-lg border-2 border-[#2c1810]/25 bg-white/70 p-5">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-serif text-2xl">
            {subject.title.replace(/^\s*[\d.\-A-Za-z]*\s+/, "")}
          </h2>
          <p className="mt-1 text-xs opacity-70">
            {subject.kind} subject · filed as{" "}
            {subject.numbers.join(", ") || "—"} · held by{" "}
            {subject.held_by.length} of {atlas.districts.length} · match
            confidence {subject.confidence}
            {subject.confidence < 0.34 && " (loose — check the titles below)"}
          </p>
        </div>
        <button className="text-sm underline" onClick={onClose}>
          close
        </button>
      </div>

      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide opacity-60">
          <tr>
            <th className="py-1">District</th>
            <th className="py-1">Filed as</th>
            <th className="py-1">Text reuse</th>
            <th className="py-1">Its title there</th>
          </tr>
        </thead>
        <tbody>
          {atlas.districts.map((d) => {
            const mine = subject.districts[d];
            return (
              <tr key={d} className="border-t border-[#2c1810]/10">
                <td className="py-1 pr-2">{NAMES[d] ?? d}</td>
                <td className="py-1 pr-2 font-mono text-xs">
                  {mine?.number || (subject.absent.includes(d) ? "—" : "")}
                </td>
                <td className="py-1 pr-2">
                  {mine ? (
                    <span
                      style={{
                        color: mine.state === "rewritten" ? REWRITTEN : undefined,
                      }}
                    >
                      {mine.state === "added"
                        ? "local"
                        : `${Math.round(mine.similarity * 100)}%`}
                    </span>
                  ) : (
                    <span style={{ color: DOWN }}>does not carry it</span>
                  )}
                </td>
                <td className="py-1">
                  {mine?.url ? (
                    <a
                      className="underline decoration-dotted underline-offset-2"
                      href={mine.url}
                      target="_blank"
                      rel="noreferrer"
                    >
                      {mine.title}
                    </a>
                  ) : (
                    mine?.title ?? ""
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>

      {divs.length > 0 && (
        <div className="mt-5">
          <h3 className="text-xs font-semibold uppercase tracking-wide opacity-70">
            Same clause, different number ({divs.length})
          </h3>
          {divs.slice(0, 6).map((v, i) => (
            <div key={i} className="mt-3 border-l-2 border-[#b5761f] pl-3 text-sm">
              <p className="text-xs opacity-70">
                {v.kind === "value" ? "different value" : "same value, different unit"}{" "}
                · wording {Math.round(v.wording * 100)}% identical
              </p>
              <p className="mt-1">
                <strong>{NAMES[v.a] ?? v.a}</strong>: {v.a_values.join(", ")}
              </p>
              <p className="opacity-80">{v.clause_a}</p>
              <p className="mt-1">
                <strong>{NAMES[v.b] ?? v.b}</strong>: {v.b_values.join(", ")}
              </p>
              <p className="opacity-80">{v.clause_b}</p>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function CreditExample({ rows }: { rows: CreditRow[] }) {
  if (!rows?.length) return null;
  return (
    <section className="mt-14">
      <h2 className="font-serif text-2xl">A worked example: losing credit for absence</h2>
      <p className="mt-2 max-w-3xl text-sm leading-relaxed opacity-80">
        Four districts share one licensed sentence and fill in three different
        numbers. One holds the same template with the threshold sentence deleted
        and leaves the decision to an administrator; one has no provision at
        all. The number is not the whole rule either — Greenburgh counts all
        absences, excused and unexcused, where Tarrytowns counts only unexcused
        ones, so two districts at 18 are not applying the same standard.
      </p>
      <table className="mt-4 w-full text-sm">
        <thead className="text-left text-xs uppercase tracking-wide opacity-60">
          <tr>
            <th className="py-1">District</th>
            <th className="py-1">Threshold</th>
            <th className="py-1">Standard</th>
            <th className="py-1">Policy</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.district} className="border-t border-[#2c1810]/10 align-top">
              <td className="py-1.5 pr-2">{NAMES[r.district] ?? r.district}</td>
              <td className="py-1.5 pr-2 font-semibold">
                {r.label || "—"}
              </td>
              <td className="py-1.5 pr-2">
                <span
                  style={{
                    color:
                      r.standard === "no provision"
                        ? DOWN
                        : r.standard === "administrator discretion"
                          ? REWRITTEN
                          : undefined,
                  }}
                >
                  {r.standard}
                </span>
              </td>
              <td className="py-1.5">
                {r.url ? (
                  <a
                    className="underline decoration-dotted underline-offset-2"
                    href={r.url}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {r.policy}
                  </a>
                ) : (
                  r.policy
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}
