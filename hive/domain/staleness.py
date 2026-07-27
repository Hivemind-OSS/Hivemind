"""The staleness verdict ladder — the PURE decision half of anchor drift.

The prober (``hive.app.sync``) asks git two questions per distinct baseline — which
paths existed at the baseline commit, and what the range ``base..tip`` did to each —
plus, ONCE per symbol binding, whether that symbol resolved at its own baseline, and,
for a symbol anchor whose file actually moved, whether the symbol's OWN lines changed.
This module turns those observations into the served wire verdict. It holds no git, no
store and no clock: the whole truth table is exercisable in microseconds with zero
subprocesses.

Direction (Law 6): **fail-CLOSED to ``unverifiable``.** Every unknown, ambiguity and
probe fault under-claims, so this function can WITHHOLD a retirement but can never
grant one. ``fresh`` is a positive claim and is reachable only from observations that
all actually happened — the path existed at the baseline, the symbol (when the anchor
names one) resolved there, and the range left the path or the symbol's own lines
alone.

PURE: stdlib only. ``decide`` is TOTAL over any ``AnchorFacts`` and never raises.
"""

from __future__ import annotations

from dataclasses import dataclass

# ── the wire vocabulary this ladder emits ─────────────────────────────────────
# Spelled here rather than imported from hive.app.drift because the dependency rule
# runs app -> domain and never the reverse; `tests/app/test_drift.py` pins the two
# spellings equal, so they cannot fork.
FRESH = "fresh"
ANCHOR_CHANGED = "anchor_changed"
ANCHOR_MISSING = "anchor_missing"
UNVERIFIABLE = "unverifiable"

# ── detail reasons (cache-only strings, never wire verdicts) ──────────────────
BASE_UNKNOWN = "base_unknown"
PATH_UNSEEN = "path_absent_at_baseline"
SYMBOL_UNSEEN = "symbol_absent_at_baseline"
PROBE_FAILED = "probe_failed"

#: How many commit SHAs ride a served hit as evidence. The full count rides beside
#: them as ``n_commits``, so a capped list never misrepresents the range.
COMMIT_EVIDENCE_CAP = 10

#: ``git diff --name-status`` letters that mean the named path LEFT the tree between
#: the baseline and the judged tip. ``R``/``C`` carry a similarity score suffix.
_GONE_STATUS = ("D", "R")


@dataclass(frozen=True, slots=True)
class AnchorFacts:
    """Everything the prober learned about ONE anchor.

    Frozen with fail-safe defaults — an unfilled field means "unknown", which the
    ladder reads as ``unverifiable``. A caller that forgets to wire an observation
    therefore under-claims; it can never manufacture a verdict. "Fail-safe" is per
    field, not a uniform polarity: an unwired ``symbol_resolved_at_base`` must read
    "never observed" (False ⇒ ``unverifiable``), while an unwired
    ``symbol_resolved_at_tip`` must read "still there" (True), because False there is
    the ONE positive claim in the carrier — it is what grants ``anchor_missing``."""

    base_tip: str = ""  # "" = never baselined
    in_base_tree: bool = False  # did `path` exist at base_tip
    status: str = ""  # "" = not in the diff; else A|M|D|R…
    symbol_commits: tuple[str, ...] = ()  # commits touching the SYMBOL's own lines
    path_commits: tuple[str, ...] = ()  # commits touching the whole path
    symbol_resolved_at_tip: bool = True
    symbol_resolved_at_base: bool = False  # did the SYMBOL exist at base_tip
    probe_failed: bool = False


def symbol_lookups(symbol: str) -> tuple[str, ...]:
    """The ordered funcname lookups for one anchor symbol.

    git's ``-L :<funcname>:<path>`` matches the DECLARATION line, so a bare method
    name resolves and a class-qualified one does not — the exact inverse of what the
    fingerprint engine demanded. The fallback is therefore the symbol's last
    ``'.'``-separated segment, tried only after the spelling as written, so both
    ``file.py::repo_remove`` and ``file.py::Store.repo_remove`` reach a real verdict
    instead of one being traded for the other. Pure. // O(len)."""
    if not symbol:
        return ()
    tail = symbol.rpartition(".")[2]
    if not tail or tail == symbol:
        return (symbol,)
    return (symbol, tail)


def _evidence(facts: AnchorFacts) -> dict[str, object]:
    """The stale-tier evidence rider: the baseline drifted from, plus the commit SHAs
    that carried the change (newest first, capped). IDS ONLY — a commit subject is
    unscanned repo text and never leaves the repo (Law 4)."""
    commits = facts.symbol_commits or facts.path_commits
    return {
        "since": facts.base_tip,
        "commits": list(commits[:COMMIT_EVIDENCE_CAP]),
        "n_commits": len(commits),
    }


def decide(facts: AnchorFacts, *, symbol: str) -> tuple[str, dict[str, object]]:
    """``(wire verdict, detail)`` for ONE anchor. TOTAL over any ``AnchorFacts``.

    The ladder, in order — each rung is an observation that actually happened:

    ``probe faulted``            -> unverifiable (git could not answer at all)
    ``no base_tip recorded``     -> unverifiable (never observed)
    ``path not in the base tree``-> unverifiable (prose, or a path that never existed)
    ``symbol absent at baseline``-> unverifiable (never observed to exist)
    ``path not in the diff``     -> fresh        (~all anchors exit here)
    ``status D / R``             -> anchor_missing
    ``file-scoped anchor``       -> anchor_changed + commits
    ``symbol gone at the tip``   -> anchor_missing (it demonstrably WAS at the baseline)
    ``symbol lines untouched``   -> fresh        (the file moved, this symbol did not)
    ``symbol lines moved``       -> anchor_changed + commits

    The baseline-resolution rung sits ABOVE the diff rung on purpose: ``fresh`` is a
    positive claim, and a quiet file is not evidence that a symbol nobody ever saw is
    current. Both of the ladder's symbol clauses therefore hang off the same measured
    fact, so a symbol that never resolved can neither read ``fresh`` nor be called
    missing.

    Direction: fail-CLOSED to ``unverifiable``. // O(commits)."""
    if facts.probe_failed:
        return UNVERIFIABLE, {"reason": PROBE_FAILED}
    if not facts.base_tip:
        return UNVERIFIABLE, {"reason": BASE_UNKNOWN}
    if not facts.in_base_tree:
        # marker: returning `fresh` here reds test_a_path_that_never_existed_is_
        # unverifiable — silence about a path we never saw is not evidence that it
        # is current.
        return UNVERIFIABLE, {"reason": PATH_UNSEEN}
    if symbol and not facts.symbol_resolved_at_base:
        # marker: moving this rung below the `status` check — or dropping it — reds
        # test_a_deleted_target_is_missing_but_an_unresolvable_one_is_not: a symbol
        # never observed at its own baseline would then read `fresh` on a quiet file
        # and `anchor_missing` on a busy one, and the second would qualify a
        # retirement the evidence does not support.
        return UNVERIFIABLE, {"reason": SYMBOL_UNSEEN}
    if not facts.status:
        return FRESH, {}
    if facts.status[:1] in _GONE_STATUS:
        return ANCHOR_MISSING, _evidence(facts)
    if not symbol:
        return ANCHOR_CHANGED, _evidence(facts)
    if not facts.symbol_resolved_at_tip:
        return ANCHOR_MISSING, _evidence(facts)
    if not facts.symbol_commits:
        return FRESH, {}
    return ANCHOR_CHANGED, _evidence(facts)
