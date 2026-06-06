"""P1.9 — M11 health(): a cheap, poll-safe, NEVER-raising snapshot.

embedder_loaded present+boolean (the container gate reads it); index_authoritative
from config; fail-soft to ok=False on ANY probe failure (store / embedder / producer).
"""
from __future__ import annotations

from hive.app.config import Config
from hive.app.health import health, HealthSnapshot


class _FakeStore:
    def __init__(self, *, raises: bool = False, tick: str | None = "1700000000"):
        self._raises = raises
        self._tick = tick

    def counts(self):
        if self._raises:
            raise RuntimeError("db gone")
        return (7, 2)  # (approved, pending)

    def meta_get(self, key):
        return self._tick


class _FakeEmbedder:
    def __init__(self, *, loaded: bool):
        self.loaded = loaded
        self.w_version = 1

    def encode(self, text):  # pragma: no cover - shape only
        raise NotImplementedError

    def encode_batch(self, texts):  # pragma: no cover - shape only
        raise NotImplementedError


class _FakeProducer:
    def step(self, now):  # pragma: no cover - shape only
        raise NotImplementedError


def test_health_ok_shape():
    cfg = Config.load(db_path="/tmp/hive-test.db")
    snap = health(cfg, _FakeStore(), _FakeEmbedder(loaded=True), _FakeProducer())
    assert isinstance(snap, HealthSnapshot)
    assert snap.ok is True
    assert snap.db_path == "/tmp/hive-test.db"
    assert snap.error is None
    assert snap.episodes_approved == 7


def test_health_embedder_loaded_key_present():
    cfg = Config.load(db_path=":memory:")
    d = health(cfg, _FakeStore(), _FakeEmbedder(loaded=True), _FakeProducer()).as_dict()
    assert "embedder_loaded" in d
    assert isinstance(d["embedder_loaded"], bool)
    assert d["embedder_loaded"] is True


def test_health_embedder_not_loaded_is_false():
    cfg = Config.load(db_path=":memory:")
    snap = health(cfg, _FakeStore(), _FakeEmbedder(loaded=False), _FakeProducer())
    assert snap.embedder_loaded is False


def test_health_index_authoritative_true():
    cfg = Config.load(db_path=":memory:", index={"backend": "exhaustive"})
    snap = health(cfg, _FakeStore(), _FakeEmbedder(loaded=True), _FakeProducer())
    assert snap.index_authoritative is True


def test_health_fail_soft_on_store_error():
    cfg = Config.load(db_path=":memory:")
    snap = health(cfg, _FakeStore(raises=True), _FakeEmbedder(loaded=True), _FakeProducer())
    assert snap.ok is False
    assert snap.error is not None
    assert "sk-" not in (snap.error or "")  # never leak a secret in the error string


def test_health_fail_soft_on_embedder_probe_error():
    cfg = Config.load(db_path=":memory:")

    class _Boom:
        @property
        def loaded(self):
            raise RuntimeError("embedder probe blew up")

    snap = health(cfg, _FakeStore(), _Boom(), _FakeProducer())
    assert snap.ok is False
    assert snap.error is not None


def test_health_malformed_tick_stays_ok():
    # a non-integer meta tick is a benign format issue, NOT a DB outage:
    cfg = Config.load(db_path=":memory:")
    snap = health(cfg, _FakeStore(tick="not-a-number"), _FakeEmbedder(loaded=True), _FakeProducer())
    assert snap.ok is True                  # store probe succeeded
    assert snap.last_producer_tick_ts is None


def test_health_never_raises_returns_snapshot():
    cfg = Config.load(db_path=":memory:")
    # pass deliberately broken probes; health must still return a snapshot, never raise
    snap = health(cfg, object(), object(), object())
    assert isinstance(snap, HealthSnapshot)
    assert snap.ok is False
