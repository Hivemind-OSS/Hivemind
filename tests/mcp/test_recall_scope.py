"""The v3 recall partition + served-hit shape at the MCP boundary (CT-3/CT-4 twins).

``hive_recall {query, repos?, anchor_prefix?}``: the handler parses the scope strings
(``name`` / ``name@branch``) through ``normalize_repos``, builds a ``RecallScope``, and
threads it into the pipeline; an unregistered name is a clean ``refused`` envelope
(never a tool error). Every served hit carries ``repos`` / ``anchors`` / ``drift``
(§3.4) — drift read from the materialized cache at the queried ref's tip else the
repo's canonical tip, attached fail-open by ``attach_drift``; a queried non-canonical
branch degrades to ``unverifiable`` and records demand in ``ref_requests``.
"""

from __future__ import annotations

import json
import math
import re

import numpy as np

from tests.fakes._fakes import FakeProvider
from tests.mcp._helpers import (
    build_real_server,
    content,
    is_error,
    register_repo,
    tool_call,
    write_text,
)

TIP = "a" * 40
Q = "qax how does the shared retry helper behave"

_A = 0.75  # query↔memory cosine: clears tau_serve 0.70; pairwise a² ≈ 0.56 < dup_tau


class AxisProvider(FakeProvider):
    """Controlled partition geometry: the ``qax`` query is the pure e0 axis; a memory
    tagged ``ax=<i>`` reads ``0.75·e0 + residual·e_i`` — every tagged memory clears
    ``tau_serve`` (0.70) against the query while any PAIR sits at cos ≈ 0.5625, below
    ``dup_tau`` (0.80), so the decorrelated serve keeps them ALL (the recall-scope
    unit-test idiom — near-dup collapse is proven elsewhere). Untagged text keeps the
    hash behavior (its own orthogonal singleton)."""

    def _vec(self, text: str) -> np.ndarray:
        if "qax" in text:
            v = np.zeros(self.d, dtype=np.float32)
            v[0] = 1.0
            return v
        m = re.search(r"ax=(\d+)", text)
        if m is None:
            return super()._vec(text)
        v = np.zeros(self.d, dtype=np.float32)
        v[0] = _A
        v[1 + int(m.group(1)) % (self.d - 1)] = math.sqrt(1.0 - _A * _A)
        return v / float(np.linalg.norm(v))


def _recall(server, query, **args):
    return content(tool_call(server, "hive_recall", {"query": query, **args}))


def _served(env) -> set:
    return {h["episode_id"] for h in env.get("reference_context", [])}


def _cluster_rig():
    server, clock = build_real_server(embedder=AxisProvider(d=64))
    for name in ("alpha", "beta", "gamma"):
        register_repo(server, name)
    ids = {
        "a": write_text(
            server,
            "ax=1 alpha: the retry helper doubles its backoff",
            anchors=[{"repo": "alpha", "anchor": "hive/app/retry.py::backoff"}],
        )["id"],
        "a2": write_text(
            server,
            "ax=2 alpha: the retry helper caps at five attempts",
            anchors=[{"repo": "alpha", "anchor": "hive/domain/policy.py::cap"}],
        )["id"],
        "b": write_text(
            server, "ax=3 beta: the retry helper jitters its delay", repos=["beta"]
        )["id"],
        "g": write_text(server, "ax=4 general: retries must be idempotent")["id"],
        "ab": write_text(
            server,
            "ax=5 shared: the retry helper is vendored in both",
            repos=["alpha", "beta"],
        )["id"],
    }
    return server, clock, ids


# ── the partition (CT-3 twins) ─────────────────────────────────────────────────
def test_scope_hit_cross_repo_exclusion_and_general_always_in():
    server, _, ids = _cluster_rig()
    env = _recall(server, Q, repos=["alpha"])
    got = _served(env)
    assert ids["a"] in got  # in-scope anchored memory serves
    assert ids["b"] not in got  # beta-only excluded from alpha scope
    assert ids["g"] in got  # general is ALWAYS in scope
    assert ids["ab"] in got  # multi-repo serves from either scope


def test_omitted_repos_is_global():
    server, _, ids = _cluster_rig()
    got = _served(_recall(server, Q))
    assert {ids["a"], ids["b"], ids["g"], ids["ab"]} <= got


def test_unknown_repo_name_is_clean_refusal():
    server, _, ids = _cluster_rig()
    resp = tool_call(server, "hive_recall", {"query": Q, "repos": ["ghost"]})
    env = content(resp)
    assert env.get("status") == "refused"
    assert not is_error(resp), "a grammar refusal is a clean envelope, not a tool error"


def test_empty_repos_list_is_global_not_refused():
    server, _, ids = _cluster_rig()
    env = _recall(server, Q, repos=[])
    assert "reference_context" in env  # advertised ⇒ accepted (CT-11 twin)
    assert ids["g"] in _served(env)


