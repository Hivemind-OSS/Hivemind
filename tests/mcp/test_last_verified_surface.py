"""The served verification-recency stamp at the MCP boundary.

Three contracts pinned here:
  1. STAMP RIDES WHEN VERIFIED — a hit whose episode carries a verify_current/
     verify_stale ledger row gains ``last_verified = {ts, sha, state}`` from the
     NEWEST row (derived ledger evidence enriched at the boundary; the kernel never
     judges churn — the edge compares the stamp to the code).
  2. BYTE-INERT WHEN ABSENT — a never-verified hit emits NO ``last_verified`` key
     (the exact-envelope golden lives in test_meta_surface; the key-absence pin here
     reds on an ``last_verified: null`` mutation).
  3. FAIL-OPEN SIDE-CHANNEL — a reader fault serves the envelope WITHOUT stamps;
     recall is never broken by the enrichment.
"""
from __future__ import annotations

import json

import numpy as np

from hive.app.contract import REMEDIATION_NOTICE
from hive.domain.evidence_kinds import EK_VERIFY_CURRENT, EK_VERIFY_STALE
from tests.fakes._fakes import FakeProvider
from tests.mcp._helpers import build_real_server, content, tool_call

_HEAD = "b" * 40


def _verify_payload(head_sha: str = _HEAD) -> str:
    return json.dumps({"schema": "verify/v1",
                       "matched": {"path": "a.py", "symbol": "F"},
                       "exists_after": False, "drift": "removed",
                       "stamp": {"base_sha": "a" * 40, "head_sha": head_sha,
                                 "combdrift": {"combdrift_version": "0.1.0"},
                                 "matrix_head": {"graph_sha256": "g" * 64,
                                                 "commit_sha": head_sha,
                                                 "engine_version": "0.1.0"}}},
                      sort_keys=True, separators=(",", ":"))


def _write_and_recall(server, text, *, plant=None):
    wr = content(tool_call(server, "hive_write", {"text": text}))
    if plant is not None:
        plant(int(wr["id"]))
    return content(tool_call(server, "hive_recall", {"query": text}))


def test_stamp_rides_the_hit_newest_wins():
    server, _ = build_real_server()

    def plant(eid: int) -> None:
        server.store.append_evidence([
            (eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload("1" * 40)),
            (eid, EK_VERIFY_STALE, "census", 200, _verify_payload("2" * 40)),
        ])

    env = _write_and_recall(server, "an anchored lesson that got re-verified",
                            plant=plant)
    hit = env["reference_context"][0]
    assert hit["last_verified"] == {"ts": 200, "sha": "2" * 40, "state": "stale"}


def test_never_verified_hit_has_no_key():
    server, _ = build_real_server()
    env = _write_and_recall(server, "a plain lesson nobody verified")
    hit = env["reference_context"][0]
    assert "last_verified" not in hit                # absent ⇒ NO key, never null


class _TwinProvider(FakeProvider):
    """Two 'flange' texts land at cos≈0.78 — above the 0.70 serve floor (one query
    confidently hits BOTH) yet below the 0.80 near-dup floor (select_served keeps
    both); everything else keeps the hash behavior."""

    def _vec(self, text: str) -> np.ndarray:
        if "flange" not in text:
            return super()._vec(text)
        v = np.zeros(self.d, dtype=np.float32)
        v[0] = 1.0
        if "gasket" in text:
            v[1] = 0.8
        return v / float(np.linalg.norm(v))


def test_mixed_hits_only_the_verified_one_carries_the_stamp():
    server, _ = build_real_server(embedder=_TwinProvider(d=64))
    verified = content(tool_call(server, "hive_write", {
        "text": "the flange gasket lesson"}))
    content(tool_call(server, "hive_write", {
        "text": "the flange rotation lesson"}))
    server.store.append_evidence([
        (int(verified["id"]), EK_VERIFY_CURRENT, "census", 100, _verify_payload())])
    env = content(tool_call(server, "hive_recall",
                            {"query": "the flange gasket lesson"}))
    by_id = {h["episode_id"]: h for h in env["reference_context"]}
    assert len(by_id) == 2                           # one envelope, both twins served
    assert by_id[int(verified["id"])]["last_verified"]["state"] == "current"
    others = [h for h in env["reference_context"]
              if h["episode_id"] != int(verified["id"])]
    assert others and all("last_verified" not in h for h in others)


def test_reader_fault_serves_the_envelope_without_stamps():
    server, _ = build_real_server()

    def plant(eid: int) -> None:
        server.store.append_evidence([
            (eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload())])
        def boom(ids):
            raise RuntimeError("ledger probe down")
        server.store.last_verification = boom        # the side-channel faults

    env = _write_and_recall(server, "a lesson served through a broken side-channel",
                            plant=plant)
    assert env["abstained"] is False                 # recall itself is unbroken
    assert all("last_verified" not in h for h in env["reference_context"])


# ── the server-side stale remediation rider (attaches on last_verified.state == "stale") ──
def test_stale_hit_carries_the_remediation_rider():
    """A hit the SERVER already knows is stale carries REMEDIATION_NOTICE on EVERY harness — the
    retire/outcome options reach an agent with zero client tooling, no hive-edge required."""
    server, _ = build_real_server()

    def plant(eid: int) -> None:
        server.store.append_evidence([
            (eid, EK_VERIFY_STALE, "census", 200, _verify_payload("2" * 40))])

    env = _write_and_recall(server, "an anchored lesson the code has moved past", plant=plant)
    hit = env["reference_context"][0]
    assert hit["last_verified"]["state"] == "stale"
    assert hit["remediation"] == REMEDIATION_NOTICE   # the options ride the stale hit verbatim


