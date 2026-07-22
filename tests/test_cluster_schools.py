"""Tests for the schools topic-map clustering + export."""

from __future__ import annotations

import asyncio
import datetime as _dt
import types

import numpy as np

from herald import cluster_schools
from herald.cluster_schools import (
    ChunkRow,
    ClusterParams,
    SweepResult,
    build_export,
    build_hierarchy,
    label_clusters,
    leaf_centroids,
    load_chunks,
    render_sweep,
    representative_indices,
    run_clustering,
    sweep_clustering,
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


def test_build_export_topic_bubbles():
    rows = [
        _row(0, "peekskill", [1, 0]),
        _row(1, "peekskill", [1, 0]),
        _row(2, "ossining", [0, 1], doc_type=None),   # noise
    ]
    labels = np.array([0, 0, -1])
    leaf_ids = [0]
    topic_xy = np.array([[0.25, 0.75]], dtype=np.float32)
    out = build_export(rows, labels, leaf_ids, topic_xy, {0: "Cell phone policy"},
                       rep_idx={0: [0]})
    assert out["n_points"] == 3 and out["n_clusters"] == 1 and out["n_noise"] == 1
    assert out["districts"] == ["ossining", "peekskill"]
    assert out["doc_types"] == ["other", "policy"]
    (c,) = out["clusters"]
    assert c["id"] == 0 and c["label"] == "Cell phone policy" and c["size"] == 2
    assert c["x"] == 0.25 and c["y"] == 0.75
    assert c["dist"] == [0, 2]                 # ossining=0, peekskill=2 (noise excluded)
    assert "Consent Agenda" in c["tip"]
    assert c["theme"] == -1 and c["mid"] == -1  # no hierarchy passed


def test_run_clustering_end_to_end_synthetic():
    # two well-separated blobs in 8 dims -> HDBSCAN finds topics, no API key
    rng = np.random.default_rng(7)
    a = rng.normal(0, 0.02, (30, 8)) + np.eye(8)[0]
    b = rng.normal(0, 0.02, (30, 8)) + np.eye(8)[1]
    rows = [
        _row(i, "peekskill" if i < 30 else "ossining", v)
        for i, v in enumerate(np.vstack([a, b]))
    ]
    params = ClusterParams(min_cluster_size=5, min_samples=2, umap_neighbors=5, cluster_dims=4)
    out = run_clustering(rows, params, api_key=None)
    assert out["n_points"] == 60
    assert out["n_clusters"] >= 1
    # one bubble per topic: aligned fields, positions in range, sizes sum sanely
    for c in out["clusters"]:
        assert c["label"].startswith("Topic ")            # unlabeled fallback
        assert 0.0 <= c["x"] <= 1.0 and 0.0 <= c["y"] <= 1.0
        assert len(c["dist"]) == len(out["districts"])
        assert c["size"] == sum(c["dist"])
    assert sum(c["size"] for c in out["clusters"]) == out["n_points"] - out["n_noise"]

    # explicit `embeddings=` override is honored (content-only path)
    out2 = run_clustering(rows, params, embeddings=np.vstack([r.embedding for r in rows]),
                          api_key=None)
    assert out2["n_points"] == 60


def test_sweep_clustering_grid_and_render():
    # two well-separated blobs -> the grid runs each cell and reports metrics
    rng = np.random.default_rng(3)
    a = rng.normal(0, 0.02, (40, 8)) + np.eye(8)[0]
    b = rng.normal(0, 0.02, (40, 8)) + np.eye(8)[1]
    emb = np.vstack([a, b]).astype(np.float32)

    results = sweep_clustering(
        emb, dims_list=[4], mcs_list=[5, 10], min_samples=2, umap_neighbors=5,
    )
    assert len(results) == 2                      # one cell per (dims x mcs)
    for r in results:
        assert r.cluster_dims == 4
        assert r.min_cluster_size in (5, 10)
        assert r.n_clusters >= 1
        assert 0.0 <= r.noise_pct <= 100.0
        assert r.median_size >= 0

    table = render_sweep(results)
    assert "# Clustering sweep" in table
    assert "min_cluster_size" in table
    assert table.count("\n|") >= 3                # header rule + >=2 data rows


def test_render_sweep_tolerates_nan_dbcv():
    r = render_sweep([SweepResult(10, 15, 3, 12.0, float("nan"), 40)])
    assert "nan" in r.lower()                      # doesn't crash on NaN DBCV


def test_build_hierarchy_nests_and_cuts():
    # six leaf centroids in two well-separated families of three
    fam_a = np.array([[1, 0, 0], [0.98, 0.05, 0], [0.97, 0, 0.05]], dtype=np.float32)
    fam_b = np.array([[0, 1, 0], [0.05, 0.98, 0], [0, 0.97, 0.05]], dtype=np.float32)
    cents = np.vstack([fam_a, fam_b])
    leaf_ids = [10, 11, 12, 20, 21, 22]

    tiers = build_hierarchy(leaf_ids, cents, targets=[2, 4])
    assert [t["target"] for t in tiers] == [2, 4]        # broadest (2) first
    # the k=2 cut recovers the two families
    coarse = tiers[0]["groups"]
    assert len(coarse) == 2
    members = sorted(sorted(v) for v in coarse.values())
    assert members == [[10, 11, 12], [20, 21, 22]]
    # every leaf appears exactly once in each tier
    for tier in tiers:
        flat = [lid for leaves in tier["groups"].values() for lid in leaves]
        assert sorted(flat) == leaf_ids

    # degenerate: targets >= n_leaves (the leaf tier itself) are dropped
    assert build_hierarchy(leaf_ids, cents, targets=[6, 99]) == []
    # too few leaves -> no hierarchy
    assert build_hierarchy([1, 2], cents[:2], targets=[2]) == []


def test_leaf_centroids_normalized():
    emb = np.array([[3, 0], [3, 0], [0, 5], [0, 5]], dtype=np.float32)
    labels = np.array([0, 0, 1, 1])
    ids, cents = leaf_centroids(emb, labels)
    assert ids == [0, 1]
    assert np.allclose(np.linalg.norm(cents, axis=1), 1.0)


def test_run_clustering_with_hierarchy_export_shape():
    # three separated blobs -> >=3 leaves so the hierarchy can form a tier
    rng = np.random.default_rng(11)
    blobs = [rng.normal(0, 0.015, (25, 8)) + np.eye(8)[i] for i in range(3)]
    rows = [_row(i, "peekskill", v) for i, v in enumerate(np.vstack(blobs))]
    params = ClusterParams(min_cluster_size=5, min_samples=2, umap_neighbors=5, cluster_dims=4)
    out = run_clustering(rows, params, api_key=None, hierarchy_targets=[2])
    if out["n_clusters"] >= 3:                       # hierarchy needs >=3 leaves
        assert "hierarchy" in out
        tier = out["hierarchy"][0]
        assert tier["level"] == 0 and tier["target"] == 2
        # tier cluster sizes sum to the clustered (non-noise) points
        assert sum(c["size"] for c in tier["clusters"]) == out["n_points"] - out["n_noise"]
        leaf_ids = {c["id"] for c in out["clusters"]}
        for c in tier["clusters"]:
            assert set(c["leaves"]) <= leaf_ids
            assert c["label"].startswith("Group ")
            assert 0.0 <= c["x"] <= 1.0 and 0.0 <= c["y"] <= 1.0   # tier bubble position
        # leaves carry their theme parent (tier 0)
        theme_ids = {c["id"] for c in tier["clusters"]}
        for leaf in out["clusters"]:
            assert leaf["theme"] in theme_ids


def test_label_clusters_uses_real_teardown(monkeypatch):
    # regression: teardown must call the method AsyncAnthropic actually has
    # (close, not aclose) and must not discard labels if teardown raises.
    calls = {"closed": 0}

    class FakeMsg:
        def __init__(self):
            self.content = [types.SimpleNamespace(text="Cell phone policy")]

    class FakeMessages:
        async def create(self, **kw):
            return FakeMsg()

    class FakeClient:
        def __init__(self, **kw):
            self.messages = FakeMessages()
        async def close(self):
            calls["closed"] += 1
            raise RuntimeError("boom")             # teardown blows up

    fake = types.ModuleType("anthropic")
    fake.AsyncAnthropic = FakeClient
    monkeypatch.setitem(__import__("sys").modules, "anthropic", fake)

    labels = asyncio.run(label_clusters("k", {0: ["a passage"], 1: ["another"]}))
    assert labels == {0: "Cell phone policy", 1: "Cell phone policy"}
    assert calls["closed"] == 1                     # close() was awaited despite raising


def test_haiku_model_id_is_current():
    assert cluster_schools.HAIKU_MODEL == "claude-haiku-4-5-20251001"


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
