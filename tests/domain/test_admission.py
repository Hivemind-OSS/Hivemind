"""M05 AdmissionService — the scan → stage → embed → complete gauntlet, driven
against a duck-typed in-memory store (the real SQLite adapter is exercised by the
contract suite; this set proves the DOMAIN policy).

v3 (thin-agent): there is NO approver anywhere — ``write`` lands
``trust='provisional'`` (servable now, labeled), ``capture`` lands
``trust='quarantined'`` (embedded, structurally unservable). The deterministic
secret scan stays the ONE non-bypassable gate (refuse = 0 rows, redact = masked
text only, meta refuse-only), anchors + repo scope thread to ``stage`` in the same
tx, ``replaces`` validates its target before anything is staged, and a write that
dedups onto a QUARANTINED row LIFTS it to provisional WITH its audit row."""

from __future__ import annotations

import json
import logging

import numpy as np
import pytest

from hive.domain.admission import AdmissionService, WriteResult
from hive.domain.errors import SecretRefused
from hive.domain.lifecycle import DEPRECATED, ESTABLISHED, PROVISIONAL, QUARANTINED
from hive.domain.models import AnchorRef, Episode, content_hash
from hive.domain.secret_scan import REDACT, REFUSE
from tests.fakes import FakeProvider
from tests.fakes._fakes import FakeScanner

DIM = 8
SECRET = "AKIAIOSFODNN7EXAMPLE"
NOW = 1_000


class _CountingProvider(FakeProvider):
    """FakeProvider that counts encodes — the no-re-embed-on-dedup pin."""

    def __init__(self, d: int = DIM) -> None:
        super().__init__(d=d)
        self.encodes = 0

    def encode(self, text: str) -> np.ndarray:
        self.encodes += 1
        return super().encode(text)


class _CountingScanner(FakeScanner):
    """FakeScanner that counts scans — the disabled-before-scan pin."""

    def __init__(self, mode: str = REFUSE) -> None:
        super().__init__(mode)
        self.scans = 0

    def scan(self, text: str):
        self.scans += 1
        return super().scan(text)


