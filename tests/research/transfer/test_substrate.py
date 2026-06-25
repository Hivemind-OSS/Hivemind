"""Chunk 2 — the transfer carriers + loader (hivemind side, black-box).

Pins the enforced contract (illegal benchmark states unconstructable, Law 2): a fact whose value
is not among its alternatives, a carrier that hides the value, a pair whose value equals the naive
default, and a recall query that leaks the value all RAISE at construction. Also pins that the real
``../Benchmark/transfer`` generator output loads cleanly (the cross-repo contract).
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from hive.research.transfer.substrate import (
    TransferFact, TransferPair, TransferWindow, fixture_transfer_window, load_transfer_window,
)

_BENCH_TRANSFER = Path(__file__).resolve().parents[4] / "Benchmark" / "transfer" / "substrate.py"


def _pair_dict(**over) -> dict:
    """A minimal VALID spec-pair dict (the shape ``transfer_window`` emits)."""
    d = {
        "pair_id": "id_field", "anchor": "the field key that holds the account id",
        "alternatives": ["acct", "uid", "holder"], "value": "acct",
        "default_value": "account_id",
        "carrier_text": "The account id is stored under the acct field key.",
        "downstream_query": "which field key stores the account identifier",
        "upstream_task_id": "id_field__up", "downstream_task_id": "id_field__down",
        "upstream_prompt": "implement get_account_id", "downstream_prompt": "implement count",
        "upstream_test": "u", "downstream_test": "d", "hidden_test": "h",
    }
    d.update(over)
    return d


def test_load_transfer_window_maps_pairs():
    win = load_transfer_window({"pairs": [_pair_dict(), _pair_dict(
        pair_id="status_token", alternatives=["live", "enabled"], value="live",
        default_value="active",
        carrier_text="A record is active when its status equals live.",
        downstream_query="what status string marks a record active",
        upstream_task_id="status_token__up", downstream_task_id="status_token__down")]})
    assert isinstance(win, TransferWindow)
    assert [p.pair_id for p in win.pairs] == ["id_field", "status_token"]
    assert win.pairs[0].fact.value == "acct"
    assert win.pairs[0].default_value == "account_id"
    assert win.pairs[1].hidden_test == "h"


def test_load_transfer_window_raises_on_missing_key():
    bad = _pair_dict()
    del bad["downstream_query"]
    with pytest.raises((KeyError, ValueError)):
        load_transfer_window({"pairs": [bad]})


def test_load_transfer_window_raises_on_missing_pairs():
    with pytest.raises((KeyError, ValueError)):
        load_transfer_window({})


def test_fact_rejects_value_not_in_alternatives():
    with pytest.raises(ValueError):
        TransferFact(pair_id="x", anchor="a", value="nope",
                     alternatives=("acct", "uid"), carrier_text="... nope ...")


def test_fact_rejects_too_few_alternatives():
    with pytest.raises(ValueError):
        TransferFact(pair_id="x", anchor="a", value="acct",
                     alternatives=("acct",), carrier_text="... acct ...")


def test_fact_rejects_carrier_without_value():
    with pytest.raises(ValueError):
        TransferFact(pair_id="x", anchor="a", value="acct",
                     alternatives=("acct", "uid"), carrier_text="no token here")


def test_pair_rejects_value_equals_default():
    fact = TransferFact(pair_id="x", anchor="a", value="acct",
                        alternatives=("acct", "uid"), carrier_text="... acct ...")
    with pytest.raises(ValueError):
        TransferPair(pair_id="x", fact=fact, upstream_task_id="u", downstream_task_id="d",
                     downstream_query="which key", default_value="acct")


def test_pair_rejects_query_leaking_value():
    fact = TransferFact(pair_id="x", anchor="a", value="acct",
                        alternatives=("acct", "uid"), carrier_text="... acct ...")
    with pytest.raises(ValueError):
        TransferPair(pair_id="x", fact=fact, upstream_task_id="u", downstream_task_id="d",
                     downstream_query="which acct goes here", default_value="account_id")


def test_window_rejects_empty():
    with pytest.raises(ValueError):
        TransferWindow(pairs=())


def test_fixture_transfer_window_is_valid_and_deterministic():
    a = fixture_transfer_window(0)
    b = fixture_transfer_window(0)
    assert isinstance(a, TransferWindow)
    assert len(a.pairs) >= 2
    assert [p.pair_id for p in a.pairs] == [p.pair_id for p in b.pairs]
    for p in a.pairs:
        assert p.fact.value in p.fact.alternatives
        assert p.fact.value != p.default_value


@pytest.mark.skipif(not _BENCH_TRANSFER.exists(), reason="../Benchmark/transfer not present")
def test_real_generator_output_loads():
    spec = importlib.util.spec_from_file_location("_bench_transfer", _BENCH_TRANSFER)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_bench_transfer"] = mod
    spec.loader.exec_module(mod)
    win = load_transfer_window(mod.transfer_window(0))
    assert len(win.pairs) == 6
    for p in win.pairs:
        assert p.fact.value in p.fact.alternatives
        assert p.fact.value != p.default_value