def test_current_hit_carries_no_remediation_rider():
    """The rider is STALE-ONLY: a current hit is not decorated with a retire-it menu (mutation 12:
    an unconditional attach reds here)."""
    server, _ = build_real_server()

    def plant(eid: int) -> None:
        server.store.append_evidence([
            (eid, EK_VERIFY_CURRENT, "census", 100, _verify_payload())])

    env = _write_and_recall(server, "an anchored lesson still current", plant=plant)
    hit = env["reference_context"][0]
    assert hit["last_verified"]["state"] == "current"
    assert "remediation" not in hit                   # byte-inert on a healthy hit


def test_never_verified_hit_carries_no_remediation_rider():
    """No stamp ⇒ no state ⇒ no rider (mutation 12: a never-attach reds the stale test above)."""
    server, _ = build_real_server()
    env = _write_and_recall(server, "a plain lesson nobody ever verified")
    hit = env["reference_context"][0]
    assert "remediation" not in hit


# ── canonical-ref rider scoping (HIVE_CENSUS__CANONICAL_REF → the recall rider) ──
def _reffed_payload(head_sha: str, ref: str | None) -> str:
    body = json.loads(_verify_payload(head_sha))
    if ref is not None:
        body["ref"] = ref
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _full_hit(server, text, rows):
    def plant(eid: int) -> None:
        server.store.append_evidence([(eid, kind, "census", ts, payload)
                                      for kind, ts, payload in rows])
    env = _write_and_recall(server, text, plant=plant)
    assert len(env["reference_context"]) == 1
    return env["reference_context"][0]


def test_recall_rider_scoped_to_canonical_ref_unset_is_byte_identical():
    # canonical_ref unset ("") ⇒ the rider derives from the NEWEST row regardless of
    # its ref — the FULL hit dict equals the pre-scoping build's (a foreign-ref newest
    # row still riders when unscoped).
    text = "an anchored lesson verified on a feature line"
    rows = [(EK_VERIFY_STALE, 200, _reffed_payload("2" * 40, "feat-y"))]
    unset_server, _ = build_real_server()
    scoped_off_server, _ = build_real_server(canonical_ref="")
    hit_unset = _full_hit(unset_server, text, rows)
    hit_off = _full_hit(scoped_off_server, text, rows)
    assert hit_unset == hit_off                       # "" == the unscoped default build
    assert hit_unset["last_verified"] == {"ts": 200, "sha": "2" * 40, "state": "stale"}
    assert hit_unset["remediation"] == REMEDIATION_NOTICE


def test_recall_rider_scoped_newest_foreign_falls_back_to_older_canonical():
    # canonical_ref="master": the newest verify_stale from feat-y is SKIPPED; the older
    # master verify_current answers — the hit reads current, carries NO remediation.
    text = "an anchored lesson stale only on a feature line"
    rows = [(EK_VERIFY_CURRENT, 100, _reffed_payload("1" * 40, "master")),
            (EK_VERIFY_STALE, 200, _reffed_payload("2" * 40, "feat-y"))]
    server, _ = build_real_server(canonical_ref="master")
    hit = _full_hit(server, text, rows)
    assert hit["last_verified"] == {"ts": 100, "sha": "1" * 40, "state": "current"}
    assert "remediation" not in hit
    # the unscoped control on the SAME rows reads the foreign-newest stale + rider
    control, _ = build_real_server()
    control_hit = _full_hit(control, text, rows)
    assert control_hit["last_verified"]["state"] == "stale"
    assert control_hit["remediation"] == REMEDIATION_NOTICE


def test_recall_rider_scoped_canonical_row_still_riders():
    text = "an anchored lesson stale on the canonical line"
    rows = [(EK_VERIFY_STALE, 200, _reffed_payload("2" * 40, "master"))]
    server, _ = build_real_server(canonical_ref="master")
    hit = _full_hit(server, text, rows)
    assert hit["last_verified"] == {"ts": 200, "sha": "2" * 40, "state": "stale"}
    assert hit["remediation"] == REMEDIATION_NOTICE


def test_recall_rider_scoped_legacy_refless_row_still_riders():
    # the ABSENCE RULE: a pre-stamp (ref-less) ledger row still answers under a set
    # canonical_ref — scoping filters foreign refs, never the legacy corpus.
    text = "an anchored lesson verified before the ref stamp existed"
    rows = [(EK_VERIFY_STALE, 200, _reffed_payload("2" * 40, None))]
    server, _ = build_real_server(canonical_ref="master")
    hit = _full_hit(server, text, rows)
    assert hit["last_verified"] == {"ts": 200, "sha": "2" * 40, "state": "stale"}
    assert hit["remediation"] == REMEDIATION_NOTICE


def test_recall_rider_scoped_no_answerable_row_means_no_rider():
    # every row foreign ⇒ nothing answers ⇒ the hit carries NO last_verified and NO
    # remediation (absent keys, never null — byte-inert).
    text = "an anchored lesson verified only on feature lines"
    rows = [(EK_VERIFY_STALE, 100, _reffed_payload("1" * 40, "feat-a")),
            (EK_VERIFY_STALE, 200, _reffed_payload("2" * 40, "feat-b"))]
    server, _ = build_real_server(canonical_ref="master")
    hit = _full_hit(server, text, rows)
    assert "last_verified" not in hit and "remediation" not in hit


# ── v3: no beacon — the enrichment-heavy recall envelope carries no version key ──
def test_recall_envelope_carries_no_contract_version_beacon():
    """The per-result contract-version beacon is DELETED in v3 (the contract is served
    fresh at every connect via initialize instructions). The enrichment-heavy recall
    envelope this file owns must not carry it either (the all-verbs sweep lives in
    test_tool_surface)."""
    server, _ = build_real_server()
    env = _write_and_recall(server, "a lesson served without any version beacon")
    assert "contract_version" not in env