class _Store:
    """The v3 store slice AdmissionService drives, in memory + recording: staged
    rows dedup on content_hash; ``get_episode`` projects the frozen carrier the
    way the real adapter's read does (anchors → AnchorRef, repos = anchor repos ∪
    declared scope, canonicalized)."""

    def __init__(self) -> None:
        self.rows: dict[int, dict] = {}
        self.anchors: dict[int, list[tuple[str, str]]] = {}
        self.scopes: dict[int, list[str]] = {}
        self.by_hash: dict[str, int] = {}
        self.audits: list[tuple] = []
        self.stage_calls: list[dict] = []
        self.complete_calls: list[dict] = []
        self.supersede_calls: list[tuple] = []
        self.rejected: list[int] = []
        self.fail_set_trust = False
        self.fail_complete = False
        self._next = 100

    def stage(
        self, *, text, weight, proposed_by, ts, polarity, kind, meta, anchors, repos
    ):
        self.stage_calls.append(
            dict(
                text=text,
                weight=weight,
                proposed_by=proposed_by,
                ts=ts,
                polarity=polarity,
                kind=kind,
                meta=meta,
                anchors=list(anchors),
                repos=list(repos),
            )
        )
        h = content_hash(text)
        if h in self.by_hash:
            return self.by_hash[h], True
        self._next += 1
        eid = self._next
        self.rows[eid] = dict(
            text=text,
            weight=float(weight),
            ts=int(ts),
            proposed_by=proposed_by,
            status="pending",
            trust=QUARANTINED,
            value=None,
            version=0,
            last_active_ts=0,
            polarity=polarity,
            kind=kind,
            meta=meta,
            superseded_by=None,
        )
        self.anchors[eid] = [(r, a) for r, a in anchors]
        self.scopes[eid] = list(repos)
        self.by_hash[h] = eid
        return eid, False

    def get_episode(self, episode_id):
        row = self.rows.get(int(episode_id))
        if row is None:
            return None
        eid = int(episode_id)
        anchor_refs = tuple(
            AnchorRef(repo=r, anchor=a) for r, a in self.anchors.get(eid, ())
        )
        repos = tuple(
            sorted({*self.scopes.get(eid, ()), *(a.repo for a in anchor_refs)})
        )
        return Episode(
            id=eid,
            tenant_id="default",
            text=row["text"],
            weight=row["weight"],
            ts=row["ts"],
            content_hash=content_hash(row["text"]),
            status=row["status"],
            proposed_by=row["proposed_by"],
            value=row["value"],
            version=row["version"],
            trust=row["trust"],
            superseded_by=row["superseded_by"],
            last_active_ts=row["last_active_ts"],
            polarity=row["polarity"],
            kind=row["kind"],
            meta=row["meta"],
            repos=repos,
            anchors=anchor_refs,
        )

    def complete(self, episode_id, value, *, expected_version, trust, last_active_ts):
        self.complete_calls.append(
            dict(
                episode_id=int(episode_id),
                trust=trust,
                expected_version=expected_version,
                last_active_ts=int(last_active_ts),
            )
        )
        row = self.rows.get(int(episode_id))
        if row is None or row["version"] != expected_version or self.fail_complete:
            return False
        row.update(
            status="approved",
            trust=trust,
            last_active_ts=int(last_active_ts),
            value=np.asarray(value, dtype=np.float32),
        )
        return True

    def reject(self, episode_id):
        row = self.rows.pop(int(episode_id), None)
        if row is not None:
            self.rejected.append(int(episode_id))
            self.by_hash.pop(content_hash(row["text"]), None)

    def set_trust(self, episode_id, new_trust, *, now):
        if self.fail_set_trust:
            return False
        row = self.rows.get(int(episode_id))
        if row is None:
            return False
        row["trust"] = new_trust
        row["last_active_ts"] = int(now)
        return True

    def insert_audit(self, episode_id, kind, actor, ts, payload):
        self.audits.append((int(episode_id), kind, actor, int(ts), payload))
        return len(self.audits)

    def supersede(self, target_id, replacement_id, *, actor, ts):
        self.supersede_calls.append(
            (int(target_id), int(replacement_id), actor, int(ts))
        )
        row = self.rows.get(int(target_id))
        if (
            row is None
            or int(target_id) == int(replacement_id)
            or row["trust"] == DEPRECATED
        ):
            return False
        row["trust"] = DEPRECATED
        row["superseded_by"] = int(replacement_id)
        return True


class _RecordingLifecycle:
    """The trigger seam capture drives: the synchronous promotion check + the
    decay-sweep piggyback (the real service is fail-open inside)."""

    def __init__(self) -> None:
        self.captures: list[int] = []
        self.sweeps = 0

    def on_capture(self, episode_id):
        self.captures.append(int(episode_id))
        return None

    def sweep(self):
        self.sweeps += 1
        return {}


def _svc(mode: str = REFUSE, *, lifecycle=None, autonomy_enabled: bool = True):
    store = _Store()
    provider = _CountingProvider()
    scanner = _CountingScanner(mode)
    svc = AdmissionService(
        store,
        scanner,
        provider,
        now=lambda: NOW,
        lifecycle=lifecycle,
        autonomy_enabled=autonomy_enabled,
    )
    return svc, store, provider, scanner


# ═══ v3 signature pins: no approver anywhere, capture has no retirement power ═══
def test_write_has_no_approved_by_parameter():
    svc, *_ = _svc()
    with pytest.raises(TypeError):
        svc.write("a fact", proposed_by="a", approved_by="human")


def test_capture_has_no_replaces_parameter():
    # a quarantined capture must never gain retirement power
    svc, *_ = _svc()
    with pytest.raises(TypeError):
        svc.capture("a fact", proposed_by="a", replaces=7)


# ── write lands PROVISIONAL, servable now, in one call ─────────────────────────
def test_write_lands_provisional_materialized():
    svc, store, provider, _ = _svc()
    r = svc.write("vector index rebuild on boot", proposed_by="agent-1")
    assert isinstance(r, WriteResult)
    assert r.status == "approved" and r.deduped is False and r.superseded is None
    ep = store.get_episode(r.episode_id)
    assert ep.status == "approved" and ep.trust == PROVISIONAL
    assert ep.value is not None and ep.last_active_ts == NOW
    assert r.content_hash == content_hash("vector index rebuild on boot")
    # complete carried the trust label in the SAME call (no separate approve step)
    assert store.complete_calls[-1]["trust"] == PROVISIONAL
    assert provider.encodes == 1


