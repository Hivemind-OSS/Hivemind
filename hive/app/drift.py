"""The single owner of the drift wire semantics (served-hit ``drift`` object,
§3.4): the ``hive-edge verify`` → wire verdict mapping, the most-severe-wins
aggregation, and the recall-side ``anchor_drift`` cache lookup — consumed by the
MCP recall/write handlers (``attach_drift``) and by the sync materializer
(``wire_verdict`` — whatever lands in the cache is ALREADY wire vocabulary;
nothing downstream re-maps it).

Wire vocabulary (one enum, most-severe first)::

    anchor_missing > anchor_changed > blast_radius_changed > branch_scoped
                   > unverifiable > fresh;   n/a = general (nothing to verify)

The verify→wire mapping's STALE arm is exhaustive by construction: ``_STALE_ARROWS``
names every engine reason ``hive.combdrift.verdict`` can classify stale —
``signature_changed → anchor_changed``, ``symbol_missing → anchor_missing``,
``file_missing → anchor_missing`` — and a static ratchet derives that set from the
engine at test time, so a stale reason added there without a decided wire arrow
cannot pass. The final fall-through stays as the fail-safe for a reason this server
genuinely does not know, never as a silent bucket for one nobody enumerated.

Verdict source at recall: the materialized ``anchor_drift`` cache at the TIP of
the queried ref (``name@branch``) else the repo's canonical ref. The canonical
tip is the sync watermark meta key ``sync:<repo>:last_tip``; a queried
non-canonical branch resolves its tip from ``ref_tips`` — the materializer's
per-ref watermark, written as soon as recall's demand pulls that ref onto the
work list, before the tip's verdicts are even computed. An unresolved branch
(never yet materialized, or since deleted) degrades to ``unverifiable`` and
records demand in ``ref_requests`` (the materializer's work list), so coverage
follows use. Every miss is fail-safe: unknown tip, un-materialized anchor,
out-of-vocabulary cache row, unparseable hit shape — all read ``unverifiable``,
never false-fresh, never false-stale.

``branch_scoped`` is the one member computed at SERVE time rather than written
to the cache. Every served anchor also carries the memory's own DECLARED ref
for that repo (from ``episode_refs``, empty when canonical); ``branch_route_verdict``
compares that declared ref against the ref the tip above was actually resolved
at, and routes a stale-tier verdict (``anchor_missing``/``anchor_changed``/
``blast_radius_changed``) read off a DIFFERENT line down to the advisory
``branch_scoped`` — never upgrading, never touching a ``fresh``/``unverifiable``
base. A verdict read on the memory's own declared line always rides verbatim;
softening it would turn the declared ref into caller-asserted immunity.

``attach_drift`` is a fail-open ENRICHMENT of the read path: any reader fault
degrades that hit to ``unverifiable``; the read itself never breaks.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from typing import Any

from hive.app.anchors import split_scope
from hive.app.sync_keys import canonical_tip_key

# The "anchor moved" tier is IMPORTED, never re-spelled here: one tier, two
# policies — it qualifies a retirement in the gate and it is the subset
# ``branch_route_verdict`` may soften to ``branch_scoped`` for an off-line
# consumer. It lives in hive/domain/ because the dependency rule runs
# app → domain and never the reverse.
from hive.domain.retirement import QUALIFYING_DRIFT

_log = logging.getLogger("hive.drift")

# ── the wire vocabulary (§3.4, normative) ─────────────────────────────────────
DRIFT_FRESH = "fresh"
DRIFT_ANCHOR_CHANGED = "anchor_changed"
DRIFT_ANCHOR_MISSING = "anchor_missing"
DRIFT_BLAST_RADIUS_CHANGED = "blast_radius_changed"
DRIFT_BRANCH_SCOPED = "branch_scoped"
DRIFT_UNVERIFIABLE = "unverifiable"
DRIFT_NA = "n/a"

#: Every verdict a served ``drift.type`` may carry.
WIRE_VERDICTS = (
    DRIFT_FRESH,
    DRIFT_ANCHOR_CHANGED,
    DRIFT_ANCHOR_MISSING,
    DRIFT_BLAST_RADIUS_CHANGED,
    DRIFT_BRANCH_SCOPED,
    DRIFT_UNVERIFIABLE,
    DRIFT_NA,
)

#: Per-anchor verdicts, most-severe FIRST (aggregation = min index wins).
SEVERITY_ORDER = (
    DRIFT_ANCHOR_MISSING,
    DRIFT_ANCHOR_CHANGED,
    DRIFT_BLAST_RADIUS_CHANGED,
    DRIFT_BRANCH_SCOPED,
    DRIFT_UNVERIFIABLE,
    DRIFT_FRESH,
)

_SEVERITY_INDEX = {v: i for i, v in enumerate(SEVERITY_ORDER)}
_UNVERIFIABLE_IDX = _SEVERITY_INDEX[DRIFT_UNVERIFIABLE]

# ── hive-edge verify → wire mapping (§3.4; the stale arm is EXHAUSTIVE) ───────

#: Every engine reason ``hive.combdrift.verdict`` can classify ``stale``, placed
#: EXPLICITLY. Prefix-matched in order (a reason may carry a ``": <detail>"``
#: suffix). The table is not a convenience — it IS the anti-fork guarantee: the
#: engine owns "which reasons are stale" and this module owns "what a stale reason
#: means on the wire", and a two-arm if-chain over a silent catch-all let those two
#: owners disagree with no test able to notice (BUG-078: a deleted FILE — the
#: strongest evidence an anchor is dead — degraded to the one verdict that never
#: qualifies retirement, while a deleted SYMBOL qualified). A static ratchet
#: derives the engine's stale set at test time and requires a member here for each,
#: so adding a stale reason to the engine without deciding its wire arrow cannot
#: pass the suite. The tiers agree: a gone file and a gone symbol are the same
#: claim — the thing this memory names is not there.
_STALE_ARROWS: tuple[tuple[str, str], ...] = (
    ("signature_changed", DRIFT_ANCHOR_CHANGED),
    ("symbol_missing", DRIFT_ANCHOR_MISSING),
    ("file_missing", DRIFT_ANCHOR_MISSING),
)


def wire_verdict(state: object, reason: object = "") -> str:
    """Map one ``hive-edge verify`` per-anchor result onto the wire enum.

    ``current → fresh``; ``radius changed → blast_radius_changed``;
    ``branch_scoped → branch_scoped``; a ``stale`` state resolves through
    ``_STALE_ARROWS`` — ``signature_changed → anchor_changed``,
    ``symbol_missing → anchor_missing``, ``file_missing → anchor_missing`` — and
    everything else is ``unverifiable`` (the fail-safe arm: an unknown state, an
    UNRECOGNIZED stale reason, an incomparable token version, a non-string —
    silence, never false-stale). The stale set is ratchet-enforced against the
    engine, so "unknown" here means genuinely unknown (a hostile cache row, an
    older engine's output, a future reason a rolling upgrade has not taught this
    server) rather than merely unenumerated.

    ``state`` may carry the reason embedded as ``"stale/<reason>"`` (the table's
    own notation) or split across ``(state, reason)``; reasons match by code
    prefix (``signature_changed...``), the verify engine's own convention. Total
    over ``object``. // O(1)."""
    if not isinstance(state, str):
        return DRIFT_UNVERIFIABLE
    if state == "current":
        return DRIFT_FRESH
    head, _sep, embedded = state.partition("/")
    if head == "stale":
        code = embedded or (reason if isinstance(reason, str) else "")
        for prefix, verdict in _STALE_ARROWS:
            if code.startswith(prefix):
                return verdict
        # marker: this is the FAIL-SAFE for a reason this server does not know —
        # deleting _STALE_ARROWS and returning unconditionally here generalizes
        # BUG-078 to every stale tier at once.
        return DRIFT_UNVERIFIABLE
    if state in ("radius_changed", "radius changed"):
        return DRIFT_BLAST_RADIUS_CHANGED
    if state == "branch_scoped":
        return DRIFT_BRANCH_SCOPED
    return DRIFT_UNVERIFIABLE


