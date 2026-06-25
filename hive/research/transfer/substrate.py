"""Transfer-pair carriers + the out-of-band window loader (Chunk 2).

The frozen carriers make an illegal benchmark state UNCONSTRUCTABLE (Law 2): a fact whose ``value``
is not among its ``alternatives`` or is hidden from its ground-truth ``carrier_text``, a pair whose
``value`` equals the naive ``default_value`` a memory-less agent assumes, or a recall query that
leaks the ``value`` token, all RAISE at construction. These mirror the import-time firewall in the
``../Benchmark/transfer`` generator, re-asserted on the consumer side so a malformed window can never
flow into the runner.

``load_transfer_window`` maps the generator's plain dict onto these carriers under a HARD contract
(a missing key RAISES). ``fixture_transfer_window`` is a self-contained offline window (no
``../Benchmark`` dependency) for the no-API tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


def _tokens(text: str) -> set[str]:
    """Lowercased word tokens, split on non-alphanumerics — the same tokenizer the substrate
    generator uses, so disjointness is judged at WORD level (``uid`` ≠ ``id``), not substring."""
    out, cur = set(), []
    for ch in text.lower():
        if ch.isalnum():
            cur.append(ch)
        else:
            if cur:
                out.add("".join(cur))
                cur = []
    if cur:
        out.add("".join(cur))
    return out


@dataclass(frozen=True)
class TransferFact:
    """One run-randomized fact. ``value`` is the fact this run; ``alternatives`` are the candidates;
    ``carrier_text`` is the ground-truth phrasing (contains ``value``) used for fidelity scoring and
    is NEVER shown to a building agent."""

    pair_id: str
    anchor: str
    value: str
    alternatives: tuple[str, ...]
    carrier_text: str
    kind: str = "note"

    def __post_init__(self) -> None:
        if self.value not in self.alternatives:
            raise ValueError(f"{self.pair_id}: value {self.value!r} not in alternatives")
        if len(self.alternatives) < 2:
            raise ValueError(f"{self.pair_id}: need >=2 alternatives (unguessability)")
        if not (_tokens(self.value) <= _tokens(self.carrier_text)):
            raise ValueError(f"{self.pair_id}: carrier_text does not contain value tokens")


@dataclass(frozen=True)
class TransferPair:
    """One transfer edge: an upstream task that EARNS ``fact`` and a downstream task that REQUIRES
    it. Carries the build materials (prompts + the visible/hidden test sources) the harness
    materializes. ``default_value`` is the naive guess a memory-less agent makes — pinned ``!=``
    the fact value, so the no-recall arm fails by construction. ``downstream_query`` is the recall
    query: topic-matchable but token-disjoint from the value (no reward-hacking)."""

    pair_id: str
    fact: TransferFact
    upstream_task_id: str
    downstream_task_id: str
    downstream_query: str
    default_value: str
    upstream_prompt: str = ""
    downstream_prompt: str = ""
    upstream_test: str = ""
    downstream_test: str = ""
    hidden_test: str = ""

    def __post_init__(self) -> None:
        if self.fact.value == self.default_value:
            raise ValueError(f"{self.pair_id}: value equals default (always-guessable)")
        leak = _tokens(self.fact.value) & _tokens(self.downstream_query)
        if leak:
            raise ValueError(f"{self.pair_id}: downstream_query leaks value token(s) {leak}")


@dataclass(frozen=True)
class TransferWindow:
    """The assembled set of transfer pairs."""

    pairs: tuple[TransferPair, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.pairs:
            raise ValueError("transfer window is empty")
        ids = [p.pair_id for p in self.pairs]
        if len(set(ids)) != len(ids):
            raise ValueError(f"duplicate pair_id in window: {ids}")


_REQUIRED_PAIR_KEYS = (
    "pair_id", "anchor", "alternatives", "value", "default_value", "carrier_text",
    "downstream_query", "upstream_task_id", "downstream_task_id", "upstream_prompt",
    "downstream_prompt", "upstream_test", "downstream_test", "hidden_test",
)


def _pair_from_dict(d: dict) -> TransferPair:
    missing = [k for k in _REQUIRED_PAIR_KEYS if k not in d]
    if missing:
        raise ValueError(f"transfer pair dict missing keys: {missing}")
    fact = TransferFact(
        pair_id=d["pair_id"], anchor=d["anchor"], value=d["value"],
        alternatives=tuple(d["alternatives"]), carrier_text=d["carrier_text"],
        kind=d.get("kind", "note"))
    return TransferPair(
        pair_id=d["pair_id"], fact=fact,
        upstream_task_id=d["upstream_task_id"], downstream_task_id=d["downstream_task_id"],
        downstream_query=d["downstream_query"], default_value=d["default_value"],
        upstream_prompt=d["upstream_prompt"], downstream_prompt=d["downstream_prompt"],
        upstream_test=d["upstream_test"], downstream_test=d["downstream_test"],
        hidden_test=d["hidden_test"])


def load_transfer_window(spec: dict) -> TransferWindow:
    """Map the ``../Benchmark/transfer`` generator dict onto the frozen carriers. HARD contract:
    a missing ``pairs`` key or a missing required pair key RAISES (never a silent default)."""
    if "pairs" not in spec:
        raise ValueError("transfer window spec missing 'pairs'")
    return TransferWindow(pairs=tuple(_pair_from_dict(p) for p in spec["pairs"]))


# ---------------------------------------------------------------------------
# Offline fixture (self-contained — no ../Benchmark dependency)
# ---------------------------------------------------------------------------

_FIXTURE = (
    {
        "pair_id": "id_field", "anchor": "the field key holding the account id",
        "alternatives": ("acct", "uid"), "default_value": "account_id",
        "downstream_query": "which field key stores the account identifier",
        "carrier_tmpl": "The account id is stored under the @T@ field key.",
        "up": "id_field__up", "down": "id_field__down",
    },
    {
        "pair_id": "status_token", "anchor": "the status string marking a record active",
        "alternatives": ("live", "enabled"), "default_value": "active",
        "downstream_query": "what status string marks a record as currently active",
        "carrier_tmpl": "A record is active when its status field equals @T@.",
        "up": "status_token__up", "down": "status_token__down",
    },
)


def fixture_transfer_window(seed: int) -> TransferWindow:
    """A deterministic, self-contained window (2 pairs) for the no-API tests. The value is chosen by
    seed parity from each pair's alternatives, always ``!=`` the default."""
    pairs = []
    for i, f in enumerate(_FIXTURE):
        value = f["alternatives"][(seed + i) % len(f["alternatives"])]
        pairs.append(_pair_from_dict({
            "pair_id": f["pair_id"], "anchor": f["anchor"],
            "alternatives": list(f["alternatives"]), "value": value,
            "default_value": f["default_value"],
            "carrier_text": f["carrier_tmpl"].replace("@T@", value),
            "downstream_query": f["downstream_query"],
            "upstream_task_id": f["up"], "downstream_task_id": f["down"],
            "upstream_prompt": f"implement the {f['pair_id']} upstream task",
            "downstream_prompt": f"implement the {f['pair_id']} downstream task",
            "upstream_test": "# visible upstream test (reveals value)",
            "downstream_test": "# visible downstream test (value-agnostic)",
            "hidden_test": "# hidden gold test (pins value)",
        }))
    return TransferWindow(pairs=tuple(pairs))
