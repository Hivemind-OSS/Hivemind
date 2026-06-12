"""CV3 — the contested-memory report: miss clusters whose representative sits
within ``contested_tau`` of a SERVABLE row group by that row (the mechanical
supersession-review queue). One index probe per CLUSTER, never per miss; the
vector-less refused bucket is never probed; below-tau clusters are excluded."""
from __future__ import annotations

import numpy as np

from hive.app.gaps import cluster_misses, contested_misses
from tests.fakes._fakes import make_episode

D = 8


def _unit(*xs) -> np.ndarray:
    v = np.array(list(xs) + [0.0] * (D - len(xs)), dtype=np.float32)
    return v / np.linalg.norm(v)


U = _unit(1.0)                          # the contested direction
W = _unit(0.0, 1.0)                     # a second, far direction


def _miss(vec, miss_type="abstained", ts=100, text="how do we rotate the key"):
    return {"query_text": text, "vector": vec, "miss_type": miss_type, "ts": ts}


def _rows_near_u(n_abstained=6, n_no_match=1):
    rows = [_miss(U, "abstained", ts=100 + i) for i in range(n_abstained)]
    rows += [_miss(U, "no_match", ts=200 + i) for i in range(n_no_match)]
    return rows


class _SpyIndex:
    """Records every probe; serves a fixed top-1 by direction."""

    def __init__(self, by_dir):
        self.by_dir = by_dir            # list of (direction, (eid, sim))
        self.calls = 0

    def search(self, vec, k):
        self.calls += 1
        for direction, hit in self.by_dir:
            if float(np.dot(vec, direction)) > 0.9:
                return [hit]
        return []


def _get_episode(eid):
    return make_episode(eid, f"servable {eid}", trust="established")


def test_contested_groups_misses_by_servable_neighbor():
    index = _SpyIndex([(U, (41, 0.9))])
    out = contested_misses(_rows_near_u(), tau=0.75, contested_tau=0.80,
                           search=index.search, get_episode=_get_episode)
    assert out == [{"episode_id": 41, "trust": "established", "miss_count": 7,
                    "miss_types": {"abstained": 6, "no_match": 1},
                    "last_seen": 200}]


def test_contested_one_probe_per_cluster_never_per_miss():
    # 7 misses near U + 2 near W ⇒ 2 clusters ⇒ exactly 2 probes
    index = _SpyIndex([(U, (41, 0.9)), (W, (52, 0.85))])
    rows = _rows_near_u() + [_miss(W, "abstained", ts=300, text="other ask"),
                             _miss(W, "abstained", ts=301, text="other ask")]
    out = contested_misses(rows, tau=0.75, contested_tau=0.80,
                           search=index.search, get_episode=_get_episode)
    assert index.calls == 2
    assert [c["episode_id"] for c in out] == [41, 52]         # miss_count desc


def test_contested_below_tau_excluded_and_refused_never_probed():
    index = _SpyIndex([(U, (41, 0.79))])                      # just under 0.80
    rows = _rows_near_u() + [_miss(None, "secret_refused", ts=400, text="")]
    out = contested_misses(rows, tau=0.75, contested_tau=0.80,
                           search=index.search, get_episode=_get_episode)
    assert out == []
    assert index.calls == 1                                   # refused bucket skipped
    # the INCLUSIVE boundary: sim == contested_tau marks contested
    index_eq = _SpyIndex([(U, (41, 0.80))])
    out_eq = contested_misses(_rows_near_u(), tau=0.75, contested_tau=0.80,
                              search=index_eq.search, get_episode=_get_episode)
    assert out_eq and out_eq[0]["episode_id"] == 41


def test_cluster_misses_report_unchanged():
    # regression: the existing gaps report keeps its shape (no _vec leak, the
    # refused aggregate still present and visible)
    rows = _rows_near_u(2, 0) + [_miss(None, "secret_refused", ts=400, text="")]
    out = cluster_misses(rows, tau=0.75)
    assert all("_vec" not in c for c in out)
    assert {c["miss_count"] for c in out} == {2, 1}
    refused = [c for c in out if c["representative_query"] == ""][0]
    assert refused["miss_types"] == {"secret_refused": 1}


def test_health_envelope_carries_contested(monkeypatch):
    # the handler wiring: include_gaps=true surfaces contested + the fixed
    # interpretation note when the report is non-empty
    from tests.mcp._helpers import build_real_server, content, tool_call
    server, _clock = build_real_server()
    entry = {"episode_id": 41, "trust": "established", "miss_count": 7,
             "miss_types": {"abstained": 7}, "last_seen": 99}
    monkeypatch.setattr(server, "_contested_report", lambda: [entry])
    snap = content(tool_call(server, "hive_health", {"include_gaps": True}))
    assert snap["contested"] == [entry]
    assert "hive_write(replaces=" in snap["contested_note"]
    snap_plain = content(tool_call(server, "hive_health", {}))
    assert "contested" not in snap_plain                      # report-time only
