"use client";

import { useMemo } from "react";
import { isRefusalLabel } from "@/lib/brief";
import type { BriefResponse, ClusterCard, WeeklyCount } from "@/lib/brief";

function cleanLabel(label: string | null | undefined): string | null {
  if (!label) return null;
  if (isRefusalLabel(label)) return null;
  return label;
}

interface Props {
  brief: BriefResponse;
}

export function ResearchBrief({ brief }: Props) {
  return (
    <article className="space-y-6">
      <TranslationPanel translation={brief.translation} />

      {brief.confidence_low && brief.confidence_message && (
        <div className="rounded border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-900">
          <strong>Low confidence:</strong> {brief.confidence_message}
        </div>
      )}

      <section className="space-y-2">
        <h2 className="font-serif text-base text-stone-700">Orientation</h2>
        <p className="font-serif text-stone-800 leading-relaxed whitespace-pre-wrap">
          {brief.orientation}
        </p>
      </section>

      <section className="space-y-3">
        <h2 className="font-serif text-base text-stone-700">
          Matched clusters ({brief.cards.length})
        </h2>
        <ol className="space-y-4">
          {brief.cards.map((card, i) => (
            <li key={`${card.tier}-${card.label}`}>
              <ClusterCardView
                card={card}
                rank={i + 1}
                question={brief.translation.restated_question}
              />
            </li>
          ))}
        </ol>
      </section>

      {brief.next_queries.length > 0 && (
        <section className="space-y-2">
          <h2 className="font-serif text-base text-stone-700">
            Suggested next queries
          </h2>
          <ul className="list-disc pl-5 space-y-1 font-serif text-stone-800">
            {brief.next_queries.map((q, i) => (
              <li key={i}>{q}</li>
            ))}
          </ul>
        </section>
      )}

      <p className="text-[10px] text-stone-400 pt-4 border-t border-stone-200">
        Generated {new Date(brief.generated_at).toLocaleString()}
      </p>
    </article>
  );
}

function TranslationPanel({ translation }: { translation: BriefResponse["translation"] }) {
  return (
    <details className="rounded border border-stone-200 bg-white">
      <summary className="cursor-pointer px-3 py-2 text-xs uppercase tracking-wide text-stone-500 select-none">
        Translation to 1840s diction
      </summary>
      <div className="px-3 pb-3 space-y-2 text-sm text-stone-700">
        <div>
          <span className="text-xs uppercase tracking-wide text-stone-500">Restated </span>
          <span className="font-serif italic">{translation.restated_question}</span>
        </div>
        <ChipRow label="Period terms" items={translation.period_terms} />
        <ChipRow label="Likely entities" items={translation.likely_entities} />
        <ChipRow label="Search phrases" items={translation.search_phrases} />
        {translation.candidate_date_ranges.length > 0 && (
          <ChipRow label="Date ranges" items={translation.candidate_date_ranges} />
        )}
      </div>
    </details>
  );
}

function ChipRow({ label, items }: { label: string; items: string[] }) {
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap items-baseline gap-1.5">
      <span className="text-xs uppercase tracking-wide text-stone-500 mr-1">
        {label}
      </span>
      {items.map((item, i) => (
        <span
          key={i}
          className="text-xs bg-stone-100 border border-stone-200 rounded px-1.5 py-0.5 font-mono"
        >
          {item}
        </span>
      ))}
    </div>
  );
}

