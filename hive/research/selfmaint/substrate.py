"""S2 — the scenario seeder (the 3-class taxonomy + the servable establish).

Authors the seeded memories the demonstration measures self-maintenance over, labeled by
provenance ``source_id`` ONLY (the agent never sees a "this is bad" tag — the same anti-telegraph
law as ``poison_substrate``). Three regimes:

* **prunable_bad** (``kind ∈ {mistake, stale}``) — a work-falsifiable WRONG value: a token-disjoint
  false copy of a real fact, carved from real prose, so acting on it FAILS the executed test. The
  agent EARNS detection by hitting the contradiction; it should be pruned.
* **keep_valuable** — a correct fact a later task genuinely NEEDS (that task fails iff it is wrongly
  retired). Should be kept + used.
* **keep_neutral** — a correct, harmless, off-topic fact. Should be LEFT — over-pruning it is the
  precision failure.

``build_selfmaint_substrate`` is OFF-is-inert (empty ``regimes`` ⇒ no seeds, a clean sequence) and
per-case clean-fallback (BUG-006: an unsynthesizable falsehood is skipped, never aborting the
build). It is repo-parameterized; ``fixture_repo_window`` is a self-contained window for the
offline tests + the no-API smoke (a real run swaps in a post-cutoff repo's symbols + tests).

Reuses the pure poison helpers where the shape matches (token-disjoint false synthesis, the
anti-telegraph guard); it does NOT reuse the comparative bench's ``MemoryBackend`` / ``Case`` /
``build_substrate`` machinery (the dropped premise). Dev-time only (purity-fenced).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional, Sequence

from hive.research.bench.poison_substrate import (
    _assert_not_telegraphed, _false_value_for, _mutate_value,
)
from hive.research.bench.task_agent import _content_tokens

_REGIMES = ("prunable_bad", "keep_neutral", "keep_valuable")
_KINDS = ("mistake", "stale", "note")


# ── the ground-truth repo window the seeds are authored against ──────────────────────
@dataclass(frozen=True)
class RepoFact:
    """One ground-truth fact about the repo window. For a ``prunable_bad`` fact, ``carrier_text``
    is real prose containing ``true_value`` (the current-tree value); the seeder substitutes a
    token-disjoint false value into it. For a keep fact, ``carrier_text`` IS the true assertion.

    ``false_value`` is an OPTIONAL hand-authored, work-falsifiable wrong value for a ``prunable_bad``
    fact (the value whose named acceptance test the substrate author already verified red). When
    given, the seeder uses it verbatim instead of synthesizing one — but it is still run through the
    single-sourced ``SeedSpec`` guard (token-disjoint + non-telegraphing), so a telegraphing or
    overlapping explicit false is rejected and the seed falls back clean (BUG-006)."""
    source_id: str
    regime: str
    true_value: str
    carrier_text: str
    kind: str = "note"
    false_value: Optional[str] = None


@dataclass(frozen=True)
class RepoTask:
    """One task in the ordered dependent sequence: which keep-valuable facts it needs and which
    prunable-bad facts falsify it, plus the external test-outcome gold reference."""
    task_id: str
    query: str
    needs_valuable: frozenset = field(default_factory=frozenset)
    falsified_by: frozenset = field(default_factory=frozenset)
    expected: Optional[str] = None


@dataclass(frozen=True)
class RepoWindow:
    facts: tuple = ()
    tasks: tuple = ()


# ── the seeded-memory + task carriers (frozen, self-asserting) ───────────────────────
@dataclass(frozen=True)
class SeedSpec:
    """One seeded memory. A ``prunable_bad`` seed carries token-disjoint ``true_value`` /
    ``false_value`` (the same invariant as ``PoisonSpec`` — emitting the false value can never be
    confused with echoing the true value); a keep seed carries no false value."""
    source_id: str
    text: str
    regime: str
    kind: str
    true_value: Optional[str] = None
    false_value: Optional[str] = None

    def __post_init__(self) -> None:
        if self.regime not in _REGIMES:
            raise ValueError(f"regime must be one of {_REGIMES}, got {self.regime!r}")
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {self.kind!r}")
        if self.regime == "prunable_bad":
            if self.kind not in ("mistake", "stale"):
                raise ValueError(f"a prunable_bad seed needs kind mistake|stale, got {self.kind!r}")
            if not self.true_value or not self.false_value:
                raise ValueError("a prunable_bad seed requires truthy true_value/false_value")
            if self.true_value == self.false_value:
                raise ValueError(f"true_value and false_value must differ, both {self.true_value!r}")
            shared = _content_tokens(self.true_value) & _content_tokens(self.false_value)
            if shared:
                raise ValueError(
                    f"true_value {self.true_value!r} and false_value {self.false_value!r} share "
                    f"content token(s) {sorted(shared)} — they must be token-disjoint")
        else:
            if self.false_value is not None:
                raise ValueError("a keep regime carries no false_value")
            if self.kind != "note":
                raise ValueError("a keep regime uses kind='note'")


@dataclass(frozen=True)
class SelfMaintTaskCase:
    """One task in the ordered sequence, referencing ONLY successfully-seeded facts."""
    task_id: str
    query: str
    needs_valuable: frozenset = field(default_factory=frozenset)
    falsified_by: frozenset = field(default_factory=frozenset)
    expected: Optional[str] = None


def _seed_key(seed: int, source_id: str) -> int:
    """A deterministic per-source key (a STABLE hash — never builtin ``hash()``, which is salted
    per process and would break bit-for-bit replay). // O(len)."""
    digest = hashlib.sha256(source_id.encode("utf-8")).digest()
    return seed ^ int.from_bytes(digest[:4], "big")


