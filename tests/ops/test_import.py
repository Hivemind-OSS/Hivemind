"""P1.12 / M07 / [B4][C5] — import_corpus (scan-before-stage, lands pending) +
reembed_from_text (clean-store geometry rewrite, NEVER re-scans).

``test_import_scans_secrets`` ★ extends the unrecallable-secret guard to the import boundary (mutation:
comment out the scan → the planted token persists → red). ``test_import_lands_pending``
proves a seeded corpus is never auto-recallable. ``test_reembed_does_not_rescan_clean_store``
proves the [C5] asymmetry: the trusted store's geometry rewrite does NOT call the scanner.
"""
from __future__ import annotations

import numpy as np

from hive.adapters.scanner_regex import DefaultSecretScanner
from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.domain.models import content_hash
from hive.ops.migration import ImportReport, import_corpus, reembed_from_text

_AKIA = "AKIA" + "Q7XALERTROGUE99"[:16].ljust(16, "X")    # AKIA + 16 [0-9A-Z] ⇒ aws_akia rule


def _store(index=None):
    return SqliteEpisodeStore(connect(":memory:"), index=index)


def test_import_lands_pending():
    store = _store()
    rows = [{"text": "an old insight about retry backoff"},
            {"text": "a non-obvious decision about the cache key"}]
    rep = import_corpus(rows, store=store, scanner=DefaultSecretScanner())
    assert isinstance(rep, ImportReport)
    assert rep.n_imported == 2 and rep.n_total == 2
    approved, pending = store.counts()
    assert approved == 0 and pending == 2                 # never auto-recallable
    ep = store.get_episode(1)
    assert ep.status == "pending"
    assert ep.proposed_by == "import-admin"
    assert ep.value is None                               # value computed only at approve


def test_import_scans_secrets():
    store = _store()
    rows = [{"text": f"deploy step uses {_AKIA} then continues"}]
    rep = import_corpus(rows, store=store, scanner=DefaultSecretScanner())  # default: refuse
    assert rep.n_refused == 1 and rep.n_imported == 0
    _, pending = store.counts()
    assert pending == 0                                   # nothing staged
    # the secret reached NO episodes row and NO blob
    episode_text = "".join(r["text"] for r in store.conn.execute("SELECT text FROM episodes"))
    blob_text = "".join(r["text"] for r in store.conn.execute("SELECT text FROM blobs"))
    assert _AKIA not in episode_text and _AKIA not in blob_text


def test_import_redacts_secret_lands_masked():
    store = _store()
    rows = [{"text": f"old note with {_AKIA} embedded"}]
    rep = import_corpus(rows, store=store,
                        scanner=DefaultSecretScanner(redact_mode="redact"))
    assert rep.n_redacted == 1 and rep.n_imported == 1
    ep = store.get_episode(1)
    assert _AKIA not in ep.text                           # masked
    assert ep.status == "pending"
    assert ep.content_hash == content_hash(ep.text)       # hash re-derived over the redaction
    blob_text = "".join(r["text"] for r in store.conn.execute("SELECT text FROM blobs"))
    assert _AKIA not in blob_text


def test_import_idempotent_resume():
    store = _store()
    rows = [{"text": "resumable memory one"}, {"text": "resumable memory two"}]
    import_corpus(rows, store=store, scanner=DefaultSecretScanner())
    rep2 = import_corpus(rows, store=store, scanner=DefaultSecretScanner())  # re-run (resume)
    assert rep2.n_deduped == 2 and rep2.n_imported == 0
    _, pending = store.counts()
    assert pending == 2                                   # no double-insert


def test_import_mixed_batch_counts():
    store = _store()
    rows = [{"text": "clean one"},
            {"text": f"secret {_AKIA} here"},             # refused
            {"text": "clean two"}]
    rep = import_corpus(rows, store=store, scanner=DefaultSecretScanner())
    assert rep.n_total == 3 and rep.n_imported == 2 and rep.n_refused == 1
    _, pending = store.counts()
    assert pending == 2                                   # one refused row never landed


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
