"""The graph-propagated staleness worklist via hive_health(include_stale_suspects=true):
detection-only surfacing of servable memories whose anchor sits in the blast radius of a
breaking/removed change (census ``--propagate`` evidence rows). Request-flag gated, no
config knob — byte-inert until requested; servable-episodes only; anchor-bucketed order;
ids/enums/hashes only (Law 4); fail-open to []. Resolution stays human (hive_supersede /
hive_prune) — the bucket proposes re-verification, it retires nothing."""
from __future__ import annotations

import json

from hive.domain.evidence_kinds import EK_STALE_SUSPECT
from tests.mcp._helpers import build_real_server, content, tool_call, write_text

D = 64


def _suspect_payload(seed="matrix/x.py::Seed", drift="removed", head_sha="b" * 40):
    return json.dumps({"schema": "stale_suspect/v1",
                       "matched": {"path": "pkg/caller.py", "symbol": "caller_fn"},
                       "seed": seed, "drift": drift,
                       "stamp": {"base_sha": "a" * 40, "head_sha": head_sha}},
                      sort_keys=True, separators=(",", ":"))


def _seed_suspect(server, clock, text, *, anchor, payload=None, ts=None):
    """One servable (established, via hive_write) episode + one stale_suspect ledger row."""
    eid = write_text(server, text, anchor=anchor)["id"]
    server.store.insert_audit(eid, EK_STALE_SUSPECT, "census",
                              int(ts if ts is not None else clock.now()),
                              payload or _suspect_payload())
    return eid


# ── byte-inert unless requested ─────────────────────────────────────────────────
def test_absent_unless_requested():
    server, clock = build_real_server(d=D)
    _seed_suspect(server, clock, "a suspected lesson", anchor="pkg/caller.py::caller_fn")
    snap = content(tool_call(server, "hive_health", {}))       # no flag
    assert "stale_suspects" not in snap


# ── the worklist: newest per id, ids-only fields, anchor-bucketed ───────────────
def test_surfaces_newest_suspect_per_servable_episode():
    server, clock = build_real_server(d=D)
    eid = _seed_suspect(server, clock, "a suspected lesson",
                        anchor="pkg/caller.py::caller_fn",
                        payload=_suspect_payload(seed="old.py::S"), ts=100)
    server.store.insert_audit(eid, EK_STALE_SUSPECT, "census", 200,
                              _suspect_payload(seed="new.py::S", drift="breaking"))
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    work = snap["stale_suspects"]
    assert len(work) == 1
    row = work[0]
    assert row == {"episode_id": eid, "anchor": "pkg/caller.py::caller_fn",
                   "seed": "new.py::S", "drift": "breaking",
                   "head_sha": "b" * 40, "ts": 200}             # newest wins, exact fields
    assert "a suspected lesson" not in json.dumps(snap)         # no memory text (Law 4)


def test_bucket_is_anchor_ordered_and_lists_multiple_episodes():
    server, clock = build_real_server(d=D)
    b = _seed_suspect(server, clock, "lesson two", anchor="zz/later.py::Z")
    a = _seed_suspect(server, clock, "lesson one", anchor="aa/early.py::A")
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    assert [r["episode_id"] for r in snap["stale_suspects"]] == [a, b]  # anchor-bucketed


def test_non_servable_episode_is_dropped_from_the_bucket():
    server, clock = build_real_server(d=D)
    servable = _seed_suspect(server, clock, "still servable",
                             anchor="pkg/caller.py::caller_fn")
    quarantined = content(tool_call(server, "hive_capture",
                                    {"text": "quarantined suspect",
                                     "anchor": "pkg/other.py::fn"}))["id"]
    server.store.insert_audit(quarantined, EK_STALE_SUSPECT, "census",
                              int(clock.now()), _suspect_payload())
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    ids = {r["episode_id"] for r in snap["stale_suspects"]}
    assert servable in ids and quarantined not in ids           # servable-only


def test_malformed_suspect_payload_is_omitted_never_a_crash():
    server, clock = build_real_server(d=D)
    _seed_suspect(server, clock, "broken ledger row",
                  anchor="pkg/caller.py::caller_fn", payload="not-json")
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    assert snap["ok"] is True
    assert snap["stale_suspects"] == []                         # under-claim, no half-shape


def test_never_suspected_store_returns_empty_list_when_requested():
    server, _ = build_real_server(d=D)
    write_text(server, "an ordinary memory", anchor="pkg/caller.py::caller_fn")
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    assert snap["stale_suspects"] == []


# ── fail-open: a probe fault degrades to [] (no half-shape, no 500) ─────────────
def test_fail_open_returns_empty_not_500():
    server, clock = build_real_server(d=D)
    _seed_suspect(server, clock, "one suspect", anchor="pkg/caller.py::caller_fn")

    def _boom(*a, **k):
        raise RuntimeError("suspect read exploded")
    server.store.stale_suspect_rows = _boom
    snap = content(tool_call(server, "hive_health", {"include_stale_suspects": True}))
    assert snap["ok"] is True                                   # health did NOT 500
    assert snap["stale_suspects"] == []                         # degraded to []