def test_capture_lands_quarantined_unservable_label():
    svc, store, *_ = _svc()
    r = svc.capture("an unvouched but maybe useful insight", proposed_by="agent-1")
    assert r.status == "quarantined" and r.deduped is False
    ep = store.get_episode(r.episode_id)
    assert ep.status == "approved" and ep.trust == QUARANTINED  # materialized, unserved
    assert ep.value is not None
    assert store.complete_calls[-1]["trust"] == QUARANTINED


# ── anchors + repos thread to stage in the same tx ─────────────────────────────
def test_write_threads_anchors_and_repos_to_stage():
    svc, store, *_ = _svc()
    svc.write(
        "the greet helper trims input",
        proposed_by="a",
        anchors=[AnchorRef(repo="alpha", anchor="app.py::greet")],
        repos=["beta"],
    )
    staged = store.stage_calls[-1]
    assert staged["anchors"] == [("alpha", "app.py::greet")]  # (repo, anchor) pairs
    assert staged["repos"] == ["beta"]  # declared scope-only


def test_capture_threads_anchors_and_repos_to_stage():
    svc, store, *_ = _svc()
    svc.capture(
        "the loader caches per repo",
        proposed_by="a",
        anchors=[AnchorRef(repo="alpha", anchor="pkg/loader.py")],
        repos=["alpha"],
    )
    staged = store.stage_calls[-1]
    assert staged["anchors"] == [("alpha", "pkg/loader.py")]
    assert staged["repos"] == ["alpha"]


def test_write_threads_polarity_kind_meta_to_stage():
    svc, store, *_ = _svc()
    svc.write(
        "never inline credentials",
        proposed_by="a",
        polarity="dont",
        kind="convention",
        meta='{"v":1}',
    )
    staged = store.stage_calls[-1]
    assert (staged["polarity"], staged["kind"], staged["meta"]) == (
        "dont",
        "convention",
        '{"v":1}',
    )


# ── #5a: the secret floor — refuse is 0 rows, redact stores masked text only ───
def test_write_refuses_on_secret_zero_rows():
    svc, store, *_ = _svc()
    with pytest.raises(SecretRefused):
        svc.write(f"my key {SECRET}", proposed_by="a")
    assert store.stage_calls == []  # scan fires BEFORE any stage
    assert store.rows == {}


def test_capture_secret_floor_unmoved():
    svc, store, *_ = _svc()
    with pytest.raises(SecretRefused):
        svc.capture(f"my key {SECRET}", proposed_by="agent-1")
    assert store.stage_calls == [] and store.rows == {}


def test_redact_stores_masked_text_and_lands_provisional():
    svc, store, *_ = _svc(mode=REDACT)
    r = svc.write(f"connect with {SECRET} please", proposed_by="a")
    assert r.status == "redacted" and r.scan.action == REDACT
    ep = store.get_episode(r.episode_id)
    assert SECRET not in ep.text and "[REDACTED]" in ep.text
    assert r.content_hash == content_hash(ep.text)  # hash over masked text
    assert ep.trust == PROVISIONAL  # redacted is servable too


def test_meta_gate_refuses_secret_bearing_meta_even_in_redact_mode():
    # meta is refuse-ONLY (a masked handle is garbage), checked BEFORE the text
    # gauntlet — nothing is staged, on write AND capture.
    for verb in ("write", "capture"):
        svc, store, *_ = _svc(mode=REDACT)
        with pytest.raises(SecretRefused):
            getattr(svc, verb)(
                "clean text", proposed_by="a", meta=f'{{"token":"{SECRET}"}}'
            )
        assert store.stage_calls == [] and store.rows == {}


# ── dedup: same text → same row; servable/deprecated idempotent ────────────────
def test_write_dedup_same_text_no_reembed():
    svc, store, provider, _ = _svc()
    a = svc.write("same insight text", proposed_by="a")
    b = svc.write("same insight text", proposed_by="b")
    assert a.episode_id == b.episode_id and b.deduped is True
    assert len(store.rows) == 1
    assert provider.encodes == 1  # the dedup path never re-embeds


