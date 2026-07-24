"""Contract tests for the regression join.

Two tiers: a pure truth-table over RegressionFinding's invariants, and the
hermetic-REAL pipeline — the fixture repo's real verdict joined against the
real base graph. Hand-assembled inputs use the real combdrift carrier classes
with fixed field values; no engine is faked.
"""

from __future__ import annotations


import pytest

from hive.census.join import RegressionFinding, regression_join

SHA_A = "a" * 40
SHA_B = "b" * 40


def _finding(**overrides) -> RegressionFinding:
    fields = dict(
        path="lib.py",
        symbol="greet",
        drift="breaking",
        seed="lib_greet",
        callers=("app_caller",),
        dependents=("app",),
        tests=(),
        depth=2,
        tag="bounded-estimate",
        reason="blast-radius-lower-bound",
    )
    fields.update(overrides)
    return RegressionFinding(**fields)


class TestRegressionFindingInvariants:
    def test_resolved_bounded_estimate_constructs(self) -> None:
        finding = _finding()
        assert finding.seed == "lib_greet"
        assert finding.tag == "bounded-estimate"

    def test_abstained_unverified_constructs(self) -> None:
        finding = _finding(
            seed=None,
            callers=(),
            dependents=(),
            tests=(),
            tag="unverified",
            reason="seed-unresolved",
        )
        assert finding.seed is None

    def test_unresolved_seed_with_bounded_tag_is_unconstructable(self) -> None:
        with pytest.raises(ValueError):
            _finding(seed=None, callers=(), dependents=(), tests=())

    def test_resolved_seed_with_unverified_tag_is_unconstructable(self) -> None:
        with pytest.raises(ValueError):
            _finding(tag="unverified")

    def test_non_regression_drift_is_unconstructable(self) -> None:
        for drift in ("additive", "unchanged", "indeterminate"):
            with pytest.raises(ValueError):
                _finding(drift=drift)

    def test_abstained_finding_with_hits_is_unconstructable(self) -> None:
        with pytest.raises(ValueError):
            _finding(seed=None, dependents=(), tests=(), tag="unverified")

    def test_unknown_tag_is_unconstructable(self) -> None:
        with pytest.raises(ValueError):
            _finding(tag="machine-checked")


def _symbol_change(path: str, symbol: str, drift: str, reason: str = "reason: detail"):
    import hive.combdrift as combdrift

    return combdrift.SymbolChange(
        path=path,
        symbol=symbol,
        existed_before=True,
        exists_after=drift not in ("removed",),
        drift=drift,
        old_fingerprint=None,
        new_fingerprint=None,
        reason=reason,
    )


def _verdict(*symbol_changes):
    import hive.combdrift as combdrift

    return combdrift.ChangeVerdict(
        verifier_version=combdrift.verifier_version(base_sha=SHA_A, head_sha=SHA_B),
        base_sha=SHA_A,
        head_sha=SHA_B,
        symbols=tuple(symbol_changes),
        verdict="stale",
    )


@pytest.fixture(scope="session")
def findings(change_verdict, graph_pair) -> tuple[RegressionFinding, ...]:
    return regression_join(change_verdict, graph_pair.base, depth=2)


@pytest.fixture(scope="session")
def seed_of(graph_pair):
    from hive.matrix.affected import resolve_seed

    def resolve(query: str) -> str:
        node_id = resolve_seed(graph_pair.base.nx, query)
        assert node_id is not None, f"fixture seed {query!r} did not resolve"
        return node_id

    return resolve


def _by_symbol(findings) -> dict[tuple[str, str], RegressionFinding]:
    return {(f.path, f.symbol): f for f in findings}


