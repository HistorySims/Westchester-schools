"""One-off BERTopic diagnostic against the Herald corpus.

THROWAWAY ANALYSIS CODE — not integrated into the app, not used by
production. Run once to compare BERTopic's discovered topics against
our custom clustering pipeline's tier-0 HDBSCAN labels.

What it does:
1. Pulls all current chunk content + precomputed Voyage embeddings
   from Supabase (read-only).
2. Hands the embeddings to BERTopic directly (no re-embed) and lets
   it run its own UMAP → HDBSCAN → c-TF-IDF chain.
3. Writes a Markdown report listing every topic with its size and
   top ~10 c-TF-IDF keywords, sorted by size.
4. Prints the actual fit parameters BERTopic used so you can
   compare apples-to-apples with src/herald/cluster.py.

Usage:
    uv run scripts/bertopic_diagnostic.py
    # or:
    python scripts/bertopic_diagnostic.py

Requires: SUPABASE_DB_URL in env or .env, and bertopic installed
(it's in the [dependency-groups] dev extras — `uv sync --group dev`
or `uv pip install bertopic`).

Output: scripts/bertopic_output_YYYYMMDD-HHMMSS.md
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import psycopg
from pgvector.psycopg import register_vector

from herald import settings


SCRIPT_DIR = Path(__file__).parent


def main() -> None:
    cfg = settings.load()
    if not cfg.supabase_db_url:
        print("SUPABASE_DB_URL is not set; aborting.", file=sys.stderr)
        sys.exit(1)

    print("Loading chunks + embeddings from Supabase (read-only)...")
    chunk_ids, contents, embeddings = load_corpus(cfg.supabase_db_url)
    print(f"  Loaded {len(chunk_ids):,} chunks, embedding dim={embeddings.shape[1]}")

    print("\nImporting BERTopic (this can take a few seconds)...")
    from bertopic import BERTopic

    # Let BERTopic build its default UMAP, HDBSCAN, vectorizer, c-TF-IDF
    # so we're seeing exactly what an out-of-the-box BERTopic run would
    # discover. We just disable probability calculation (slow) and turn
    # on verbose so progress is visible.
    topic_model = BERTopic(
        calculate_probabilities=False,
        verbose=True,
    )

    print("\nRunning BERTopic.fit_transform with precomputed Voyage embeddings...")
    topics, _ = topic_model.fit_transform(
        documents=contents,
        embeddings=embeddings,
    )
    n_topics = len({t for t in topics if t != -1})
    n_noise = sum(1 for t in topics if t == -1)
    print(
        f"\n  Discovered {n_topics:,} non-noise topics, "
        f"{n_noise:,} outliers ({100 * n_noise / len(topics):.1f}%)"
    )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_path = SCRIPT_DIR / f"bertopic_output_{timestamp}.md"
    print(f"\nWriting report → {output_path}")
    write_report(
        topic_model=topic_model,
        n_docs=len(chunk_ids),
        n_topics=n_topics,
        n_noise=n_noise,
        output_path=output_path,
    )
    print("Done.")


def load_corpus(db_url: str) -> tuple[list[UUID], list[str], np.ndarray]:
    """Read all current chunks (id, content, embedding) from the DB."""
    chunk_ids: list[UUID] = []
    contents: list[str] = []
    emb_list: list[list[float]] = []

    conn = psycopg.connect(db_url, autocommit=False, prepare_threshold=None)
    register_vector(conn)
    try:
        with conn.cursor(name="bertopic_load") as cur:
            cur.itersize = 5000
            cur.execute(
                """
                SELECT id, content, embedding
                FROM chunks
                WHERE is_current = true
                  AND embedding IS NOT NULL
                ORDER BY id
                """
            )
            batch = 0
            for row in cur:
                chunk_ids.append(
                    row[0] if isinstance(row[0], UUID) else UUID(str(row[0]))
                )
                contents.append(row[1])
                emb_list.append(row[2])
                batch += 1
                if batch % 5000 == 0:
                    print(f"  loaded {batch:,}...")
        conn.commit()
    finally:
        conn.close()

    return chunk_ids, contents, np.array(emb_list, dtype=np.float32)


def write_report(
    *,
    topic_model: Any,
    n_docs: int,
    n_topics: int,
    n_noise: int,
    output_path: Path,
) -> None:
    """Emit a Markdown report sorted by topic size."""
    info = topic_model.get_topic_info().sort_values("Count", ascending=False)

    lines: list[str] = []
    lines.append("# BERTopic diagnostic")
    lines.append("")
    lines.append(f"Generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Run summary")
    lines.append("")
    lines.append(f"- Corpus size: **{n_docs:,}** chunks")
    lines.append(f"- Topics discovered: **{n_topics:,}**")
    lines.append(f"- Outlier chunks (topic -1): **{n_noise:,}** ({100 * n_noise / n_docs:.1f}%)")
    lines.append("")

    lines.append("## Fit parameters")
    lines.append("")
    lines.append("These are the parameters BERTopic ended up using after defaults")
    lines.append("were resolved. Compare against `LABEL_MIN_SIZE`, the HDBSCAN config,")
    lines.append("and UMAP config in `src/herald/cluster.py`.")
    lines.append("")

    sections = [
        ("UMAP (dimensionality reduction)", "umap_model"),
        ("HDBSCAN (clustering)", "hdbscan_model"),
        ("CountVectorizer (c-TF-IDF input)", "vectorizer_model"),
        ("ClassTfidfTransformer (c-TF-IDF weighting)", "ctfidf_model"),
    ]
    for heading, attr in sections:
        model = getattr(topic_model, attr, None)
        if model is None:
            continue
        try:
            params = model.get_params()
        except Exception:
            params = {"__repr__": repr(model)}
        lines.append(f"**{heading}** ({type(model).__name__})")
        lines.append("")
        lines.append("```python")
        for k, v in sorted(params.items()):
            lines.append(f"{k} = {v!r}")
        lines.append("```")
        lines.append("")

    lines.append("## Topics (sorted by size, largest first)")
    lines.append("")
    lines.append("Topic `-1` is BERTopic's outlier bucket — chunks that didn't fit any cluster.")
    lines.append("")

    for _, row in info.iterrows():
        topic_id = int(row["Topic"])
        size = int(row["Count"])
        try:
            words = topic_model.get_topic(topic_id) or []
        except Exception:
            words = []
        keywords = ", ".join(w for w, _score in words[:10]) or "(no keywords)"

        bucket_note = " — outlier bucket" if topic_id == -1 else ""
        lines.append(f"### Topic {topic_id} — n={size:,}{bucket_note}")
        lines.append("")
        lines.append(f"**Keywords:** {keywords}")
        lines.append("")

    output_path.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
