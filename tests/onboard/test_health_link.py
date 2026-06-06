"""P1.12 / M07 — the additive hive_health link extension.

No repo_path ⇒ the snapshot is BYTE-IDENTICAL to the pre-M07 payload (link keys
absent). With repo_path ⇒ ``linked``/``link`` surface via the injected link probe.
A failing core probe (ok=False) still omits link keys when no repo_path was given.
"""
from __future__ import annotations

from hive.app.config import Config
from hive.app.health import health


class _FakeStore:
    def __init__(self, *, raises=False, tick="1700000000"):
        self._raises = raises
        self._tick = tick

    def counts(self):
        if self._raises:
            raise RuntimeError("db gone")
        return (3, 1)

    def meta_get(self, key):
        return self._tick


class _FakeEmbedder:
    loaded = True
    w_version = 1

    def encode(self, text):  # pragma: no cover
        raise NotImplementedError

    def encode_batch(self, texts):  # pragma: no cover
        raise NotImplementedError


_PRE_M07_KEYS = {
    "ok", "db_path", "tenant_id", "embedder_loaded", "w_version", "index_authoritative",
    "episodes_approved", "episodes_pending", "error",
}


def _cfg():
    return Config.load(db_path=":memory:")


def test_health_unlinked_byte_identical():
    snap = health(_cfg(), _FakeStore(), _FakeEmbedder())  # no repo_path
    d = snap.as_dict()
    assert "linked" not in d and "link" not in d
    assert set(d.keys()) == _PRE_M07_KEYS                # byte-identical pre-M07 payload


def test_health_reports_link():
    def link_status(repo_path):
        return True, {"block_version": 1, "block_hash": "abc123", "trailer_key": "Hive-Trace"}

    snap = health(_cfg(), _FakeStore(), _FakeEmbedder(),
                  repo_path="/r", link_status=link_status)
    d = snap.as_dict()
    assert d["linked"] is True
    assert d["link"]["block_version"] == 1


def test_health_repo_path_no_probe_is_unlinked():
    snap = health(_cfg(), _FakeStore(), _FakeEmbedder(), repo_path="/r")
    d = snap.as_dict()
    assert d["linked"] is False and d["link"] is None    # present (probed) but unlinked


def test_health_unlinked_repo_probe_false():
    snap = health(_cfg(), _FakeStore(), _FakeEmbedder(),
                  repo_path="/r", link_status=lambda rp: (False, None))
    d = snap.as_dict()
    assert d["linked"] is False and d["link"] is None


def test_health_link_probe_failure_is_soft():
    # a raising link probe must NOT crash health nor flip ok=False — degrade to unlinked
    def boom(repo_path):
        raise RuntimeError("planner exploded")

    snap = health(_cfg(), _FakeStore(), _FakeEmbedder(),
                  repo_path="/r", link_status=boom)
    assert snap.ok is True
    d = snap.as_dict()
    assert d["linked"] is False and d["link"] is None


def test_health_failsoft_no_repo_omits_link_keys():
    # core probe fails (ok=False) and no repo_path ⇒ still byte-identical (no link keys)
    snap = health(_cfg(), _FakeStore(raises=True), _FakeEmbedder())
    d = snap.as_dict()
    assert snap.ok is False
    assert "linked" not in d and "link" not in d