class TestRegressionJoinPipeline:
    def test_only_regression_class_symbols_produce_findings(self, findings) -> None:
        # The real verdict also carries additive (util.py) and unchanged
        # (app.py) symbols; none of them may reach the join.
        assert set(_by_symbol(findings)) == {
            ("lib.py", "greet"),
            ("lib.py", "Greeter.wave"),
            ("lib.py", "farewell"),
            ("lib.py", "orphan"),
        }

    def test_breaking_finding_carries_callers_and_tests(
        self, findings, seed_of
    ) -> None:
        finding = _by_symbol(findings)[("lib.py", "greet")]
        assert finding.tag == "bounded-estimate"
        assert finding.drift == "breaking"
        assert finding.seed == seed_of("greet")
        assert finding.depth == 2
        assert set(finding.callers) == {
            seed_of("caller"),
            seed_of("wave"),
            seed_of("test_greet"),
            seed_of("outer"),
        }
        assert set(finding.dependents) == {seed_of("app.py"), seed_of("test_lib.py")}
        assert set(finding.tests) == {seed_of("test_lib.py"), seed_of("test_greet")}

    def test_removed_symbol_joins_on_the_base_graph(self, findings, seed_of) -> None:
        # farewell exists only at base; its caller is visible only there.
        finding = _by_symbol(findings)[("lib.py", "farewell")]
        assert finding.tag == "bounded-estimate"
        assert finding.seed == seed_of("farewell")
        assert finding.callers == (seed_of("bye"),)
        assert finding.dependents == (seed_of("app.py"),)
        assert finding.tests == ()

    def test_head_graph_join_would_abstain_for_removed_symbols(
        self, change_verdict, graph_pair
    ) -> None:
        # The head graph has no node for a removed symbol: joining there
        # abstains instead of reporting the real caller — why base is the
        # only sound join side.
        head_findings = _by_symbol(
            regression_join(change_verdict, graph_pair.head, depth=2)
        )
        assert head_findings[("lib.py", "farewell")].tag == "unverified"
        assert head_findings[("lib.py", "farewell")].seed is None

    def test_resolved_zero_impact_stays_bounded_estimate(
        self, findings, seed_of
    ) -> None:
        # orphan resolves but nothing depends on it: a resolved-empty result
        # is evidence, distinct from the unresolvable abstain.
        finding = _by_symbol(findings)[("lib.py", "orphan")]
        assert finding.tag == "bounded-estimate"
        assert finding.seed == seed_of("orphan")
        assert finding.callers == ()
        assert finding.dependents == ()
        assert finding.tests == ()

    def test_method_symbol_seed_resolves_despite_dotted_label_shape(
        self, findings, seed_of
    ) -> None:
        finding = _by_symbol(findings)[("lib.py", "Greeter.wave")]
        assert finding.tag == "bounded-estimate"
        assert finding.seed == seed_of("wave")

    def test_depth_is_threaded_into_the_walk(
        self, change_verdict, graph_pair, seed_of
    ) -> None:
        shallow = _by_symbol(regression_join(change_verdict, graph_pair.base, depth=1))
        greet = shallow[("lib.py", "greet")]
        assert greet.depth == 1
        assert seed_of("outer") not in greet.callers  # only reachable at depth 2
        assert seed_of("caller") in greet.callers


class TestRegressionJoinFailClosed:
    def test_unresolvable_symbol_abstains_distinct_from_resolved_zero(
        self, graph_pair, findings
    ) -> None:
        ghost = _verdict(
            _symbol_change(
                "lib.py", "ghost_zz9", "removed", "symbol_missing: ghost_zz9"
            )
        )
        (finding,) = regression_join(ghost, graph_pair.base, depth=2)
        assert finding.tag == "unverified"
        assert finding.seed is None
        assert finding.reason == "seed-unresolved"
        assert finding.callers == ()
        assert finding.dependents == ()
        assert finding.tests == ()
        # Byte-distinct from the resolved-zero-callers finding: that one keeps
        # its seed and its bounded-estimate tag.
        orphan = _by_symbol(findings)[("lib.py", "orphan")]
        assert orphan.tag != finding.tag
        assert orphan.seed is not None

    def test_non_regression_drifts_produce_no_finding(self, graph_pair) -> None:
        verdict = _verdict(
            _symbol_change("lib.py", "greet", "additive"),
            _symbol_change("lib.py", "farewell", "unchanged"),
            _symbol_change("lib.py", "orphan", "indeterminate"),
        )
        assert regression_join(verdict, graph_pair.base, depth=2) == ()

    def test_unknown_drift_value_degrades_to_no_finding_never_crashes(
        self, graph_pair
    ) -> None:
        verdict = _verdict(_symbol_change("lib.py", "greet", "sideways"))
        assert regression_join(verdict, graph_pair.base, depth=2) == ()

    def test_pathological_symbol_degrades_that_symbol_never_aborts(
        self, monkeypatch: pytest.MonkeyPatch, change_verdict, graph_pair, seed_of
    ) -> None:
        import hive.matrix as matrix

        real_blast = matrix.blast_radius
        poisoned_seed = seed_of("farewell")

        def exploding_blast(graph, seed, *, depth=2, **kwargs):
            if seed == poisoned_seed:
                raise RuntimeError("engine exploded")
            return real_blast(graph, seed, depth=depth, **kwargs)

        monkeypatch.setattr(matrix, "blast_radius", exploding_blast)
        results = _by_symbol(regression_join(change_verdict, graph_pair.base, depth=2))
        degraded = results[("lib.py", "farewell")]
        assert degraded.tag == "unverified"
        assert degraded.seed is None
        assert degraded.reason.startswith("join-error")
        assert degraded.callers == ()
        # Every other symbol's evidence still lands intact.
        intact = results[("lib.py", "greet")]
        assert intact.tag == "bounded-estimate"
        assert seed_of("caller") in intact.callers