def build_selfmaint_substrate(
    repo_window: RepoWindow, *, seed: int, regimes: Sequence[str] = _REGIMES,
) -> tuple[list[SeedSpec], list[SelfMaintTaskCase]]:
    """Synthesize the ``SeedSpec``s + the ordered ``SelfMaintTaskCase`` sequence from the repo
    window. OFF-is-inert: with ``regimes == ()`` no fact is seeded and the task sequence references
    nothing (a clean run). Per-case clean-fallback: a ``prunable_bad`` fact whose false value would
    telegraph itself or fail the token-disjoint guard is SKIPPED (the build never aborts — BUG-006).
    The task cases reference ONLY successfully-seeded facts, so a dropped bad fact silently leaves
    its tasks un-falsified rather than scoring a phantom miss."""
    selected = set(regimes)
    specs: list[SeedSpec] = []
    seeded: set[str] = set()
    for fact in repo_window.facts:
        if fact.regime not in selected:
            continue
        if fact.regime == "prunable_bad":
            try:
                # a hand-authored false value (already test-verified) rides through verbatim;
                # otherwise synthesize one. Either way the SeedSpec guard below has the final word.
                false_value = (fact.false_value
                               or _false_value_for(fact.true_value, _seed_key(seed, fact.source_id)))
                text = _mutate_value(fact.carrier_text, fact.true_value, false_value)
                _assert_not_telegraphed(text, true_value=fact.true_value)
                spec = SeedSpec(source_id=fact.source_id, text=text, regime="prunable_bad",
                                kind=fact.kind, true_value=fact.true_value, false_value=false_value)
            except ValueError:
                continue           # unsynthesizable / telegraphing falsehood ⇒ skip this seed (BUG-006)
        else:
            spec = SeedSpec(source_id=fact.source_id, text=fact.carrier_text, regime=fact.regime,
                            kind=fact.kind, true_value=fact.true_value)
        specs.append(spec)
        seeded.add(fact.source_id)

    tasks = [
        SelfMaintTaskCase(
            task_id=t.task_id, query=t.query,
            needs_valuable=frozenset(t.needs_valuable) & seeded,
            falsified_by=frozenset(t.falsified_by) & seeded,
            expected=t.expected)
        for t in repo_window.tasks
    ]
    return specs, tasks


def _require(item, fields: Sequence[str], what: str):
    """Read each required field off ``item`` (a dataclass instance or a dict), RAISING ValueError on
    any absent one — the loader is an enforced contract, not a best-effort mapper. Returns the field
    values in order. // O(len(fields))."""
    out = []
    for f in fields:
        if isinstance(item, dict):
            if f not in item:
                raise ValueError(f"malformed {what}: missing required key {f!r}")
            out.append(item[f])
        else:
            sentinel = object()
            val = getattr(item, f, sentinel)
            if val is sentinel:
                raise ValueError(f"malformed {what}: missing required attribute {f!r}")
            out.append(val)
    return out


