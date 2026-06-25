"""Chunk 4 — the diagnostic chain decomposition (off the gold path).

These reducers explain WHERE a transfer chain broke (the upstream agent stored the wrong fact / the
fact never became servable / the downstream recall did not surface it) by reading text observations
against the ground-truth value. They are a fail-soft side-channel: empty input yields empty output,
never a raise.
"""
from __future__ import annotations

from hive.research.transfer import diagnostics


def test_carries_value_token_containment():
    got = diagnostics.carries_value(
        {"id_field": ["the account id is under the acct field key"],
         "status_token": ["a record is active when status is enabled"]},
        {"id_field": "acct", "status_token": "ready"})
    assert got == {"id_field": True, "status_token": False}  # served the WRONG status value


def test_carries_value_any_text_carries():
    got = diagnostics.carries_value(
        {"p": ["unrelated note", "the key is holder"]}, {"p": "holder"})
    assert got == {"p": True}


def test_carries_value_skips_unlabeled_pair_and_is_failsoft():
    assert diagnostics.carries_value({}, {}) == {}
    # a pair with observations but no ground-truth label contributes nothing (no raise)
    assert diagnostics.carries_value({"p": ["holder"]}, {}) == {}


def test_chain_decomposition_three_stages():
    out = diagnostics.chain_decomposition(
        captured={"p": ["stored under the acct key"]},
        servable={"p": ["stored under the acct key"]},
        served={"p": ["nothing relevant"]},
        value_by_pair={"p": "acct"})
    assert out["capture_fidelity"] == {"p": True}
    assert out["promoted_servable"] == {"p": True}
    assert out["served_correct"] == {"p": False}  # promoted but recall did not surface it
