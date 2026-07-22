"""Tests for the schools topic-map clustering + export."""

from __future__ import annotations

import datetime as _dt

import numpy as np

from herald.cluster_schools import (
    ChunkRow,
    ClusterParams,
    build_export,
    load_chunks,
    representative_indices,
    run_clustering,
)


def _row(i: int, district: str, vec, *, doc_type: str = "policy") -> ChunkRow:
    return ChunkRow(
        chunk_id=f"c{i}", district=district, meeting_date=_dt.date(2026, 3, 17),
        doc_type=doc_type, section_type="Consent Agenda", heading=f"Item {i}",
        content=f"Passage {i} about school governance.",
        embedding=np.asarray(vec, dtype=np.float32),
    )


def test_representative_indices_picks_nearest_centroid():
    # cluster 0 around (1,0), cluster 1 around (0,1); one straggler each
    emb = np.array([[1, 0], [0.9, 0.1], [0.5, 0.5], [0, 1], [0.1, 0.9]], dtype=np.float32)
    labels = np.array([0, 0, 0, 1, 1])
    reps = representative_indices(emb, labels, per_cluster=2)
    assert set(reps) == {0, 1}
    assert 2 not in reps[0]          # straggler is least representative
    assert len(reps[0]) == 2 and len(reps[1]) == 2


def test_build_export_columnar_shape():
    rows = [
        _row(0, "peekskill", [1, 0]),
        _row(1, "ossining", [0, 1], doc_type=None),
    ]
    labels = np.array([0, -1])
    xy = np.array([[0.25, 0.75], [0.5, 0.5]], dtype=np.float32)
    out = build_export(rows, labels, xy, {0: "Cell phone policy"})
    assert out["n_points"] == 2 and out["n_clusters"] == 1 and out["n_noise"] == 1
    assert out["districts"] == ["ossining", "peekskill"]
    assert out["doc_types"] == ["other", "policy"]
    assert out["clusters"] == [{"id": 0, "label": "Cell phone policy", "size": 1}]
    # columnar arrays all aligned
    for key in ("x", "y", "cluster", "district", "doc_type", "month", "tip"):
        assert len(out[key]) == 2
    assert out["cluster"] == [0, -1]
    assert out["district"] == [1, 0]          # peekskill, ossining -> indices
    assert out["month"] == ["2026-03", "2026-03"]
    assert "Consent Agenda" in out["tip"][0]


def test_run_clustering_end_to_end_synthetic():
    # two well-separated blobs in 8 dims -> HDBSCAN finds 2 topics, no API key
    rng = np.random.default_rng(7)
    a = rng.normal(0, 0.02, (30, 8)) + np.eye(8)[0]
    b = rng.normal(0, 0.02, (30, 8)) + np.eye(8)[1]
    rows = [
        _row(i, "peekskill" if i < 30 else "ossining", v)
        for i, v in enumerate(np.vstack([a, b]))
    ]
    params = ClusterParams(min_cluster_size=5, min_samples=2, umap_neighbors=5)
    out = run_clustering(rows, params, api_key=None)
    assert out["n_points"] == 60
    assert out["n_clusters"] == 2
    # unlabeled fallback names
    assert all(c["label"].startswith("Topic ") for c in out["clusters"])
    assert all(0.0 <= v <= 1.0 for v in out["x"] + out["y"])


class FakeCursor:
    def __init__(self, rows):
        self.calls = []
        self._rows = rows

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self._rows


def test_load_chunks_sql_and_sampling():
    row = ("id1", "peekskill", _dt.date(2026, 1, 5), "policy", "Reports",
           "Heading", "Content here", [0.0, 1.0])
    cur = FakeCursor([row])
    out = load_chunks(cur, sample=500)
    sql, params = cur.calls[0]
    assert "c.status = 'active'" in sql and "embedding is not null" in sql
    assert "order by random() limit" in sql and params["sample"] == 500
    assert out[0].district == "peekskill" and out[0].embedding.dtype == np.float32

    cur2 = FakeCursor([row])
    load_chunks(cur2)
    assert "limit" not in cur2.calls[0][0]
