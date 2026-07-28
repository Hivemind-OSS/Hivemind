"""Record the REAL server envelopes the TypeScript harness parses, and gate them.

The harness reads one server shape — the MCP tool result — and every claim its
tests make about that shape is only worth what the shape is really like. So the
fixture is not hand-authored JSON: each entry is the literal
``{content:[{type,text}], isError}`` a real ``HiveMCPServer`` returned over a
real temp store, driven through the same substrate ``tests/contract/conftest.py``
already provides.

This module is both the recorder and the gate. A moved key, a renamed status, a
dropped rider — anything that changes what the server actually emits — shows up
here as a diff against the committed fixture, which is the Python-tier half of
the polyglot pin (the constants module is the other half).

Only ``trace_id`` is normalized: it is a fresh uuid per call and carries no
shape. Everything else is deterministic under the fake embedder and fake clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from tests.contract.conftest import (
    BASE_TIP,
    call,
    drift_put,
    ident,
    make_rig,
    register_repo,
    set_canonical_tip,
)

FIXTURE = (
    Path(__file__).resolve().parent.parent.parent
    / "harnesses"
    / "test"
    / "fixtures"
    / "envelopes.json"
)

REPO = "alpha"
ANCHOR_A = "app.py::greet"
ANCHOR_B = "billing.py::charge"
TIP = "a" * 40
DIM = 32
TRACE_PLACEHOLDER = "trace-recorded"

# The angle every recorded episode sits at from the shared query direction. Its
# square is the pairwise cosine between two of them, so 0.86 puts each hit above
# the serve floor (0.70) and each pair below the near-dup floor (0.80) — a
# multi-hit envelope that is genuinely multi-hit rather than one survivor of the
# decorrelation pass.
CONE_COS = 0.86


class ConeProvider:
    """A deterministic provider that places every ``cone=<n>``-tagged text on a
    fixed-angle cone around one shared direction. Untagged text keeps its own
    hash vector. The existing fakes cannot express this: hash vectors are
    orthogonal (nothing co-serves) and cluster vectors are near-identical (the
    decorrelation pass collapses them to one)."""

    name = "fake-cone"

    def __init__(self, d: int = DIM, cos_theta: float = CONE_COS) -> None:
        self.d = int(d)
        self.loaded = True
        self._c = float(cos_theta)

    def load(self) -> "ConeProvider":
        self.loaded = True
        return self

    def _hashvec(self, text: str) -> np.ndarray:
        rng = np.random.default_rng(
            int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
        )
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return (v / n) if n else v

    def _vec(self, text: str) -> np.ndarray:
        m = re.search(r"cone=(\w+)", text)
        if m is None:
            return self._hashvec(text)
        axis = np.zeros(self.d, dtype=np.float32)
        axis[0] = 1.0
        if m.group(1) == "q":
            return axis
        n = int(m.group(1))
        side = np.zeros(self.d, dtype=np.float32)
        side[1 + (n % (self.d - 1))] = 1.0
        v = self._c * axis + float(np.sqrt(1.0 - self._c * self._c)) * side
        return (v / float(np.linalg.norm(v))).astype(np.float32)

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


QUERY = "cone=q what does the caller pass"


def _result(resp: Any) -> dict[str, Any]:
    """The literal MCP tool result, with the per-call trace id normalized."""
    assert resp.error is None, f"the recorder drove a JSON-RPC error: {resp.error}"
    out = json.loads(json.dumps(resp.result))
    for item in out.get("content", []):
        text = item.get("text")
        if not isinstance(text, str):
            continue
        try:
            env = json.loads(text)
        except ValueError:
            continue
        if isinstance(env, dict) and "trace_id" in env:
            env["trace_id"] = TRACE_PLACEHOLDER
            item["text"] = json.dumps(env)
    return out


def _cone_rig(tmp_path, name: str):
    root = tmp_path / name
    root.mkdir(parents=True, exist_ok=True)
    return make_rig(root, embedder=ConeProvider(), db_name="hive.db")


def _anchored_rig(
    tmp_path, name: str, *, verdict: str | None, refs: list[str] | None = None
):
    """One anchored memory whose materialized drift verdict is ``verdict``
    (``None`` records nothing, which is the un-materialized ``unverifiable``)."""
    rig = _cone_rig(tmp_path, name)
    register_repo(
        rig.store, REPO, "https://example.invalid/alpha.git", canonical_ref="main"
    )
    set_canonical_tip(rig.store, REPO, TIP)
    args: dict[str, Any] = {
        "text": "cone=1 greet() must stay single-arg; callers pass positionally",
        "anchors": [{"repo": REPO, "anchor": ANCHOR_A}],
    }
    if refs is not None:
        args["repos"] = refs
    env = json.loads(
        _result(call(rig.server, "hive_write", args, identity=ident("writer")))[
            "content"
        ][0]["text"]
    )
    assert env.get("status") == "approved", env
    if verdict is not None:
        drift_put(
            rig.store,
            [
                (
                    REPO,
                    TIP,
                    BASE_TIP,
                    ANCHOR_A,
                    verdict,
                    json.dumps({"per_anchor": []}),
                    1,
                )
            ],
        )
    return rig


def _recall(rig, *, repos: list[str] | None = None) -> dict[str, Any]:
    args: dict[str, Any] = {"query": QUERY}
    if repos is not None:
        args["repos"] = repos
    return _result(call(rig.server, "hive_recall", args, identity=ident("reader")))


def record(tmp_path) -> dict[str, dict[str, Any]]:
    """Drive the real server once per scenario and collect its literal results."""
    out: dict[str, dict[str, Any]] = {
        "recall": {},
        "write": {},
        "capture": {},
        "supersede": {},
        "prune": {},
        "flag": {},
        "outcome": {},
    }

    # ── recall: a confident multi-hit envelope ────────────────────────────────
    rig = _cone_rig(tmp_path, "multi")
    for n in (1, 2):
        call(
            rig.server,
            "hive_write",
            {"text": f"cone={n} the {n}th caller passes the row, never the id"},
            identity=ident("writer"),
        )
    out["recall"]["confident_multi"] = _recall(rig)

    # ── recall: an abstain (nothing stored clears the absolute floor) ─────────
    rig = _cone_rig(tmp_path, "abstain")
    call(
        rig.server,
        "hive_write",
        {"text": "an unrelated fact about the backup retention window"},
        identity=ident("writer"),
    )
    out["recall"]["abstained"] = _recall(rig)

    # ── recall: one envelope per drift verdict ────────────────────────────────
    for label, verdict in (
        ("drift_fresh", "fresh"),
        ("drift_anchor_missing", "anchor_missing"),
        ("drift_anchor_changed", "anchor_changed"),
        ("drift_blast_radius_changed", "blast_radius_changed"),
    ):
        out["recall"][label] = _recall(_anchored_rig(tmp_path, label, verdict=verdict))

    # unverifiable: an anchored memory whose anchor was never materialized
    out["recall"]["drift_unverifiable"] = _recall(
        _anchored_rig(tmp_path, "unverifiable", verdict=None)
    )

    # branch_scoped: a stale-tier verdict read off a line the memory never declared
    branch = _anchored_rig(
        tmp_path, "branch", verdict="anchor_missing", refs=[f"{REPO}@feature"]
    )
    out["recall"]["drift_branch_scoped"] = _recall(branch, repos=[f"{REPO}@main"])

    # n/a: a general memory has nothing to verify
    rig = _cone_rig(tmp_path, "general")
    call(
        rig.server,
        "hive_write",
        {"text": "cone=1 a general lesson that binds to no repo or path"},
        identity=ident("writer"),
    )
    out["recall"]["drift_na"] = _recall(rig)

    # ── recall: two actionable ids in one envelope (the partial-close case) ───
    rig = _cone_rig(tmp_path, "multi_actionable")
    register_repo(
        rig.store, REPO, "https://example.invalid/alpha.git", canonical_ref="main"
    )
    set_canonical_tip(rig.store, REPO, TIP)
    for n, anchor in ((1, ANCHOR_A), (2, ANCHOR_B)):
        call(
            rig.server,
            "hive_write",
            {
                "text": f"cone={n} binding {n} that the tree has since moved",
                "anchors": [{"repo": REPO, "anchor": anchor}],
            },
            identity=ident("writer"),
        )
    drift_put(
        rig.store,
        [
            (
                REPO,
                TIP,
                BASE_TIP,
                anchor,
                "anchor_missing",
                json.dumps({"per_anchor": []}),
                1,
            )
            for anchor in (ANCHOR_A, ANCHOR_B)
        ],
    )
    out["recall"]["multi_actionable"] = _recall(rig)

    # ── recall: a surfaced conflict (near-dup pair, opposing polarity) ────────
    from tests.fakes._fakes import FakeClusterProvider

    (tmp_path / "conflicts").mkdir(parents=True, exist_ok=True)
    rig = make_rig(tmp_path / "conflicts", embedder=FakeClusterProvider(d=DIM))
    call(
        rig.server,
        "hive_write",
        {"text": "cid=3 always resolve the tip before probing", "polarity": "do"},
        identity=ident("writer-one"),
    )
    call(
        rig.server,
        "hive_write",
        {"text": "cid=3 never resolve the tip before probing", "polarity": "dont"},
        identity=ident("writer-two"),
    )
    out["recall"]["conflicts"] = _result(
        call(
            rig.server,
            "hive_recall",
            {"query": "cid=3 resolving the tip before probing"},
            identity=ident("reader"),
        )
    )

    # ── stores ────────────────────────────────────────────────────────────────
    rig = _cone_rig(tmp_path, "stores")
    register_repo(
        rig.store, REPO, "https://example.invalid/alpha.git", canonical_ref="main"
    )
    out["write"]["approved"] = _result(
        call(
            rig.server,
            "hive_write",
            {"text": "a lesson with no carrier at all"},
            identity=ident("writer"),
        )
    )
    out["write"]["anchored"] = _result(
        call(
            rig.server,
            "hive_write",
            {
                "text": "a lesson bound to a line of code",
                "anchors": [{"repo": REPO, "anchor": ANCHOR_A}],
            },
            identity=ident("writer"),
        )
    )
    out["write"]["repos_only"] = _result(
        call(
            rig.server,
            "hive_write",
            {"text": "a lesson scoped to a repo", "repos": [REPO]},
            identity=ident("writer"),
        )
    )
    out["write"]["refused"] = _result(
        call(
            rig.server,
            "hive_write",
            {
                "text": "the deploy key is sk-ABCDEFGHIJKLMNOPQRSTUVWX12345678 keep it safe"
            },
            identity=ident("writer"),
        )
    )
    out["capture"]["approved"] = _result(
        call(
            rig.server,
            "hive_capture",
            {"text": "an ambiguous observation"},
            identity=ident("writer"),
        )
    )
    out["outcome"]["ok"] = _result(
        call(
            rig.server,
            "hive_outcome",
            {"helped": [], "hurt": []},
            identity=ident("reader"),
        )
    )
    out["flag"]["recorded"] = _result(
        call(
            rig.server,
            "hive_flag",
            {"a": 1, "b": 2, "kind": "conflict"},
            identity=ident("reader"),
        )
    )

    # ── maintenance: an affirmed and a benign-noop envelope for each verb ─────
    out["prune"]["noop"] = _result(
        call(rig.server, "hive_prune", {"episode_id": 1}, identity=ident("reader"))
    )
    out["supersede"]["noop"] = _result(
        call(
            rig.server,
            "hive_supersede",
            {"loser": 1, "winner": 2},
            identity=ident("reader"),
        )
    )

    # An affirmed retirement needs a qualifying machine signal: a moved anchor on
    # the memory's own declared line is the one the server verifies.
    retire = _anchored_rig(tmp_path, "retire", verdict="anchor_missing")
    out["prune"]["affirmed"] = _result(
        call(retire.server, "hive_prune", {"episode_id": 1}, identity=ident("reader"))
    )

    sup = _anchored_rig(tmp_path, "supersede", verdict="anchor_missing")
    winner = json.loads(
        _result(
            call(
                sup.server,
                "hive_write",
                {"text": "cone=2 the successor lesson"},
                identity=ident("writer"),
            )
        )["content"][0]["text"]
    )["id"]
    out["supersede"]["affirmed"] = _result(
        call(
            sup.server,
            "hive_supersede",
            {"loser": 1, "winner": winner},
            identity=ident("reader"),
        )
    )

    rep = _anchored_rig(tmp_path, "replaces", verdict="anchor_missing")
    out["write"]["replaces_affirmed"] = _result(
        call(
            rep.server,
            "hive_write",
            {"text": "cone=2 the successor, written in one call", "replaces": 1},
            identity=ident("writer"),
        )
    )
    return out


# ── the gate ──────────────────────────────────────────────────────────────────


def _committed() -> dict[str, Any]:
    if not FIXTURE.exists():
        return {}
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _serialize(recorded: dict[str, Any]) -> str:
    doc = {
        "_generated_by": "tests/harness/test_envelope_fixture.py — real "
        "HiveMCPServer results over a real temp store; DO NOT HAND-EDIT",
        **recorded,
    }
    return json.dumps(doc, indent=2, sort_keys=True) + "\n"


def _env(result: dict[str, Any]) -> dict[str, Any]:
    return json.loads(result["content"][0]["text"])


def _served(result: dict[str, Any]) -> list[int]:
    return [h["episode_id"] for h in _env(result).get("reference_context", [])]


def test_the_committed_fixture_matches_a_fresh_recording(tmp_path) -> None:
    """The gate: a moved key, a renamed status or a dropped rider reds here.

    On drift the fresh recording is written so the diff is reviewable in git;
    the test still fails, so a drift can never pass silently."""
    fresh = _serialize(record(tmp_path))
    if FIXTURE.exists() and FIXTURE.read_text(encoding="utf-8") == fresh:
        return
    FIXTURE.parent.mkdir(parents=True, exist_ok=True)
    FIXTURE.write_text(fresh, encoding="utf-8")
    raise AssertionError(
        f"{FIXTURE} did not match what the server emits — it has been re-recorded. "
        "Review the diff (the harness parses this shape) and re-run."
    )


def test_the_recall_scenarios_carry_the_shapes_the_harness_parses() -> None:
    fx = _committed()
    assert fx, "the fixture has not been recorded yet"
    recall = fx["recall"]

    assert len(_served(recall["confident_multi"])) >= 2, "multi-hit must be multi"
    assert _env(recall["confident_multi"])["abstained"] is False

    assert _env(recall["abstained"])["abstained"] is True
    assert _served(recall["abstained"]) == []

    for label, verdict in (
        ("drift_fresh", "fresh"),
        ("drift_anchor_missing", "anchor_missing"),
        ("drift_anchor_changed", "anchor_changed"),
        ("drift_blast_radius_changed", "blast_radius_changed"),
        ("drift_unverifiable", "unverifiable"),
        ("drift_branch_scoped", "branch_scoped"),
        ("drift_na", "n/a"),
    ):
        hits = _env(recall[label])["reference_context"]
        assert hits, f"{label} must serve a hit"
        assert {h["drift"]["type"] for h in hits} == {verdict}, label

    # the server attaches its stale rider exactly on the qualifying tier
    for label in (
        "drift_anchor_missing",
        "drift_anchor_changed",
        "drift_blast_radius_changed",
    ):
        assert all(
            "remediation" in h for h in _env(recall[label])["reference_context"]
        ), label
    for label in (
        "drift_fresh",
        "drift_unverifiable",
        "drift_branch_scoped",
        "drift_na",
    ):
        assert all(
            "remediation" not in h for h in _env(recall[label])["reference_context"]
        ), label

    actionable = _served(recall["multi_actionable"])
    assert len(actionable) >= 2, "the partial-close case needs two actionable ids"
    assert all(
        h["drift"]["type"] == "anchor_missing"
        for h in _env(recall["multi_actionable"])["reference_context"]
    )

    conflicts = _env(recall["conflicts"])
    assert conflicts.get("conflicts"), (
        "conflict detection ships ON, so the default envelope must carry the key"
    )
    ids = {n["a_id"] for n in conflicts["conflicts"]} | {
        n["b_id"] for n in conflicts["conflicts"]
    }
    assert ids & set(_served(recall["conflicts"])), (
        "at least one conflicted id must also be served, or no served id is actionable"
    )


def test_the_store_and_maintenance_scenarios_carry_their_statuses() -> None:
    fx = _committed()
    assert fx, "the fixture has not been recorded yet"
    assert _env(fx["write"]["approved"])["status"] == "approved"
    assert _env(fx["write"]["refused"])["status"] == "refused"
    assert _env(fx["write"]["refused"]).get("scan", {}).get("findings"), (
        "a refusal names its rule and span"
    )
    # a landed capture is not an approval — the two store verbs land in different
    # states, and the harness must credit both as "a store landed".
    assert _env(fx["capture"]["approved"])["status"] == "quarantined"

    assert _env(fx["prune"]["affirmed"])["status"] == "pruned"
    assert _env(fx["prune"]["noop"])["status"] == "noop"
    assert _env(fx["supersede"]["affirmed"])["status"] == "superseded"
    assert _env(fx["supersede"]["noop"])["status"] == "noop"
    assert _env(fx["flag"]["recorded"])["status"] == "flagged"

    replaces = _env(fx["write"]["replaces_affirmed"])
    assert replaces["status"] == "approved"
    assert isinstance(replaces.get("superseded"), int), (
        "an affirmed replaces= rider reports its retirement in the same envelope"
    )


def test_no_scenario_carries_an_unclassified_status() -> None:
    from scripts.gen_harness_constants import (
        AFFIRMATIVE_STATUS,
        NON_AFFIRMATIVE_STATUS,
        OTHER_STATUS,
    )

    known = set(AFFIRMATIVE_STATUS) | set(NON_AFFIRMATIVE_STATUS) | set(OTHER_STATUS)
    fx = _committed()
    for group, scenarios in fx.items():
        if group.startswith("_"):
            continue
        for label, result in scenarios.items():
            status = _env(result).get("status")
            if status is None:
                continue
            assert status in known, (
                f"{group}.{label} carries an unknown status {status!r}"
            )