def test_anchor_prefix_narrows_anchored_hits_only():
    server, _, ids = _cluster_rig()
    env = _recall(server, Q, repos=["alpha"], anchor_prefix="hive/app")
    got = _served(env)
    assert ids["a"] in got  # anchor under the prefix kept
    assert ids["a2"] not in got  # every anchor outside ⇒ filtered
    assert ids["g"] in got  # general kept (prefix narrows anchored)


def test_scoped_weak_field_abstains():
    server, _, ids = _cluster_rig()
    # gamma has NOTHING servable and no ax= general matches gamma-only: an empty
    # scoped partition (minus the general rows, excluded by an off-axis query)
    # must abstain — a strong OUT-OF-SCOPE match never rescues the field.
    env = _recall(server, "an off-distribution cooking question", repos=["gamma"])
    assert env["abstained"] is True
    assert _served(env) == set()


# ── the served hit shape (CT-4 twins) ──────────────────────────────────────────
def _drift_rig():
    server, clock = build_real_server()
    register_repo(server, "alpha", canonical_ref="main")
    server.store.meta_set("sync:alpha:last_tip", TIP)
    return server, clock


def test_every_hit_carries_repos_anchors_drift():
    server, _ = _drift_rig()
    a = write_text(
        server,
        "anchored fact one",
        anchors=[{"repo": "alpha", "anchor": "pkg/one.py::fn"}],
    )["id"]
    g = write_text(server, "general fact two")["id"]
    for text, eid in (("anchored fact one", a), ("general fact two", g)):
        env = _recall(server, text)
        hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
        for key in ("repos", "anchors", "drift"):
            assert key in hit, f"served hit lacks {key!r}: {hit}"
        assert json.dumps(hit["drift"])  # json-serializable wire object


def test_materialized_verdict_rides_the_hit():
    server, _ = _drift_rig()
    eid = write_text(
        server,
        "fact about the changed anchor",
        anchors=[{"repo": "alpha", "anchor": "pkg/mod.py::fn"}],
    )["id"]
    server.store.drift_put(
        [("alpha", TIP, "pkg/mod.py::fn", "anchor_changed", "{}", 5)]
    )
    env = _recall(server, "fact about the changed anchor")
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    drift = hit["drift"]
    assert drift["type"] == "anchor_changed"
    per = drift["detail"]["per_anchor"]
    assert per[0]["repo"] == "alpha" and per[0]["anchor"] == "pkg/mod.py::fn"
    assert per[0]["verdict"] == "anchor_changed"
    assert hit["repos"] == ["alpha"]
    assert hit["anchors"] == [{"repo": "alpha", "anchor": "pkg/mod.py::fn"}]


def test_unmaterialized_anchor_reads_unverifiable_general_reads_na():
    server, _ = _drift_rig()
    cold = write_text(
        server,
        "fact whose tip was never materialized",
        anchors=[{"repo": "alpha", "anchor": "pkg/cold.py::fn"}],
    )["id"]
    env = _recall(server, "fact whose tip was never materialized")
    hit = next(h for h in env["reference_context"] if h["episode_id"] == cold)
    assert hit["drift"]["type"] == "unverifiable"

    g = write_text(server, "a general memory with no repo and no anchor")["id"]
    env = _recall(server, "a general memory with no repo and no anchor")
    hit = next(h for h in env["reference_context"] if h["episode_id"] == g)
    assert hit["drift"]["type"] == "n/a" and hit["repos"] == []


def test_name_at_branch_routes_ref_and_records_request():
    server, _ = _drift_rig()
    eid = write_text(
        server,
        "fact judged against a feature branch",
        anchors=[{"repo": "alpha", "anchor": "pkg/feat.py::fn"}],
    )["id"]
    server.store.drift_put([("alpha", TIP, "pkg/feat.py::fn", "fresh", "{}", 5)])
    env = _recall(
        server, "fact judged against a feature branch", repos=["alpha@feature"]
    )
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    drift = hit["drift"]
    assert drift["type"] == "unverifiable", (
        "an unmaterialized branch tip never serves the canonical verdict"
    )
    assert drift["detail"].get("ref") == "feature"
    rows = [
        dict(r) for r in server.store.conn.execute("SELECT repo, ref FROM ref_requests")
    ]
    assert {"repo": "alpha", "ref": "feature"} in rows, (
        "a name@branch recall must touch ref_requests (demand-driven coverage)"
    )


def test_drift_reader_fault_degrades_not_breaks(monkeypatch):
    server, _ = _drift_rig()
    eid = write_text(
        server,
        "fact that must survive a drift-cache fault",
        anchors=[{"repo": "alpha", "anchor": "pkg/faulty.py::fn"}],
    )["id"]
    server.store.drift_put([("alpha", TIP, "pkg/faulty.py::fn", "fresh", "{}", 5)])

    def boom(*a, **kw):
        raise RuntimeError("drift cache exploded")

    monkeypatch.setattr(server.store, "drift_get", boom, raising=False)
    env = _recall(server, "fact that must survive a drift-cache fault")
    hit = next(h for h in env["reference_context"] if h["episode_id"] == eid)
    assert hit["drift"]["type"] == "unverifiable"  # degraded, read unbroken
