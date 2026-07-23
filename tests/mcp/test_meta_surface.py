"""The `meta` map carrier at the MCP boundary (M06, v3 envelopes).

Four contracts pinned here:
  1. BYTE-INERT WHEN ABSENT — with no meta anywhere, the capture/write/recall
     envelopes are byte-identical to the v3 golden (exact serialized bytes, key set
     AND order): the carrier is invisible until a producer exists. (The goldens also
     pin the v3 shape itself: write carries trust+deduped, hits carry
     repos/anchors/drift, and NO contract_version beacon exists anywhere.)
  2. OMIT-WHEN-EMPTY — a served hit gains a ``meta`` key IFF the stored carrier is
     non-empty; it rides verbatim (the canonical serialized string, never re-parsed).
  3. REFUSE-ONLY SECRET FLOOR — a credential in a meta VALUE refuses the whole
     write/capture (0 rows) and is NEVER redacted (a masked handle is garbage),
     even under a scanner whose text disposition is REDACT.
  4. CLEAN REFUSAL ON BAD GRAMMAR — a malformed map is a refusal envelope (or a
     schema-belt reject for a non-object), never a crash; and meta never enters
     the embedder (label-blind recall).
"""
from __future__ import annotations

import json

import numpy as np

from hive.domain.secret_scan import REDACT
from tests.fakes._fakes import FakeProvider, FakeScanner
from tests.mcp._helpers import build_real_server, content, is_error, tool_call

_CARRIER = '{"combdrift/fp":"combdrift-fp/1:deadbeef"}'
_META_ARG = {"combdrift/fp": "combdrift-fp/1:deadbeef"}
_CRED = "AKIAABCDEFGHIJKLMNOP"                       # fires the aws_akia rule


def _raw_text(resp) -> str:
    """The exact serialized content bytes the agent sees."""
    return resp.result["content"][0]["text"]


# ── 1. byte-inert when absent (the v3 envelope, byte-for-byte) ────────────────
def test_capture_and_write_envelopes_byte_identical_when_meta_absent():
    server, _ = build_real_server()
    cap = tool_call(server, "hive_capture", {"text": "the widget frobnicates"})
    expected_cap = json.dumps({
        "status": "quarantined", "id": 1,
        "scan": {"action": "clean", "rules": [], "n_findings": 0},
        "deduped": False})
    assert _raw_text(cap) == expected_cap

    wr = tool_call(server, "hive_write", {"text": "the gadget rotates the flange"})
    expected_wr = json.dumps({
        "status": "approved", "id": 2, "trust": "provisional",
        "scan": {"action": "clean", "rules": [], "n_findings": 0},
        "deduped": False})
    assert _raw_text(wr) == expected_wr

    rc = tool_call(server, "hive_recall", {"query": "the gadget rotates the flange"})
    got = json.loads(_raw_text(rc))
    expected_rc = json.dumps({
        "reference_context": [{
            "episode_id": 2, "text": "the gadget rotates the flange",
            # sim rides through float32 cosine — take the served value, pin the shape
            "sim": got["reference_context"][0]["sim"],
            "trust": "provisional", "ts": 1000, "polarity": "neutral",
            "kind": "note", "repos": [], "anchors": [],
            "drift": {"type": "n/a", "detail": {"per_anchor": []}}}],
        "abstained": False,
        "trace_id": got["trace_id"],                 # per-call join key
        "state": "CONFIDENT", "top_cos": got["top_cos"]})
    assert _raw_text(rc) == expected_rc              # no meta key ANYWHERE, no beacon


# ── 2. omit-when-empty: meta rides IFF set, verbatim ──────────────────────────
def test_recall_hit_carries_meta_only_when_set():
    server, _ = build_real_server()
    tool_call(server, "hive_write", {"text": "lesson with a fingerprint",
                                     "meta": _META_ARG})
    tool_call(server, "hive_write", {"text": "plain lesson, no fingerprint"})
    with_meta = content(tool_call(
        server, "hive_recall", {"query": "lesson with a fingerprint"}))
    hit = with_meta["reference_context"][0]
    assert hit["meta"] == _CARRIER                   # the canonical string, verbatim
    without = content(tool_call(
        server, "hive_recall", {"query": "plain lesson, no fingerprint"}))
    assert "meta" not in without["reference_context"][0]


# ── 3. refuse-only secret floor on meta values ────────────────────────────────
def test_meta_value_is_secret_scanned_refuse_only():
    server, _ = build_real_server()
    for verb in ("hive_capture", "hive_write"):
        res = content(tool_call(server, verb,
                                {"text": "clean text", "meta": {"tool/key": _CRED}}))
        assert res["status"] == "refused"
        assert "aws_akia" in res["scan"]["rules"]
        assert server.store.counts() == (0, 0)       # 0 rows — nothing staged


def test_meta_credential_refused_even_under_redact_mode_scanner():
    # Under a REDACT-disposition scanner, credential TEXT is masked-and-staged —
    # but a credential in a meta VALUE still REFUSES: meta is never redacted.
    server, _ = build_real_server(scanner=FakeScanner(mode=REDACT))
    ctl = content(tool_call(server, "hive_write", {"text": f"the token is {_CRED}"}))
    assert ctl["status"] == "redacted"               # control: text path redacts
    res = content(tool_call(server, "hive_write", {
        "text": "clean text", "meta": {"tool/key": _CRED}}))
    assert res["status"] == "refused"
    assert server.store.counts() == (1, 0)           # only the redacted control row


# ── 4. clean refusal on bad grammar; recall stays meta-blind ──────────────────
def test_bad_meta_is_clean_refusal():
    server, _ = build_real_server()
    # a non-object is stopped by the schema belt BEFORE any port (clean error)
    belt = tool_call(server, "hive_capture", {"text": "t", "meta": ["x"]})
    assert is_error(belt)
    assert "meta" in belt.result["content"][0]["text"]
    # an object violating the grammar is a refusal ENVELOPE (never a crash)
    for bad in ({"BadKey": "v"}, {"a/b": 1}):
        res = content(tool_call(server, "hive_capture", {"text": "t", "meta": bad}))
        assert res["status"] == "refused"
        assert res["reason"].startswith("bad meta")
    assert server.store.counts() == (0, 0)


class _SpyProvider(FakeProvider):
    """Records every text the embedder sees — meta must never be among them."""
    def __init__(self, d: int = 64) -> None:
        super().__init__(d=d)
        self.seen: list[str] = []

    def encode(self, text: str) -> np.ndarray:
        self.seen.append(text)
        return super().encode(text)


def test_recall_is_meta_blind():
    # same text served from a meta-bearing and a meta-less store must score
    # identically, and the embedder must never see the carrier bytes.
    spy = _SpyProvider(d=64)
    with_meta, _ = build_real_server(embedder=spy)
    plain, _ = build_real_server()
    text = "an anchored lesson about the flange rotation"
    tool_call(with_meta, "hive_write", {"text": text, "meta": _META_ARG})
    tool_call(plain, "hive_write", {"text": text})
    a = content(tool_call(with_meta, "hive_recall", {"query": text}))
    b = content(tool_call(plain, "hive_recall", {"query": text}))
    ha, hb = a["reference_context"][0], b["reference_context"][0]
    assert (ha["sim"], a["top_cos"]) == (hb["sim"], b["top_cos"])
    assert all("combdrift" not in t for t in spy.seen)   # meta never embedded
