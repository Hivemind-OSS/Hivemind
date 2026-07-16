"""The regression join: breaking/removed contract drift ∩ blast radius.

The census's one net-new evidence computation: for every symbol whose drift is
in the regression class, the reverse-reachability lower bound over the BASE
graph names the callers, dependents, and tests built against the old contract
(a removed symbol has no head node at all, so the head graph cannot answer).
Pure over in-memory engine outputs; its lazy matrix imports assume the process
already pinned MATRIX_OUT via `engines.ensure_scratch_matrix_out`.

blast_radius fails OPEN on an unresolvable seed — it returns an empty result
byte-indistinguishable from "resolved, zero callers" — so resolve_seed is
called explicitly first, and None becomes an abstained (unverified) finding,
never an empty machine-trusted impact set. RegressionFinding's invariants make
that confusion unconstructable at the type level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # annotation target only; runtime imports stay lazy
    import combdrift

_REGRESSION_DRIFTS = frozenset({"breaking", "removed"})
_TAGS = frozenset({"bounded-estimate", "unverified"})

REASON_SEED_UNRESOLVED = "seed-unresolved"
REASON_BLAST_LOWER_BOUND = "blast-radius-lower-bound"


@dataclass(frozen=True, slots=True)
class RegressionFinding:
    path: str
    symbol: str
    drift: str  # "breaking" | "removed" only
    seed: str | None  # resolved graph node id, or None when unresolvable
    callers: tuple[str, ...]
    dependents: tuple[str, ...]
    tests: tuple[str, ...]
    depth: int
    tag: str  # "bounded-estimate" | "unverified"
    reason: str

    def __post_init__(self) -> None:
        if self.drift not in _REGRESSION_DRIFTS:
            raise ValueError(
                f"a regression finding covers only {sorted(_REGRESSION_DRIFTS)} "
                f"drift, got {self.drift!r}"
            )
        if self.tag not in _TAGS:
            raise ValueError(f"unknown finding tag {self.tag!r}")
        if (self.seed is None) != (self.tag == "unverified"):
            raise ValueError(
                "seed is None exactly when the finding abstains "
                f"(tag='unverified'); got seed={self.seed!r}, tag={self.tag!r}"
            )
        if self.seed is None and (self.callers or self.dependents or self.tests):
            raise ValueError("an abstained finding carries no impact hits")


def _abstained(
    path: str, symbol: str, drift: str, depth: int, reason: str
) -> RegressionFinding:
    return RegressionFinding(
        path=path,
        symbol=symbol,
        drift=drift,
        seed=None,
        callers=(),
        dependents=(),
        tests=(),
        depth=depth,
        tag="unverified",
        reason=reason,
    )


def _seed_queries(symbol: str) -> tuple[str, ...]:
    # The dotted spelling can miss (method labels are dot-prefixed, e.g.
    # ".wave()"); the label form and the bare member name recover it. The
    # source-file form is deliberately absent: a file owns several nodes, so
    # its unique-match rule never fires.
    member = symbol.rsplit(".", 1)[-1]
    return tuple(dict.fromkeys((symbol, f"{member}()", member)))


def _hit_ids(hits) -> tuple[str, ...]:
    return tuple(str(getattr(hit, "node_id", "") or "") for hit in (hits or ()))


def _join_one(
    g_nx, path: str, symbol: str, drift: str, depth: int
) -> RegressionFinding:
    from matrix.affected import resolve_seed

    seed: str | None = None
    for query in _seed_queries(symbol):
        seed = resolve_seed(g_nx, query)
        if seed is not None:
            break
    if seed is None:
        return _abstained(path, symbol, drift, depth, REASON_SEED_UNRESOLVED)

    from matrix import blast_radius

    blast = blast_radius(g_nx, seed, depth=depth)
    return RegressionFinding(
        path=path,
        symbol=symbol,
        drift=drift,
        seed=seed,
        callers=_hit_ids(blast.callers),
        dependents=_hit_ids(blast.dependents),
        tests=_hit_ids(blast.tests),
        depth=depth,
        tag="bounded-estimate",
        reason=REASON_BLAST_LOWER_BOUND,
    )


def regression_join(
    verdict: combdrift.ChangeVerdict, base_graph, *, depth: int = 2
) -> tuple[RegressionFinding, ...]:
    """Join regression-class drift onto the base graph's blast radius.

    Every cross-engine field is read defensively: a symbol with an unknown
    drift value produces no finding (never a crash), and a symbol whose join
    step raises degrades to its own abstained finding without aborting the
    rest.
    """
    g_nx = getattr(base_graph, "nx", base_graph)
    findings: list[RegressionFinding] = []
    for change in getattr(verdict, "symbols", ()) or ():
        drift = str(getattr(change, "drift", "") or "")
        if drift not in _REGRESSION_DRIFTS:
            continue
        path = str(getattr(change, "path", "") or "")
        symbol = str(getattr(change, "symbol", "") or "")
        try:
            findings.append(_join_one(g_nx, path, symbol, drift, depth))
        except Exception as error:
            findings.append(
                _abstained(
                    path,
                    symbol,
                    drift,
                    depth,
                    f"join-error: {type(error).__name__}: {error}",
                )
            )
    return tuple(findings)