def load_repo_window(spec: dict) -> RepoWindow:
    """Map an out-of-band substrate spec (the Benchmark ``repo_window()`` dict —
    ``{tasks, facts, ...}`` whose items are ``Task`` / ``Fact`` records) onto the internal
    ``RepoWindow`` / ``RepoFact`` / ``RepoTask`` the seeder consumes. A pure dict→dataclass mapping
    that imports nothing from the product; it is a HARD contract — a missing top-level key, or an
    item missing a required field, RAISES ValueError (never a silently-empty window). The Benchmark
    ``Task.build_prompt`` becomes the seeder's task ``query``; explicit ``false_value``s ride through
    onto ``RepoFact`` so a test-verified falsehood is seeded verbatim."""
    if not isinstance(spec, dict):
        raise ValueError(f"repo-window spec must be a dict, got {type(spec).__name__}")
    for key in ("tasks", "facts"):
        if key not in spec:
            raise ValueError(f"malformed repo-window spec: missing required key {key!r}")

    facts: list[RepoFact] = []
    for raw in spec["facts"]:
        source_id, regime, kind, true_value, carrier_text, false_value, _gated_by = _require(
            raw, ("source_id", "regime", "kind", "true_value", "carrier_text", "false_value",
                  "gated_by"), "fact")
        facts.append(RepoFact(source_id=source_id, regime=regime, true_value=true_value,
                              carrier_text=carrier_text, kind=kind, false_value=false_value))

    tasks: list[RepoTask] = []
    for raw in spec["tasks"]:
        task_id, build_prompt, _gated, _deps, needs_valuable, falsified_by = _require(
            raw, ("task_id", "build_prompt", "gated_by", "depends_on", "needs_valuable",
                  "falsified_by"), "task")
        tasks.append(RepoTask(task_id=task_id, query=build_prompt,
                              needs_valuable=frozenset(needs_valuable),
                              falsified_by=frozenset(falsified_by)))

    return RepoWindow(facts=tuple(facts), tasks=tuple(tasks))


def seed_store(client, specs: Sequence[SeedSpec], *, approver: str) -> dict[str, int]:
    """Establish each seed SERVABLE via the orchestrator-vouched ``hive_write`` seam — so a seeded
    bad fact is genuinely servable in EVERY arm (the demonstration measures serve-time selection /
    retirement, never structural admission-absence). Returns ``source_id → episode_id`` over what
    actually became servable; a write refused at the secret floor (no ``id``) is SKIPPED, never
    leaked as an empty handle (BUG-002). // O(specs)."""
    mapping: dict[str, int] = {}
    for spec in specs:
        res = client.write(spec.text, approved_by=approver)
        eid = res.get("id") if isinstance(res, dict) else None
        if eid is None:        # refused / disabled ⇒ never stored (no empty-handle leak)
            continue
        mapping[spec.source_id] = int(eid)
    return mapping


def fixture_repo_window() -> RepoWindow:
    """A self-contained repo window for the offline tests + the no-API smoke (one fact per regime,
    two dependent tasks). A real run replaces this with a post-cutoff repo's symbols + tests."""
    return RepoWindow(
        facts=(
            RepoFact("bad::parse-arity", "prunable_bad", "2",
                     "The parse function takes 2 positional arguments.", kind="stale"),
            RepoFact("keep::config-path", "keep_valuable", "config/app.toml",
                     "The application configuration file is config/app.toml.", kind="note"),
            RepoFact("keep::license", "keep_neutral", "MIT",
                     "This project is distributed under the MIT license.", kind="note"),
        ),
        tasks=(
            RepoTask("t1", "How many positional arguments does the parse function take?",
                     falsified_by=frozenset({"bad::parse-arity"}), expected="2"),
            RepoTask("t2", "What path holds the application configuration file?",
                     needs_valuable=frozenset({"keep::config-path"}), expected="config/app.toml"),
        ),
    )
