"""[C5] — reembed_from_text: the clean-store geometry rewrite that NEVER re-scans.

``test_reembed_does_not_rescan_clean_store`` proves the [C5] asymmetry: the trusted
store's geometry rewrite does NOT call the scanner. ``test_reembed_rejects_nonfinite_
vector_atomically`` proves a NaN/Inf embedder output rolls the whole rewrite back
un-mutated rather than persisting a corrupt BLOB.
"""
from __future__ import annotations

import numpy as np

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.ops.migration import reembed_from_text


def _store(index=None):
    return SqliteEpisodeStore(connect(":memory:"), index=index)


class _SpyScanner:
    def __init__(self):
        self.calls = 0

    def scan(self, text):  # pragma: no cover - must never be called by reembed
        self.calls += 1
        raise AssertionError("reembed must not scan a clean store")


class _ShiftEmbedder:
    d = 8
    w_version = 2

    def encode(self, text):
        v = np.full(self.d, 0.5, dtype=np.float32)
        return v / np.linalg.norm(v)

    def encode_batch(self, texts):  # pragma: no cover - shape only
        return np.stack([self.encode(t) for t in texts])


def test_reembed_does_not_rescan_clean_store():
    store = _store()
    eid, _ = store.stage(text="a clean approved memory", weight=1.0, source="",
                         tags="", proposed_by="u")
    v0 = np.zeros(8, dtype=np.float32)                    # one-hot: a direction the
    v0[0] = 1.0                                           # uniform re-embed cannot match
    store.approve(eid, "u", v0, expected_version=0)
    before = store.get_episode(eid)

    spy = _SpyScanner()
    n = reembed_from_text(store, embedder=_ShiftEmbedder(), scanner=spy)
    assert n == 1
    assert spy.calls == 0                                 # [C5]: NEVER re-scans a clean store

    after = store.get_episode(eid)
    assert after.text == before.text                      # text untouched
    assert after.content_hash == before.content_hash      # hash untouched
    assert after.status == before.status == "approved"    # status untouched
    assert not np.array_equal(after.value, before.value)  # value WAS re-projected


class _PartialNaNEmbedder:
    """Returns a valid unit vector for a clean text but NaN for a 'BAD' text — so a
    multi-row reembed updates the good row FIRST, then hits the NaN on the second row."""
    d = 8
    w_version = 2

    def encode(self, text):
        if "BAD" in text:
            return np.full(self.d, np.nan, dtype=np.float32)   # e.g. a zero vector normalized
        v = np.full(self.d, 0.5, dtype=np.float32)
        return v / np.linalg.norm(v)

    def encode_batch(self, texts):  # pragma: no cover - shape only
        return np.stack([self.encode(t) for t in texts])


def test_reembed_rejects_nonfinite_vector_atomically():
    # A non-finite embedder output must NOT be persisted: reembed raises ReembedError and
    # the ENTIRE rewrite rolls back — even a row that was re-projected BEFORE the bad row
    # keeps its original value (no NaN BLOB that would later crash the index-rebuild
    # finiteness guard and strand the store with a cleared index over corrupt rows).
    import pytest

    from hive.domain.errors import ReembedError

    store = _store()
    good_id, _ = store.stage(text="a good approved memory", weight=1.0, source="",
                             tags="", proposed_by="u")
    bad_id, _ = store.stage(text="a BAD approved memory", weight=1.0, source="",
                            tags="", proposed_by="u")
    onehot = np.zeros(8, dtype=np.float32)
    onehot[0] = 1.0
    store.approve(good_id, "u", onehot, expected_version=0)
    store.approve(bad_id, "u", onehot, expected_version=0)
    good_before = store.get_episode(good_id)

    with pytest.raises(ReembedError):
        reembed_from_text(store, embedder=_PartialNaNEmbedder())

    good_after = store.get_episode(good_id)
    assert np.array_equal(good_after.value, good_before.value)   # the good row's update rolled back
    assert bool(np.all(np.isfinite(good_after.value)))           # no NaN anywhere
