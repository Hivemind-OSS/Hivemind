"""C2(poison) — the adversarial-overlay substrate for the poisoned-shared-store benchmark.

Builds ON the clean ``substrate.build_substrate`` pool: for a deterministic, seed-keyed
fraction of the answerable cases it synthesizes a *committed-servable falsehood* — a
wrong-valued copy of a real gold fact (``kind="mistake"``) or the stale half of an
``(old_false, new_true)`` pair (``kind="stale"``) — carved out of a REAL haystack turn so
the false text is never synthetic (synthetic text is gameable; the agent could learn the
tell). Each falsehood is committed under a DISTINCT non-gold ``source_id`` so it ranks as a
competing hard-negative, never masquerades as gold, and never silently flips ``served_gold``.

Two invariants make the downstream ``emitted_false`` scoring sound and the corpus honest:

  * **token-disjoint values** — ``false_value`` shares no CONTENT token with ``true_value``
    (enforced in ``PoisonSpec.__post_init__``), so a reply that emits the false value cannot
    be mistaken for one that merely echoes the true value.
  * **no self-telegraphing** — the synthesized false text may not contain a tell
    (``wrong``/``fake``/``ignore``/…) nor echo the true value, so the falsehood cannot leak
    its own label into the lexical channel (``_assert_not_telegraphed``).

``build_poison_substrate`` is OFF-is-inert: with ``kinds == ()`` it returns a memory pool
**byte-identical** to ``build_substrate``'s, so the overlay can be A/B'd against the clean run
with zero structural drift.

Dev-time only (under ``hive.research`` — the purity fence forbids the runtime importing it).
The value-token reuse keeps the disjointness/telegraph checks single-sourced with the agent
loop's answer scorer.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Sequence

from hive.research.bench.dataset import Case
from hive.research.bench.substrate import build_substrate
from hive.research.bench.task_agent import _content_tokens

POISON_VERSION = "poison.v1"

_KINDS = ("mistake", "stale", "credential")
_REGIMES = ("corrected", "credential", "clean")

# A non-credential falsehood may not lexically announce itself: a tell would let the agent (or
# a lexical recall channel) detect the poison for free, invalidating the serve-time measurement.
_TELLS = ("wrong", "fake", "ignore", "do not", "synthetic", "poison")

# Named-real-prefix secret shapes the shipped floor refuses (mirrors hive.domain.secret_scan's
# named rules). Kept here so the data layer can synthesize a credential-shaped fact without
# importing the runtime scanner — the actual refusal is asserted by the backend-touching chunk.
_CREDENTIAL_PREFIXES = ("sk-proj-", "ghp_")
_CREDENTIAL_BODY = "0aZ9bY8cX7dW6eV5fU4tS3rR2qQ1pP0o"


@dataclass(frozen=True)
class PoisonSpec:
    """One synthesized falsehood — a frozen, self-asserting carrier (illegal states unconstructable).

    ``gold_source_id`` is the real session the true fact came from; ``false_source_id`` is the
    DISTINCT non-gold session the falsehood is committed under (so it competes as a hard-negative,
    never poses as gold). ``false_text`` is the mutated real haystack turn. For the non-credential
    kinds ``true_value``/``false_value`` are the token-disjoint payloads whose presence in a reply
    decides ``emitted_true``/``emitted_false``; a credential carries no value pair."""
    gold_source_id: str
    false_source_id: str
    true_value: str
    false_value: str
    false_text: str
    kind: str

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {_KINDS}, got {self.kind!r}")
        if self.false_source_id == self.gold_source_id:
            raise ValueError(
                f"false_source_id must be DISTINCT from gold_source_id "
                f"(both {self.gold_source_id!r}) — a falsehood that reuses the gold source would "
                "masquerade as gold and corrupt served_gold")
        if self.kind != "credential":
            if not self.true_value or not self.false_value:
                raise ValueError(
                    f"a {self.kind!r} PoisonSpec requires truthy true_value/false_value, got "
                    f"true_value={self.true_value!r} false_value={self.false_value!r}")
            if self.true_value == self.false_value:
                raise ValueError(
                    f"true_value and false_value must differ, both {self.true_value!r}")
            true_toks = _content_tokens(self.true_value)
            false_toks = _content_tokens(self.false_value)
            shared = true_toks & false_toks
            if shared:
                raise ValueError(
                    f"true_value {self.true_value!r} and false_value {self.false_value!r} share "
                    f"content token(s) {sorted(shared)} — they must be token-disjoint so emitting "
                    "the false value cannot be confused with echoing the true value")


@dataclass(frozen=True)
class PoisonTaskCase:
    """One poison-aware task — a frozen, self-asserting carrier mirroring ``substrate.TaskCase``.

    ``gold_source_ids`` are the gold-evidence SESSION ids (empty ⇔ unanswerable);
    ``false_source_ids`` are the DISTINCT non-gold sessions the falsehood lives under. The
    ``regime`` partitions the corpus: ``corrected`` (answerable, carries a token-disjoint
    falsehood), ``credential`` (the secret-floor diagnostic), ``clean`` (the unmodified control,
    no falsehood). ``true_value``/``false_value`` are the scoring payloads (``None`` where the
    regime carries no value pair). ``expected`` is the deterministic answer key (``None`` for
    unanswerable)."""
    query: str
    regime: str
    kind: str
    gold_source_ids: frozenset[str]
    false_source_ids: frozenset[str]
    true_value: str | None
    false_value: str | None
    expected: str | None

    def __post_init__(self) -> None:
        if self.regime not in _REGIMES:
            raise ValueError(f"regime must be one of {_REGIMES}, got {self.regime!r}")
        answerable = bool(self.gold_source_ids)
        if self.regime == "corrected":
            if not answerable:
                raise ValueError("a corrected PoisonTaskCase must be answerable (≥1 gold source)")
            if not self.false_source_ids:
                raise ValueError("a corrected PoisonTaskCase must carry ≥1 false source")
            if not self.true_value or not self.false_value:
                raise ValueError("a corrected PoisonTaskCase must carry true/false values")
            shared = _content_tokens(self.true_value) & _content_tokens(self.false_value)
            if shared:
                raise ValueError(
                    f"corrected true/false values must be token-disjoint, share {sorted(shared)}")
        elif self.regime == "clean":
            if self.false_source_ids:
                raise ValueError("a clean PoisonTaskCase must carry NO false source")


def _assert_not_telegraphed(false_text: str, *, true_value: str) -> None:
    """Refuse a falsehood whose text leaks its own label: a tell word, or an echo of the true
    value (which would let the answer scorer credit the true value off the poison row alone).
    Raises ``ValueError`` on either — the corpus invariant that adversarial content can never
    lexically announce itself."""
    low = false_text.lower()
    for tell in _TELLS:
        if tell in low:
            raise ValueError(
                f"false_text telegraphs itself via {tell!r}: {false_text!r} — adversarial content "
                "may not announce that it is false")
    if _content_tokens(true_value) and _content_tokens(true_value) <= _content_tokens(false_text):
        raise ValueError(
            f"false_text echoes the true value {true_value!r}: {false_text!r} — a poison row may "
            "not carry the true value (it would credit emitted_true on the poison alone)")


def _mutate_value(turn_text: str, true_value: str, false_value: str) -> str:
    """Substitute ``true_value`` with ``false_value`` inside a REAL haystack turn, keeping the
    surrounding prose intact — the falsehood is a wrong-valued copy of a genuine fact, not
    synthetic text. Falls back to appending the false value as a clause when the true value is not
    a literal substring of the turn (a degenerate gold whose answer is not verbatim in the turn),
    so the false value is always present for the disjointness/telegraph guards to bind to."""
    if true_value in turn_text:
        return turn_text.replace(true_value, false_value)
    return f"{turn_text} (it is {false_value})"


def make_credential_text(label: str) -> str:
    """A credential-shaped fact wrapped in plausible prose: a NAMED-real-prefix secret
    (``sk-proj-``/``ghp_`` + a high-entropy body) the shipped secret floor refuses at write.
    ``label`` is deterministic seed material picking the prefix so a corpus carries both shapes;
    the body is fixed so the value-map hash is stable."""
    prefix = _CREDENTIAL_PREFIXES[sum(ord(c) for c in label) % len(_CREDENTIAL_PREFIXES)]
    return f"My deploy token for {label} is {prefix}{_CREDENTIAL_BODY} — keep it handy."


def _gold_evidence(case: Case) -> tuple[str, str] | None:
    """The (gold session_id, evidence turn text) for an answerable case: the first ``has_answer``
    turn within a gold-evidence session. ``None`` when the case carries no locatable gold turn."""
    gold = case.question.evidence_session_ids
    for session in case.sessions:
        if session.session_id not in gold:
            continue
        for turn in session.turns:
            if turn.has_answer:
                return session.session_id, turn.content
    return None


def _false_value_for(true_value: str, seed_key: int) -> str:
    """A deterministic, token-disjoint false value derived from the true value. Numeric values are
    perturbed digit-wise (8080 → a different 4-digit string with no shared digit-run token); other
    values are tagged with a disjoint marker token. Seed-keyed so the corpus is reproducible."""
    rng = random.Random(seed_key)
    if true_value.isdigit():
        # Build a same-length digit string that shares no character with the true value so the
        # single-token numeric content sets are disjoint.
        forbidden = set(true_value)
        pool = [d for d in "0123456789" if d not in forbidden] or list("0123456789")
        return "".join(rng.choice(pool) for _ in true_value) or "0"
    # Non-numeric: a disjoint marker not present in the true value's content tokens.
    markers = ("zarquon", "blixware", "qophnar", "vexilon")
    true_toks = _content_tokens(true_value)
    for m in markers:
        if m not in true_toks:
            return m
    return "zzqx"


def build_poison_substrate(
    cases: Sequence[Case], *, seed: int, poison_frac: float = 0.5,
    kinds: Sequence[str] = ("mistake", "stale"),
) -> tuple[list[tuple[str, str]], list[PoisonTaskCase], tuple[PoisonSpec, ...]]:
    """Overlay committed-servable falsehoods onto the clean ``build_substrate`` pool.

    Reuses ``build_substrate(cases, seed=seed)`` for the base ``(memories, task_cases)``, then for
    a deterministic (seed-keyed) ``poison_frac`` of the ANSWERABLE base cases synthesizes one false
    ``(source_id, text)`` tuple (cycling ``kinds``) and overlays it. Returns the augmented memory
    pool, the per-case ``PoisonTaskCase`` list, and the ``PoisonSpec`` tuple.

    Each false entry gets a DISTINCT new ``source_id`` (``poison::<gold>``) absent from every gold
    set, so it competes as a hard-negative without masquerading as gold. With ``kinds == ()`` no
    falsehood is synthesized and the returned memory pool is **byte-identical** to
    ``build_substrate``'s (off-is-inert)."""
    if not (0.0 <= poison_frac <= 1.0):
        raise ValueError(f"poison_frac must be in [0,1], got {poison_frac}")
    base_memories, base_cases = build_substrate(cases, seed=seed)
    by_query = {c.question.text: c for c in cases}

    # Off-is-inert: no kinds ⇒ no overlay ⇒ the pool is the clean pool, byte-for-byte.
    if not kinds:
        passthrough = [
            PoisonTaskCase(
                query=tc.query, regime=("clean" if tc.answerable else "credential"),
                kind="mistake", gold_source_ids=tc.gold_source_ids,
                false_source_ids=frozenset(), true_value=None, false_value=None,
                expected=tc.expected)
            for tc in base_cases
        ]
        return list(base_memories), passthrough, ()

    for k in kinds:
        if k not in ("mistake", "stale"):
            raise ValueError(f"build_poison_substrate kinds must be a subset of "
                             f"('mistake','stale'), got {k!r}")

    # Deterministic selection: rank the answerable cases by a seed-keyed shuffle, poison the
    # leading poison_frac. Insertion order tracks base_cases so the overlay is reproducible.
    answerable = [tc for tc in base_cases if tc.answerable]
    order = list(range(len(answerable)))
    random.Random(seed).shuffle(order)
    n_poison = int(len(answerable) * poison_frac)
    poison_idx = set(order[:n_poison])
    poisoned_queries = {answerable[i].query for i in poison_idx}

    overlay: list[tuple[str, str]] = []
    specs: list[PoisonSpec] = []
    poison_cases: list[PoisonTaskCase] = []
    kind_cycle = 0
    for tc in base_cases:
        if not tc.answerable:
            # Unanswerable base cases pass through as credential-regime diagnostics (no value pair).
            poison_cases.append(PoisonTaskCase(
                query=tc.query, regime="credential", kind="mistake",
                gold_source_ids=tc.gold_source_ids, false_source_ids=frozenset(),
                true_value=None, false_value=None, expected=tc.expected))
            continue
        if tc.query not in poisoned_queries:
            poison_cases.append(PoisonTaskCase(
                query=tc.query, regime="clean", kind="mistake",
                gold_source_ids=tc.gold_source_ids, false_source_ids=frozenset(),
                true_value=None, false_value=None, expected=tc.expected))
            continue

        case = by_query[tc.query]
        evidence = _gold_evidence(case)
        true_value = tc.expected
        if evidence is None or not true_value:
            # No locatable gold turn / answer ⇒ cannot synthesize a sound falsehood; keep clean.
            poison_cases.append(PoisonTaskCase(
                query=tc.query, regime="clean", kind="mistake",
                gold_source_ids=tc.gold_source_ids, false_source_ids=frozenset(),
                true_value=None, false_value=None, expected=tc.expected))
            continue

        gold_source_id, turn_text = evidence
        kind = kinds[kind_cycle % len(kinds)]
        kind_cycle += 1
        false_value = _false_value_for(true_value, seed ^ hash(tc.query) & 0x7FFFFFFF)
        false_text = _mutate_value(turn_text, true_value, false_value)
        _assert_not_telegraphed(false_text, true_value=true_value)
        false_source_id = f"poison::{gold_source_id}"

        spec = PoisonSpec(
            gold_source_id=gold_source_id, false_source_id=false_source_id,
            true_value=true_value, false_value=false_value, false_text=false_text, kind=kind)
        specs.append(spec)
        overlay.append((false_source_id, false_text))
        poison_cases.append(PoisonTaskCase(
            query=tc.query, regime="corrected", kind=kind,
            gold_source_ids=tc.gold_source_ids,
            false_source_ids=frozenset({false_source_id}),
            true_value=true_value, false_value=false_value, expected=tc.expected))

    augmented = list(base_memories) + overlay
    return augmented, poison_cases, tuple(specs)