# ── declared-line routing (§3.4; branch_scoped computed at READ time) ────────


def branch_route_verdict(base: str, *, declared_ref: str, consumer_ref: str) -> str:
    """Route a materialized verdict through the memory's DECLARED line.

    A memory that names its line is not stale to a consumer on another line — it is
    scoped elsewhere. When both refs are known, differ, and ``base`` is stale-tier,
    the served verdict is the advisory ``branch_scoped``. Every other combination
    (no declared ref, same line, unknown consumer, ``fresh``/``unverifiable`` base)
    returns ``base`` verbatim. Pure, total, and NEVER upgrades to ``fresh`` — the
    most-advisory verdict this can emit is ``branch_scoped``. // O(1)."""
    if (
        declared_ref
        and consumer_ref
        and declared_ref != consumer_ref
        and base in QUALIFYING_DRIFT
    ):
        return DRIFT_BRANCH_SCOPED
    return base


# ── most-severe-wins aggregation (§3.4; the CT-4 property surface) ────────────


def _severity_index(verdict: object) -> int:
    """A member outside the six per-anchor verdicts counts as ``unverifiable``
    (fail-safe: a hostile member can never read fresh; ``n/a`` is an AGGREGATE
    verdict, not a per-anchor one, so it coerces too)."""
    if not isinstance(verdict, str):
        return _UNVERIFIABLE_IDX  # non-string (incl. unhashable) member
    return _SEVERITY_INDEX.get(verdict, _UNVERIFIABLE_IDX)


