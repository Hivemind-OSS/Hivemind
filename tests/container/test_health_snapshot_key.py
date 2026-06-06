"""P1.13 / M12 — the cross-module contract the healthcheck depends on.

M12's "healthy ≡ embedder resident" reads `embedder_loaded` from the health snapshot.
The M12 design review flagged this as a seam that could silently drift (a missing key
reads falsey ⇒ red for the WRONG reason). This test pins that the key is PRESENT + boolean
in the REAL `health()` snapshot and that it TRACKS `embedder.loaded` — so a regression
that drops the key (or stops populating it) goes red distinctly from an unloaded embedder.
"""
from __future__ import annotations

from hive.app.config import Config
from hive.app.health import health


class _Store:
    def counts(self):
        return (2, 1)

    def meta_get(self, key):
        return "1700000000"


class _Embedder:
    def __init__(self, loaded: bool) -> None:
        self.loaded = loaded
        self.w_version = 1


class _Producer:
    pass


def _cfg():
    return Config.load(toml_path=None)


def test_health_snapshot_has_embedder_loaded_key():
    snap = health(_cfg(), _Store(), _Embedder(loaded=True), _Producer()).as_dict()
    assert "embedder_loaded" in snap
    assert isinstance(snap["embedder_loaded"], bool)


def test_embedder_loaded_tracks_resident_state():
    hot = health(_cfg(), _Store(), _Embedder(loaded=True), _Producer()).as_dict()
    cold = health(_cfg(), _Store(), _Embedder(loaded=False), _Producer()).as_dict()
    assert hot["embedder_loaded"] is True
    assert cold["embedder_loaded"] is False


def test_embedder_loaded_present_even_on_failed_probe():
    # store probe raises ⇒ ok=False but the key is still present + boolean (never absent)
    class _Boom:
        def counts(self):
            raise RuntimeError("db gone")

        def meta_get(self, key):
            return None

    snap = health(_cfg(), _Boom(), _Embedder(loaded=True), _Producer()).as_dict()
    assert snap["ok"] is False
    assert snap["embedder_loaded"] is False and isinstance(snap["embedder_loaded"], bool)
