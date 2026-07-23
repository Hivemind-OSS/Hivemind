"""Contract tests for the full-bundle receipt assembly + self-validation.

The hand-built fixture is the byte contract: a fixed verdict (one breaking +
one additive + one unverifiable symbol), one resolved + one seed-unresolved
finding, and a decided execution pair (typecheck passed, tests failed) with
its verifier stamp — the COMPLETE receipt shape — compared exactly against
the checked-in golden. The decided pair is a constructed contract-shaped
carrier fed through the same coercion the CLI uses (never a live producer:
the golden path stays byte-deterministic; the live path belongs to the e2e
and in-situ tiers). The real-pipeline test builds the same bundle over the
session engine fixture repo — real diff, real graphs, real drift — and pins
that the receipt stays ids-only. matrix imports stay inside fixtures/tests,
after the matrix_scratch pin, because matrix reads MATRIX_OUT once at import
time.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from combdrift import ChangeVerdict, SymbolChange, VerifierVersion

from hive.census import ChangedFile, ChangeSet, RegressionFinding, coerce_execution
from hive.census.envelope import canonical_bytes, unsigned_envelope
from hive.census.execution import coerce_verifier_stamp
from hive.census.memory import MemoryContext
from hive.census.receipt import build_receipt
from hive.census.schema import ReceiptSchemaError

_BASE_SHA = "a" * 40
_HEAD_SHA = "b" * 40

_FIXTURES = Path(__file__).parent / "fixtures"
_GOLDEN = _FIXTURES / "golden_receipt.json"


@dataclass(frozen=True)
class _DecidedClass:
    """Contract-shaped decided class result for the byte-deterministic golden."""

    state: str
    passed: int = 0
    failed: int = 0
    errored: int = 0
    reason: str | None = None
    runners: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ToolVersionStub:
    head_sha: str = _HEAD_SHA
    base_sha: str = _BASE_SHA
    tool_versions: tuple = (("pyright", "1.1.0"), ("pytest", "8.0.0"))
    registry_version: str = "r3"


@dataclass(frozen=True)
class _DecidedResult:
    typecheck: _DecidedClass = _DecidedClass(
        state="passed", passed=2, runners=("pyright",)
    )
    tests: _DecidedClass = _DecidedClass(
        state="failed", passed=1, failed=1, runners=("pytest",)
    )
    tool_version: _ToolVersionStub = _ToolVersionStub()


def _hand_inputs() -> dict:
    """The byte-contract fixture inputs; matrix must already be scratch-pinned."""
    from matrix.version import ModelVersion

    from hive.census.engines import GraphPair

    touched = ChangeSet(
        base_sha=_BASE_SHA,
        head_sha=_HEAD_SHA,
        files=(
            ChangedFile(
                path="pkg/lib.py",
                status="modified",
                added_spans=((1, 6),),
                removed_spans=((1, 9),),
            ),
        ),
    )
    verdict = ChangeVerdict(
        verifier_version=VerifierVersion(
            combdrift_version="0.1.0",
            fingerprint_version="1",
            head_sha=_HEAD_SHA,
            base_sha=_BASE_SHA,
        ),
        base_sha=_BASE_SHA,
        head_sha=_HEAD_SHA,
        symbols=(
            SymbolChange(
                path="pkg/lib.py",
                symbol="greet",
                existed_before=True,
                exists_after=True,
                drift="breaking",
                old_fingerprint="cd1:function(name,punct)",
                new_fingerprint="cd1:function(name)",
                reason="signature_changed: required parameter removed: punct",
            ),
            SymbolChange(
                path="pkg/lib.py",
                symbol="helper",
                existed_before=True,
                exists_after=True,
                drift="additive",
                old_fingerprint="cd1:function(x)",
                new_fingerprint="cd1:function(x,y=None)",
                reason="ok",
            ),
            SymbolChange(
                path="pkg/lib.py",
                symbol="mystery",
                existed_before=True,
                exists_after=True,
                drift="indeterminate",
                old_fingerprint=None,
                new_fingerprint=None,
                reason="symbol_indirect: re-export or dynamic binding",
            ),
        ),
        verdict="stale",
    )
    findings = (
        RegressionFinding(
            path="pkg/lib.py",
            symbol="greet",
            drift="breaking",
            seed="pkg/lib.py::greet()",
            callers=("pkg/app.py::caller()",),
            dependents=("pkg/app.py",),
            tests=("tests/test_lib.py::test_greet()",),
            depth=2,
            tag="bounded-estimate",
            reason="blast-radius-lower-bound",
        ),
        RegressionFinding(
            path="pkg/lib.py",
            symbol="Ghost.gone",
            drift="removed",
            seed=None,
            callers=(),
            dependents=(),
            tests=(),
            depth=2,
            tag="unverified",
            reason="seed-unresolved",
        ),
    )
    graphs = GraphPair(
        base=None,
        head=None,
        base_stamp=ModelVersion(
            graph_sha256="c" * 64,
            commit_sha=_BASE_SHA,
            engine_version="0.1.0",
            built_at="2026-07-06T00:00:00+00:00",
            node_count=10,
            edge_count=12,
        ),
        head_stamp=ModelVersion(
            graph_sha256="d" * 64,
            commit_sha=_HEAD_SHA,
            engine_version="0.1.0",
            built_at="2026-07-06T00:00:01+00:00",
            node_count=9,
            edge_count=11,
        ),
    )
    decided = _DecidedResult()
    return dict(
        touched=touched,
        verdict=verdict,
        findings=findings,
        execution=coerce_execution(decided),
        verifier_stamp=coerce_verifier_stamp(decided),
        graphs=graphs,
        precision_budget=0.9,
        depth=2,
    )


@pytest.fixture(scope="module")
def hand_inputs(matrix_scratch) -> dict:
    return _hand_inputs()


@pytest.fixture(scope="module")
def hand_receipt(hand_inputs: dict) -> dict:
    return build_receipt(**hand_inputs)


def test_golden_full_bundle_receipt(hand_receipt: dict) -> None:
    golden = json.loads(_GOLDEN.read_text(encoding="utf-8"))
    assert hand_receipt == golden


def test_unverifiable_symbol_tag_is_single_sourced(hand_receipt: dict) -> None:
    mystery = [
        line
        for line in hand_receipt["lines"]
        if line["subject"] == "pkg/lib.py::mystery"
    ]
    assert len(mystery) == 2  # existence + contract
    assert all(line["tag"] == "unverified" for line in mystery)
    greet_node = [
        line
        for line in hand_receipt["lines"]
        if line["subject"] == "pkg/lib.py::greet"
        and line["class"] in ("existence", "contract")
    ]
    assert len(greet_node) == 2
    assert all(line["tag"] == "machine-checked" for line in greet_node)
    # The raw reason (with its ": <detail>" suffix) rides the line untrimmed.
    contract = next(line for line in greet_node if line["class"] == "contract")
    assert contract["reason"] == "signature_changed: required parameter removed: punct"


def test_class4_differential_line_always_present_never_faked(
    hand_receipt: dict,
) -> None:
    (line,) = [ln for ln in hand_receipt["lines"] if ln["class"] == "differential"]
    assert line["tag"] == "not-run"
    assert line["reason"] == "no-producer"
    assert "gating" not in line  # its vacuous precision entry gates


def test_below_budget_classes_are_shown_not_gated(hand_receipt: dict) -> None:
    # Node/edge/regression sit below the 0.9 budget; the fully decided
    # execution pair (2/2) gates, so its lines ride unstamped like the
    # vacuous differential.
    for line in hand_receipt["lines"]:
        if line["class"] in ("differential", "typecheck", "tests"):
            assert "gating" not in line
        else:
            assert line["gating"] is False
    entries = {entry["class"]: entry for entry in hand_receipt["precision"]}
    assert entries["node"]["gating"] is False
    assert entries["execution"]["gating"] is True
    assert entries["differential"]["gating"] is True


def test_above_budget_class_lines_ride_unstamped(hand_inputs: dict) -> None:
    receipt = build_receipt(**{**hand_inputs, "precision_budget": 0.7})
    exec_lines = [
        line for line in receipt["lines"] if line["class"] in ("typecheck", "tests")
    ]
    assert exec_lines and all("gating" not in line for line in exec_lines)  # 2/2 >= 0.7
    node_lines = [
        line for line in receipt["lines"] if line["class"] in ("existence", "contract")
    ]
    assert node_lines and all(
        line["gating"] is False for line in node_lines
    )  # 2/3 < 0.7


def test_precision_entries_carry_decided_fractions(hand_receipt: dict) -> None:
    expected = {
        "node": (2, 3),  # indeterminate is undecided
        "edge": (1, 2),  # one seed resolved
        "regression": (1, 2),
        "execution": (2, 2),  # both classes decided
        "differential": (0, 0),
    }
    entries = {entry["class"]: entry for entry in hand_receipt["precision"]}
    assert set(entries) == set(expected)
    for cls, (decided, total) in expected.items():
        assert (entries[cls]["decided"], entries[cls]["total"]) == (decided, total)
    assert entries["node"]["value"] == 2 / 3
    assert entries["execution"]["value"] == 1.0
    assert entries["differential"]["value"] is None


def test_provenance_binds_shas_versions_and_both_stamps(hand_receipt: dict) -> None:
    provenance = hand_receipt["provenance"]
    assert provenance["base_sha"] == _BASE_SHA
    assert provenance["head_sha"] == _HEAD_SHA
    assert provenance["schema_version"] == "v0"
    assert provenance["precision_model"] == "v0-decided-fraction"
    assert provenance["blast_depth"] == 2
    assert provenance["combdrift"] == {
        "combdrift_version": "0.1.0",
        "fingerprint_version": "1",
        "base_sha": _BASE_SHA,
        "head_sha": _HEAD_SHA,
    }
    assert provenance["matrix"]["base"]["commit_sha"] == _BASE_SHA
    assert provenance["matrix"]["head"]["commit_sha"] == _HEAD_SHA
    assert provenance["matrix"]["base"]["graph_sha256"] == "c" * 64
    # The census rides the `hive` distribution now; the wire key is unchanged
    # and reports the installed hive version.
    from importlib import metadata

    assert provenance["hive_census_version"] == metadata.version("hive")


def test_verifier_stamp_rides_provenance_only_when_present(
    hand_inputs: dict, hand_receipt: dict
) -> None:
    assert hand_receipt["provenance"]["verifier"] == {
        "head_sha": _HEAD_SHA,
        "base_sha": _BASE_SHA,
        "tool_versions": [["pyright", "1.1.0"], ["pytest", "8.0.0"]],
        "registry_version": "r3",
    }
    receipt = build_receipt(**{**hand_inputs, "verifier_stamp": None})
    assert "verifier" not in receipt["provenance"]


def test_off_schema_receipt_refused_not_emitted(
    hand_inputs: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    import hive.census.receipt as receipt_module

    monkeypatch.setattr(
        receipt_module,
        "_differential_line",
        lambda: {
            "class": "differential",
            "tag": "wibble",  # outside the schema's tag vocabulary
            "subject": "differential",
            "reason": "no-producer",
            "detail": {},
        },
    )
    with pytest.raises(ReceiptSchemaError):
        build_receipt(**hand_inputs)


def _memory_entries() -> tuple[MemoryContext, MemoryContext]:
    return (
        MemoryContext(
            episode_id=41,
            trust="established",
            polarity="dont",
            kind="gotcha",
            anchor="pkg/lib.py::greet",
            ts=1751000000,
            sim=0.91,
            text="greet's punct parameter is load-bearing for templating callers",
        ),
        MemoryContext(
            episode_id=7,
            trust="provisional",
            polarity="do",
            kind="decision",
            anchor="",
            ts=1750000000,
            sim=0.62,
            text="prefer explicit greeting arity",
        ),
    )


class TestInstitutionalMemoryContext:
    def test_context_rides_tagged_outside_lines_and_changes_nothing_else(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        receipt = build_receipt(**hand_inputs, context=_memory_entries())
        assert receipt["context"] == [
            {
                "tag": "institutional-memory",
                "episode_id": 41,
                "trust": "established",
                "polarity": "dont",
                "kind": "gotcha",
                "anchor": "pkg/lib.py::greet",
                "ts": 1751000000,
                "sim": 0.91,
                "text": "greet's punct parameter is load-bearing for templating callers",
            },
            {
                "tag": "institutional-memory",
                "episode_id": 7,
                "trust": "provisional",
                "polarity": "do",
                "kind": "decision",
                "anchor": "",
                "ts": 1750000000,
                "sim": 0.62,
                "text": "prefer explicit greeting arity",
            },
        ]
        # Label-blind: every servable tier rides, trust-labelled, verbatim.
        assert {entry["trust"] for entry in receipt["context"]} == {
            "established",
            "provisional",
        }
        # The block is the ONLY delta: lines, precision counts, gating stamps,
        # and provenance stay byte-identical to the context-free receipt —
        # context informs, it never becomes an evidence line and never gates.
        assert {
            key: value for key, value in receipt.items() if key != "context"
        } == hand_receipt

    def test_no_context_key_when_empty(self, hand_inputs: dict) -> None:
        assert "context" not in build_receipt(**hand_inputs)
        assert "context" not in build_receipt(**hand_inputs, context=())

    def test_context_bearing_receipt_digest_covers_context(
        self, hand_inputs: dict
    ) -> None:
        receipt = build_receipt(**hand_inputs, context=_memory_entries())
        envelope = unsigned_envelope(receipt)
        statement = json.loads(base64.b64decode(envelope["payload"]))
        assert statement["predicate"]["context"] == receipt["context"]
        # The subject digest covers the context bytes: a tampered lesson text
        # changes the recomputed digest, so the ingest-door re-check (kernel-side)
        # refuses it — the tamper-detection property census owns after signing removal.
        claimed = statement["subject"][0]["digest"]["sha256"]
        statement["predicate"]["context"][0]["text"] = "tampered lesson"
        recomputed = hashlib.sha256(canonical_bytes(statement["predicate"])).hexdigest()
        assert recomputed != claimed


def _propagation_entries() -> list[dict]:
    return [
        {
            "seed": "pkg/lib.py::greet",
            "drift": "breaking",
            "depth": 2,
            "neighbors": ["pkg/app.py::caller", "tests/test_lib.py::test_greet"],
        }
    ]


class TestPropagationBlock:
    def test_propagation_rides_outside_lines_and_changes_nothing_else(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        receipt = build_receipt(**hand_inputs, propagation=_propagation_entries())
        assert receipt["propagation"] == _propagation_entries()
        # The block is the ONLY delta: lines, precision counts, gating stamps,
        # and provenance stay byte-identical to the propagation-free receipt —
        # a neighbourhood is advisory fuel, never an evidence line, never gates.
        assert {
            key: value for key, value in receipt.items() if key != "propagation"
        } == hand_receipt

    def test_no_propagation_key_when_empty(self, hand_inputs: dict) -> None:
        assert "propagation" not in build_receipt(**hand_inputs)
        assert "propagation" not in build_receipt(**hand_inputs, propagation=())

    def test_off_schema_propagation_refused_not_emitted(
        self, hand_inputs: dict
    ) -> None:
        entry = _propagation_entries()[0]
        del entry["neighbors"]
        with pytest.raises(ReceiptSchemaError):
            build_receipt(**hand_inputs, propagation=[entry])


def test_real_pipeline_receipt_is_ids_only_and_valid(
    engine_change, graph_pair, change_verdict
) -> None:
    from hive.census import regression_join, validate_receipt

    findings = regression_join(change_verdict, graph_pair.base, depth=2)
    receipt = build_receipt(
        touched=engine_change.touched,
        verdict=change_verdict,
        findings=findings,
        execution=coerce_execution(None),
        verifier_stamp=None,
        graphs=graph_pair,
        precision_budget=0.9,
        depth=2,
    )
    validate_receipt(receipt)
    dumped = json.dumps(receipt)
    # "lonely" is body text planted in the fixture repo's source; ids-only
    # receipts never carry worktree source text.
    assert "lonely" not in dumped
    assert any(line["class"] == "differential" for line in receipt["lines"])
    greet_contract = [
        line
        for line in receipt["lines"]
        if line["class"] == "contract" and line["subject"].endswith("::greet")
    ]
    assert greet_contract
    assert all(line["tag"] == "machine-checked" for line in greet_contract)
    entries = {entry["class"]: entry for entry in receipt["precision"]}
    assert entries["execution"]["decided"] == 0  # producer absent, never a pass


class TestRefStamp:
    """The provenance ref stamp: `ref` rides only when truthy — legacy (ref-less)
    receipts stay byte-identical (the golden above already pins the ref=None shape)."""

    def test_ref_rides_provenance_and_validates(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        receipt = build_receipt(**hand_inputs, ref="feat-x")
        assert receipt["provenance"]["ref"] == "feat-x"
        # the stamp is the ONLY delta: everything else is byte-identical
        provenance_less_ref = {
            k: v for k, v in receipt["provenance"].items() if k != "ref"
        }
        assert provenance_less_ref == hand_receipt["provenance"]
        assert {k: v for k, v in receipt.items() if k != "provenance"} == {
            k: v for k, v in hand_receipt.items() if k != "provenance"
        }

    def test_absent_ref_is_byte_identical(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        # marker target: writing the key unconditionally (None/"" included) breaks
        # legacy byte-identity — the FULL receipt must equal the ref-less golden shape.
        assert build_receipt(**hand_inputs, ref=None) == hand_receipt
        assert build_receipt(**hand_inputs, ref="") == hand_receipt


class TestRepoStamp:
    """The provenance repo stamp: `repo` (the recorded repository identity) rides
    only when truthy — a repo-id-less build stays byte-identical (the golden above
    already pins the repo=None shape), the exact idiom the ref stamp set."""

    def test_repo_rides_provenance_and_validates(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        receipt = build_receipt(**hand_inputs, repo="https://example.test/org/repo.git")
        assert receipt["provenance"]["repo"] == "https://example.test/org/repo.git"
        # the stamp is the ONLY delta: everything else is byte-identical
        provenance_less_repo = {
            k: v for k, v in receipt["provenance"].items() if k != "repo"
        }
        assert provenance_less_repo == hand_receipt["provenance"]
        assert {k: v for k, v in receipt.items() if k != "provenance"} == {
            k: v for k, v in hand_receipt.items() if k != "provenance"
        }

    def test_absent_repo_is_byte_identical(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        # marker target: writing the key unconditionally (None/"" included) breaks
        # legacy byte-identity — the FULL receipt must equal the repo-less golden shape.
        assert build_receipt(**hand_inputs, repo=None) == hand_receipt
        assert build_receipt(**hand_inputs, repo="") == hand_receipt

    def test_repo_and_ref_ride_together(
        self, hand_inputs: dict, hand_receipt: dict
    ) -> None:
        # The sync builder stamps both (--repo-id + --ref): each key renders
        # independently and nothing else moves.
        receipt = build_receipt(
            **hand_inputs, ref="refs/pull/7/head", repo="https://example.test/o/r.git"
        )
        assert receipt["provenance"]["ref"] == "refs/pull/7/head"
        assert receipt["provenance"]["repo"] == "https://example.test/o/r.git"
        stripped = {
            k: v for k, v in receipt["provenance"].items() if k not in ("ref", "repo")
        }
        assert stripped == hand_receipt["provenance"]