def aggregate_verdicts(verdicts: Iterable[object]) -> str:
    """Fold per-anchor verdicts into the ONE served ``drift.type``.

    Empty (a general / scope-only memory) → ``n/a``; otherwise the most severe
    member wins under ``SEVERITY_ORDER`` — so ``fresh`` aggregates ONLY from an
    all-fresh multiset (a partially-unverifiable memory can never read fresh).
    Total, permutation-invariant, idempotent. // O(n)."""
    best: int | None = None
    for verdict in verdicts:
        index = _severity_index(verdict)
        best = index if best is None else min(best, index)
    return DRIFT_NA if best is None else SEVERITY_ORDER[best]


# ── the fail-open recall-side enrichment ──────────────────────────────────────


def _as_wire(verdict: object) -> str:
    """A cache row rides verbatim ONLY while inside the per-anchor vocabulary;
    anything else serves as ``unverifiable`` (the wire shape promises the enum)."""
    if isinstance(verdict, str) and verdict in _SEVERITY_INDEX:
        return verdict
    return DRIFT_UNVERIFIABLE


def _meta_value(store: object, key: str) -> str | None:
    """Read one meta kv (the sync/census_health raw-read idiom); None if absent."""
    row = store.conn.execute(  # type: ignore[attr-defined]
        "SELECT value FROM meta WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return None
    value = row["value"]
    return value if isinstance(value, str) and value else None


def tip_for(store: object, repo: str, ref: str) -> str | None:
    """The tip to judge ``repo`` at: ``ref_tips(repo, ref)`` for a non-canonical
    ref, else the canonical watermark ``sync:<repo>:last_tip``. None = unknown tip
    (the fail-safe: the caller reads unverifiable, never false-fresh).

    The SINGLE owner of "which tip do I judge repo R at", shared by the recall
    enrichment here and the retirement gate's drift feed at the MCP boundary."""
    if not ref:
        return _meta_value(store, canonical_tip_key(repo))
    tip = store.ref_tip(repo, ref)  # type: ignore[attr-defined]
    return tip if isinstance(tip, str) and tip else None


def _queried_refs(queried_repos: Iterable[object]) -> dict[str, str]:
    """The query's explicit ``name@branch`` routing → ``{name: branch}``.

    Accepts the raw MCP ``repos`` strings (already boundary-validated by
    ``normalize_repos``) or its parsed ``(name, branch)`` pairs; entries without
    a branch route canonical and are omitted. Hostile members are skipped —
    this map only ever REMOVES trust (branch routing degrades, §3.4)."""
    out: dict[str, str] = {}
    for entry in queried_repos:
        if isinstance(entry, str) and entry:
            name, branch = split_scope(entry)
        elif (
            isinstance(entry, (tuple, list))
            and len(entry) == 2
            and all(isinstance(part, str) for part in entry)
        ):
            name, branch = entry
        else:
            continue
        if name and branch:
            out[name] = branch
    return out


def _drift_for_hit(
    hit: dict[str, Any],
    store: object,
    canonical: dict[str, str],
    queried_ref: dict[str, str],
) -> dict[str, Any]:
    """One hit's ``drift`` object — most-severe across its anchors, each judged
    at the tip of its repo's queried ref else canonical, then routed through
    the memory's OWN declared line for that repo (``branch_route_verdict``): a
    stale-tier verdict read off a line the memory never named degrades to the
    advisory ``branch_scoped``, while a verdict read on the memory's own
    declared line always rides verbatim. Raises freely; ``attach_drift`` owns
    the fail-open."""
    raw_anchors = hit.get("anchors")
    if not isinstance(raw_anchors, (list, tuple)) or not raw_anchors:
        return {"type": DRIFT_NA, "detail": {"per_anchor": []}}

    # per-repo resolution: (tip | None, ref-if-branch-routed), one drift_get per repo
    parsed: list[
        tuple[str, str, str]
    ] = []  # (repo, anchor, declared_ref); "" = unparseable
    for item in raw_anchors:
        if (
            isinstance(item, dict)
            and isinstance(item.get("repo"), str)
            and isinstance(item.get("anchor"), str)
        ):
            raw_ref = item.get("ref", "")
            declared_ref = raw_ref if isinstance(raw_ref, str) else ""
            parsed.append((item["repo"], item["anchor"], declared_ref))
        else:
            parsed.append(("", "", ""))  # contributes unverifiable below
    tips: dict[str, str | None] = {}
    branch_of: dict[str, str] = {}
    rows: dict[str, dict[str, tuple[str, str]]] = {}
    for repo in {r for r, _a, _d in parsed if r}:
        branch = queried_ref.get(repo, "")
        routed = bool(branch) and branch != canonical.get(repo, "")
        if routed:
            branch_of[repo] = branch
        tips[repo] = tip_for(store, repo, branch if routed else "")
        if tips[repo] is not None:
            anchors = [a for r, a, _d in parsed if r == repo]
            rows[repo] = store.drift_get(repo, tips[repo], anchors)  # type: ignore[attr-defined]

    per_anchor: list[dict[str, Any]] = []
    routed_ref = ""
    for repo, anchor, declared_ref in parsed:
        entry: dict[str, Any] = {"repo": repo, "anchor": anchor}
        if repo in branch_of and not routed_ref:
            routed_ref = branch_of[repo]  # rides regardless of resolution below
        tip = tips.get(repo)
        if repo and tip is not None:
            entry["tip_sha"] = tip  # tip resolved — honest even on a miss
            cached = rows.get(repo, {}).get(anchor)
            base = (
                _as_wire(cached[0])
                if isinstance(cached, (tuple, list)) and cached
                else DRIFT_UNVERIFIABLE
            )
            consumer_ref = branch_of.get(repo) or canonical.get(repo, "")
            entry["verdict"] = branch_route_verdict(
                base, declared_ref=declared_ref, consumer_ref=consumer_ref
            )
        else:
            entry["verdict"] = DRIFT_UNVERIFIABLE
        per_anchor.append(entry)

    detail: dict[str, Any] = {"per_anchor": per_anchor}
    if routed_ref:
        detail["ref"] = routed_ref  # the queried ref rides drift.detail.ref
    return {
        "type": aggregate_verdicts([e["verdict"] for e in per_anchor]),
        "detail": detail,
    }


def attach_drift(
    hits: Sequence[dict[str, Any]],
    *,
    store: object,
    queried_repos: Iterable[object] = (),
    now: int = 0,
) -> Sequence[dict[str, Any]]:
    """Attach the §3.4 ``drift`` object to every served hit, in place; returns
    ``hits``. FAIL-OPEN: no fault in the registry, the tip read, the cache read,
    or the demand touch ever raises into the read path — a faulting hit degrades
    to ``unverifiable``, a general hit (no anchors, reader never consulted)
    keeps its intrinsic ``n/a``.

    ``store`` is duck-typed (the SqliteEpisodeStore surface): ``repo_registry()``,
    ``drift_get(repo, tip_sha, anchors)``, ``ref_tip(repo, ref)``,
    ``touch_ref_request(repo, ref, ts)``, and ``conn`` for the watermark meta
    read. ``queried_repos`` is the recall call's validated ``repos`` argument
    (raw strings or parsed pairs); ``now`` stamps the demand touch.
    // O(hits · anchors)."""
    try:
        canonical = {row.name: row.canonical_ref for row in store.repo_registry()}  # type: ignore[attr-defined]
    except Exception:
        _log.debug("drift.registry_read_failed", exc_info=True)
        canonical = {}
    queried_ref = _queried_refs(queried_repos or ())

    # Demand: a queried non-canonical branch wants materialization at ITS tip —
    # recorded per QUERY (demand is the query, not the hit), fail-open write.
    for name, branch in queried_ref.items():
        if branch == canonical.get(name, "") or (name not in canonical and canonical):
            continue  # canonical routing / unregistered name
        try:
            store.touch_ref_request(name, branch, int(now))  # type: ignore[attr-defined]
        except Exception:
            _log.debug(
                "drift.ref_request_touch_failed repo=%s ref=%s",
                name,
                branch,
                exc_info=True,
            )

    for hit in hits:
        if not isinstance(hit, dict):
            continue  # nothing to enrich, never a fault
        try:
            hit["drift"] = _drift_for_hit(hit, store, canonical, queried_ref)
        except Exception:
            _log.debug("drift.attach_degraded", exc_info=True)
            hit["drift"] = {"type": DRIFT_UNVERIFIABLE, "detail": {"per_anchor": []}}
    return hits