function ClusterCardView({
  card,
  rank,
  question,
}: {
  card: ClusterCard;
  rank: number;
  question: string;
}) {
  // Defensive label sanitization. The API already strips refusals,
  // but if a new refusal pattern surfaces, we don't want it on the page.
  const headerLabel =
    cleanLabel(card.label_text) ?? `(unlabeled cluster #${card.label})`;
  const cleanParents = card.parent_chain
    .map((p) => ({
      ...p,
      display: cleanLabel(p.label_text) ?? "(broad theme — unlabeled)",
    }));
  return (
    <div className="rounded border border-stone-200 bg-white shadow-sm p-4 space-y-3">
      <header className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="text-xs text-stone-400 font-mono">#{rank}</span>
            <h3 className="font-serif text-base text-stone-900 break-words">
              {headerLabel}
            </h3>
          </div>
          {cleanParents.length > 0 && (
            <p className="text-xs text-stone-500 mt-0.5 break-words">
              under: {cleanParents.map((p) => p.display).join(" › ")}
            </p>
          )}
        </div>
        <ShapeBadge tag={card.shape_tag} />
      </header>

      <p className="text-xs text-stone-600 italic">{card.shape_explanation}</p>

      <dl className="grid grid-cols-2 sm:grid-cols-4 gap-3 text-xs">
        <Metric label="Active / stored" value={`${card.active_size.toLocaleString()} / ${card.size.toLocaleString()}`} />
        <Metric
          label="Date range"
          value={`${card.date_min || "?"} → ${card.date_max || "?"}`}
        />
        <Metric label="Weeks" value={`${card.weeks}`} />
        <Metric
          label="Peak week"
          value={card.peak_week ? `${card.peak_week} (×${card.peak_count})` : "—"}
        />
        <Metric label="Burstiness" value={card.burstiness.toFixed(2)} />
        <Metric
          label="Net drift"
          value={card.drift_net !== null ? card.drift_net.toFixed(3) : "—"}
        />
        <Metric
          label="Direction ratio"
          value={card.drift_ratio !== null ? card.drift_ratio.toFixed(2) : "—"}
        />
        <Metric label="Relevance" value={card.relevance.toFixed(2)} />
      </dl>

      <Sparkline counts={card.weekly_counts} />

      {card.papers.length > 0 && (
        <PaperBar papers={card.papers} />
      )}

      <div className="pt-2 flex flex-wrap gap-3 text-xs">
        <a
          href={`/cluster/${card.cluster_id}?q=${encodeURIComponent(question)}`}
          className="text-amber-700 hover:text-amber-900 underline font-medium"
        >
          → View cluster dossier
        </a>
        <a
          href={`/?scope_tier=${card.tier}&scope_label=${card.label}`}
          className="text-amber-700 hover:text-amber-900 underline"
        >
          → Ask in chat, scoped to this cluster
        </a>
        <a
          href={`/explore?tier=${card.tier}&label=${card.label}`}
          className="text-amber-700 hover:text-amber-900 underline"
        >
          → View in Explore
        </a>
      </div>
    </div>
  );
}

function ShapeBadge({ tag }: { tag: string }) {
  return (
    <span className="text-xs px-2 py-0.5 rounded-full bg-stone-800 text-stone-50 whitespace-nowrap">
      {tag}
    </span>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <dt className="text-stone-500 uppercase tracking-wide text-[10px]">{label}</dt>
      <dd className="font-mono text-stone-800 break-words">{value}</dd>
    </div>
  );
}

function Sparkline({ counts }: { counts: WeeklyCount[] }) {
  const { points, max } = useMemo(() => {
    const max = Math.max(1, ...counts.map((c) => c.count));
    const w = 320;
    const h = 36;
    if (counts.length === 0) return { points: "", max };
    const stepX = counts.length === 1 ? w : w / (counts.length - 1);
    const pts = counts
      .map((c, i) => {
        const x = i * stepX;
        const y = h - (c.count / max) * (h - 2) - 1;
        return `${x.toFixed(1)},${y.toFixed(1)}`;
      })
      .join(" ");
    return { points: pts, max };
  }, [counts]);

  if (counts.length === 0) {
    return <p className="text-xs text-stone-500">No weekly data.</p>;
  }
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-stone-500">
        Weekly chunk count (peak {max})
      </div>
      <svg
        viewBox="0 0 320 36"
        preserveAspectRatio="none"
        className="w-full h-9 block"
      >
        <polyline
          points={points}
          fill="none"
          stroke="#78716c"
          strokeWidth={1.5}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <div className="flex justify-between text-[10px] text-stone-400 font-mono">
        <span>{counts[0]?.week}</span>
        <span>{counts[counts.length - 1]?.week}</span>
      </div>
    </div>
  );
}

function PaperBar({ papers }: { papers: ClusterCard["papers"] }) {
  return (
    <div className="space-y-1">
      <div className="text-[10px] uppercase tracking-wide text-stone-500">
        Contributing papers
      </div>
      <div className="flex h-3 w-full rounded overflow-hidden border border-stone-200">
        {papers.map((p, i) => (
          <div
            key={p.lccn}
            title={`${p.title}: ${p.count} (${(p.share * 100).toFixed(0)}%)`}
            style={{ width: `${p.share * 100}%` }}
            className={
              i === 0
                ? "bg-stone-700"
                : i === 1
                ? "bg-stone-500"
                : "bg-stone-300"
            }
          />
        ))}
      </div>
      <ul className="text-xs text-stone-600 grid grid-cols-1 sm:grid-cols-2 gap-x-3">
        {papers.map((p) => (
          <li key={p.lccn} className="truncate" title={p.title}>
            <span className="text-stone-800">{p.title}</span>{" "}
            <span className="text-stone-500">
              · {p.count} ({(p.share * 100).toFixed(0)}%)
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
