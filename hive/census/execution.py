"""Census-side owner of the execution-producer seam (fail-closed).

One module owns the whole boundary with the hive.verifier producer (classes
1-2: typecheck, tests): the input projection (`verifier_touched_set`), the
invocation (`run_verifier`), and the result coercion (`coerce_execution` /
`coerce_verifier_stamp`). The producer's imports stay lazy — the module idiom
for engine-adjacent packages — but the census wheel depends on the verifier
wheel: the producer is part of the front door, never an optional extra.

The result is consumed through its contract shape, never its package: every
field is read via defensive getattr with a default, additive unknown fields
are tolerated, and anything outside the contract coerces toward abstention —
an unknown state becomes "errored", an absent producer becomes "not_run", and
neither can ever read as a pass because the carrier makes an undecided state
with a deciding tag unconstructable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from hive.census.diff import Change, ChangeSet
from hive.census.engines import GraphPair

if TYPE_CHECKING:  # annotation target only; the runtime import stays lazy
    import hive.verifier

RUN_STATES: frozenset[str] = frozenset({"passed", "failed", "not_run", "errored"})

_DECIDED_STATES = frozenset({"passed", "failed"})
_TAGS = frozenset({"machine-checked", "bounded-estimate", "unverified"})

REASON_PRODUCER_ABSENT = "producer-absent"
REASON_MISSING_CLASS = "missing-class"
_UNRECOGNIZED_STATE = "unrecognized-state:"

# Statuses whose files carry no usable post-image lines for the verifier.
_NO_POST_IMAGE_STATUSES = frozenset({"deleted", "binary"})


@dataclass(frozen=True, slots=True)
class ExecutionClassLine:
    cls: Literal["typecheck", "tests"]
    state: str  # coerced member of RUN_STATES
    passed: int
    failed: int
    errored: int
    tag: str
    reason: str
    runners: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.state not in RUN_STATES:
            raise ValueError(f"unknown run state {self.state!r}")
        if self.tag not in _TAGS:
            raise ValueError(f"unknown execution tag {self.tag!r}")
        if self.state not in _DECIDED_STATES and self.tag != "unverified":
            raise ValueError(
                "an undecided class cannot carry a deciding tag: "
                f"state={self.state!r} requires tag='unverified', got {self.tag!r}"
            )
        if self.passed < 0 or self.failed < 0 or self.errored < 0:
            raise ValueError("execution counts must be >= 0")


def _count(value: object) -> int:
    try:
        count = int(value)  # type: ignore[call-overload]
    except (TypeError, ValueError):
        return 0
    return count if count >= 0 else 0


def _runners(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):  # one runner name, never a character iterable
        return (value,)
    try:
        # Optimistic duck-iteration: the except IS the type gate (a non-iterable
        # raises TypeError there), so the cast asserts nothing the try does not
        # already check at runtime.
        return tuple(str(item) for item in cast(Iterable[object], value))
    except TypeError:
        return ()


def _absent_line(cls: Literal["typecheck", "tests"]) -> ExecutionClassLine:
    return ExecutionClassLine(
        cls=cls,
        state="not_run",
        passed=0,
        failed=0,
        errored=0,
        tag="unverified",
        reason=REASON_PRODUCER_ABSENT,
        runners=(),
    )


def _coerce_class(
    cls: Literal["typecheck", "tests"], class_obj: object | None, *, authoritative: bool
) -> ExecutionClassLine:
    if class_obj is None:
        return ExecutionClassLine(
            cls=cls,
            state="errored",
            passed=0,
            failed=0,
            errored=0,
            tag="unverified",
            reason=REASON_MISSING_CLASS,
            runners=(),
        )
    raw_state = getattr(class_obj, "state", None)
    if isinstance(raw_state, str) and raw_state in RUN_STATES:
        state = raw_state
        raw_reason = getattr(class_obj, "reason", None)
        reason = str(raw_reason) if raw_reason is not None else ""
    else:
        state = "errored"
        reason = f"{_UNRECOGNIZED_STATE}{raw_state}"
    if state in _DECIDED_STATES:
        tag = "machine-checked" if authoritative else "bounded-estimate"
    else:
        tag = "unverified"
    return ExecutionClassLine(
        cls=cls,
        state=state,
        passed=_count(getattr(class_obj, "passed", 0)),
        failed=_count(getattr(class_obj, "failed", 0)),
        errored=_count(getattr(class_obj, "errored", 0)),
        tag=tag,
        reason=reason,
        runners=_runners(getattr(class_obj, "runners", ())),
    )


def coerce_execution(
    result: object | None, *, authoritative: bool = False
) -> tuple[ExecutionClassLine, ExecutionClassLine]:
    """Coerce a contract-shaped verification result into frozen census lines.

    None means no producer ran at all: both classes surface as not_run with
    the producer-absent reason. A decided state (passed/failed) tags as
    machine-checked only when the caller vouches the run was authoritative;
    the default scoped run is a bounded estimate. Abstentions (not_run,
    errored, anything unrecognizable) always tag unverified.
    """
    if result is None:
        return (_absent_line("typecheck"), _absent_line("tests"))
    return (
        _coerce_class(
            "typecheck", getattr(result, "typecheck", None), authoritative=authoritative
        ),
        _coerce_class(
            "tests", getattr(result, "tests", None), authoritative=authoritative
        ),
    )


def coerce_verifier_stamp(result: object | None) -> dict[str, Any] | None:
    """Extract the producer's tool-version provenance block, or omit it.

    Any missing field omits the whole block (under-claim, never a guess).
    """
    tool_version = getattr(result, "tool_version", None)
    if tool_version is None:
        return None
    head_sha = getattr(tool_version, "head_sha", None)
    base_sha = getattr(tool_version, "base_sha", None)
    # Any, not Any | None: the duck-read pair-iteration below is guarded by the
    # None-membership check plus the except (iterating None raises TypeError).
    tool_versions: Any = getattr(tool_version, "tool_versions", None)
    registry_version = getattr(tool_version, "registry_version", None)
    if None in (head_sha, base_sha, tool_versions, registry_version):
        return None
    try:
        pairs = [[str(name), str(version)] for name, version in tool_versions]
    except (TypeError, ValueError):
        return None
    return {
        "head_sha": str(head_sha),
        "base_sha": str(base_sha),
        "tool_versions": pairs,
        "registry_version": str(registry_version),
    }


def run_verifier(
    change: Change, graphs: GraphPair, *, depth: int, authoritative: bool
) -> hive.verifier.VerifyResult:
    """Run the live execution producer over the HEAD side of the change.

    Head worktree + head graph, never split: the projection is head-side by
    construction, an added-at-head file has no base node (blast_radius fails
    open on an unresolvable seed), and the affected tests the engine names
    must exist in the tree the spawn runs in. The head side's path-dialect
    offset rides along the same way (BUG-039): the verifier bridges its
    repo-relative seeds against the graph's extraction-root dialect with it.
    The base-side dual lives in the regression join. verify() raises only on
    invalid arguments; every operational failure fails closed INSIDE the
    result.
    """
    from hive.verifier import VerifyOptions, verify

    return verify(
        change.head_tree,
        verifier_touched_set(change.touched),
        base_sha=change.touched.base_sha,
        head_sha=change.touched.head_sha,
        engine=graphs.head,
        opts=VerifyOptions(
            depth=depth,
            authoritative=authoritative,
            root_offset=graphs.head_offset,
        ),
    )


def verifier_touched_set(touched: ChangeSet) -> hive.verifier.TouchedSet:
    """Project the census diff carrier into the verifier's input type.

    Head-side only: the union of added-span lines per file. Deleted and
    binary files carry no usable post-image lines and are excluded.
    """
    from hive.verifier import TouchedFile, TouchedSet

    files = []
    for changed in sorted(touched.files, key=lambda f: f.path):
        if changed.status in _NO_POST_IMAGE_STATUSES:
            continue
        lines = frozenset(
            line for start, end in changed.added_spans for line in range(start, end + 1)
        )
        files.append(TouchedFile(path=changed.path, lines=lines))
    return TouchedSet(files=tuple(files))