def test_write_dedup_onto_established_never_demotes():
    svc, store, *_ = _svc()
    r = svc.write("an established team fact", proposed_by="a")
    store.rows[r.episode_id]["trust"] = ESTABLISHED
    again = svc.write("an established team fact", proposed_by="b")
    assert again.deduped is True and again.episode_id == r.episode_id
    assert store.get_episode(r.episode_id).trust == ESTABLISHED  # no demotion
    assert store.audits == []  # no lift audit


def test_write_dedup_onto_deprecated_does_not_revive():
    svc, store, *_ = _svc()
    old = svc.write("the port is 5432", proposed_by="a")
    new = svc.write(
        "the port is 6543 since the migration", proposed_by="a", replaces=old.episode_id
    )
    assert new.superseded == old.episode_id
    again = svc.write("the port is 5432", proposed_by="b")  # dedups onto the dead row
    assert again.deduped is True and again.episode_id == old.episode_id
    assert store.get_episode(old.episode_id).trust == DEPRECATED  # NOT revived


# ── the dedup-onto-quarantined LIFT (v3): promote in place + audit ─────────────
def test_write_dedup_onto_quarantined_lifts_with_audit():
    # THE named-mutation twin (CT-1 audit assertion): a write whose text already
    # sits quarantined LIFTS that row to provisional in place AND lands the
    # transition's audit row — dropping the insert_audit is the mutation that
    # reds here.
    svc, store, provider, _ = _svc()
    cap = svc.capture("a fact first seen autonomously", proposed_by="agent-1")
    assert store.get_episode(cap.episode_id).trust == QUARANTINED
    w = svc.write("a fact first seen autonomously", proposed_by="agent-2")
    assert w.deduped is True and w.episode_id == cap.episode_id
    ep = store.get_episode(cap.episode_id)
    assert ep.trust == PROVISIONAL  # lifted in place…
    assert provider.encodes == 1  # …without a re-embed
    eid, kind, actor, ts, payload = store.audits[-1]
    assert (eid, kind, actor, ts) == (cap.episode_id, "promote", "server", NOW)
    body = json.loads(payload)
    assert body["rule"] == "write_lift" and body["from"] == QUARANTINED


def test_lift_failure_raises_loud():
    svc, store, *_ = _svc()
    svc.capture("a fact first seen autonomously", proposed_by="agent-1")
    store.fail_set_trust = True  # lost-update race on the lift
    with pytest.raises(RuntimeError, match="lift-on-dedup"):
        svc.write("a fact first seen autonomously", proposed_by="agent-2")


# ── the replaces rider: validate-first, benign-refused, applied-after-land ─────
def test_write_replaces_supersedes_existing_target():
    svc, store, *_ = _svc()
    old = svc.write("the port is 5432", proposed_by="a")
    new = svc.write(
        "the port is 6543 since the migration", proposed_by="a", replaces=old.episode_id
    )
    assert new.superseded == old.episode_id
    dead = store.get_episode(old.episode_id)
    assert dead.trust == DEPRECATED and dead.superseded_by == new.episode_id
    assert store.supersede_calls[-1][:2] == (old.episode_id, new.episode_id)


def test_write_replaces_unknown_target_fails_whole_call():
    svc, store, *_ = _svc()
    with pytest.raises(ValueError, match="does not exist"):
        svc.write("a correction aimed at nothing", proposed_by="a", replaces=424242)
    assert store.stage_calls == [] and store.rows == {}  # nothing stored


def test_write_replaces_self_dedup_is_benign_noop():
    svc, store, *_ = _svc()
    old = svc.write("exactly this text", proposed_by="a")
    r = svc.write("exactly this text", proposed_by="a", replaces=old.episode_id)
    assert r.deduped is True and r.episode_id == old.episode_id
    assert r.superseded is None  # a memory can never retire itself
    ep = store.get_episode(old.episode_id)
    assert ep.trust == PROVISIONAL and ep.superseded_by is None


def test_write_replaces_store_refusal_is_benign():
    # the store refuses the supersede (target already deprecated): the write
    # STANDS, superseded is None — both rows coexist (pre-supersession status quo).
    svc, store, *_ = _svc()
    old = svc.write("the port is 5432", proposed_by="a")
    store.rows[old.episode_id]["trust"] = DEPRECATED
    store.rows[old.episode_id]["superseded_by"] = None
    r = svc.write("a brand new correction", proposed_by="a", replaces=old.episode_id)
    assert r.status == "approved" and r.superseded is None
    assert store.get_episode(r.episode_id).trust == PROVISIONAL


# ── no-leak on downstream failure ──────────────────────────────────────────────
def test_embed_failure_raises_and_drops_the_staged_row(monkeypatch):
    svc, store, provider, _ = _svc()

    def _boom(_t):
        raise RuntimeError("embedder dead")

    monkeypatch.setattr(provider, "encode", _boom)
    with pytest.raises(RuntimeError, match="embedder dead"):
        svc.write("clean text that fails to embed", proposed_by="a")
    assert store.rows == {} and store.rejected != []


def test_complete_failure_raises_and_drops_the_staged_row():
    svc, store, *_ = _svc()
    store.fail_complete = True
    with pytest.raises(RuntimeError, match="complete failed"):
        svc.write("clean text whose complete loses the CAS", proposed_by="a")
    assert store.rows == {} and store.rejected != []


def test_capture_complete_failure_raises_and_drops_the_staged_row():
    svc, store, *_ = _svc()
    store.fail_complete = True
    with pytest.raises(RuntimeError, match="complete failed"):
        svc.capture("clean capture whose complete loses the CAS", proposed_by="a")
    assert store.rows == {} and store.rejected != []


# ── capture: triggers, dedup powerlessness, the autonomy gate ──────────────────
def test_capture_triggers_promotion_check_and_sweep():
    life = _RecordingLifecycle()
    svc, *_ = _svc(lifecycle=life)
    r = svc.capture("durable insight needing demand", proposed_by="agent-1")
    assert life.captures == [r.episode_id]
    assert life.sweeps == 1
    svc.capture("a second distinct insight", proposed_by="agent-1")
    assert life.sweeps == 2


def test_capture_dedup_never_touches_existing_trust():
    life = _RecordingLifecycle()
    svc, store, *_ = _svc(lifecycle=life)
    w = svc.write("a provisional team fact", proposed_by="a")
    r = svc.capture("a provisional team fact", proposed_by="agent-1")
    assert r.deduped is True and r.episode_id == w.episode_id
    assert store.get_episode(w.episode_id).trust == PROVISIONAL  # untouched
    assert life.captures == [] and life.sweeps == 0  # dedup is not a new candidate


def test_capture_disabled_refuses_before_the_scan():
    life = _RecordingLifecycle()
    svc, store, _provider, scanner = _svc(lifecycle=life, autonomy_enabled=False)
    r = svc.capture("anything at all", proposed_by="agent-1")
    assert r.status == "disabled" and r.episode_id is None and r.scan is None
    assert scanner.scans == 0  # the text was never even scanned
    assert store.stage_calls == [] and store.rows == {}
    assert life.captures == [] and life.sweeps == 0


# ── the secret-never-logged invariant (REFUSE + REDACT paths) ──────────────────
def test_refuse_log_contains_no_secret_substring(caplog):
    svc, *_ = _svc()
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        with pytest.raises(SecretRefused):
            svc.write(f"key {SECRET}", proposed_by="a")
    assert caplog.records
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)


def test_redact_log_contains_no_secret_substring(caplog):
    svc, *_ = _svc(mode=REDACT)
    with caplog.at_level(logging.DEBUG, logger="hive.admission"):
        svc.write(f"key {SECRET} here", proposed_by="a")
    assert caplog.records
    for rec in caplog.records:
        assert SECRET not in rec.getMessage()
        assert SECRET not in str(rec.__dict__)


def test_secret_refused_exception_carries_no_secret():
    svc, *_ = _svc()
    try:
        svc.write(f"key {SECRET}", proposed_by="a")
        raise AssertionError("expected SecretRefused")
    except SecretRefused as e:
        assert SECRET not in str(e)  # only rule names + counts
