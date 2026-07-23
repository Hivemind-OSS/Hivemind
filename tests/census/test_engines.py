"""Contract tests for the engine adapters.

Hermetic-REAL tier: the real matrix and combdrift engines run over the
constructed fixture repo (see conftest). The only hand-built graphs appear in
the pathological-node degrade test, and those use the real matrix Graph class
around real networkx data — no engine fakes.
"""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

from hive.census.diff import Change, ChangedFile, ChangeSet
from hive.census.engines import (
    EngineError,
    GraphPair,
    attribute_symbols,
    build_graphs,
    ensure_scratch_matrix_out,
)

SHA_A = "a" * 40
SHA_B = "b" * 40


class TestEnsureScratchMatrixOut:
    def test_pin_is_idempotent_and_env_points_at_scratch(
        self, matrix_scratch: Path
    ) -> None:
        assert matrix_scratch.is_dir()
        assert Path(os.environ["MATRIX_OUT"]) == matrix_scratch
        assert ensure_scratch_matrix_out() == matrix_scratch

    def test_matrix_imported_before_any_pin_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import hive.census.engines as engines

        monkeypatch.setattr(engines, "_scratch", None)
        monkeypatch.setitem(sys.modules, "matrix", types.ModuleType("matrix"))
        with pytest.raises(EngineError):
            ensure_scratch_matrix_out()

    def test_foreign_matrix_out_after_pin_is_refused(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hive.census.engines as engines

        monkeypatch.setattr(engines, "_scratch", tmp_path / "scratch")
        fake_paths = types.ModuleType("matrix.paths")
        fake_paths.MATRIX_OUT = str(tmp_path / "elsewhere" / "matrix-out")
        monkeypatch.setitem(sys.modules, "matrix.paths", fake_paths)
        with pytest.raises(EngineError):
            ensure_scratch_matrix_out()

    def test_matrix_out_inside_the_scratch_is_accepted(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        import hive.census.engines as engines

        scratch = tmp_path / "scratch"
        monkeypatch.setattr(engines, "_scratch", scratch)
        fake_paths = types.ModuleType("matrix.paths")
        fake_paths.MATRIX_OUT = str(scratch)
        monkeypatch.setitem(sys.modules, "matrix.paths", fake_paths)
        assert ensure_scratch_matrix_out() == scratch


class TestResolveSeedConformance:
    def test_non_public_import_surface_is_present_and_callable(
        self, matrix_scratch: Path
    ) -> None:
        # The join depends on this non-public matrix function; an engine
        # upgrade that moves or reshapes it must go red here, not as a silent
        # empty join downstream.
        import networkx as nx
        from matrix.affected import resolve_seed

        assert callable(resolve_seed)
        assert resolve_seed(nx.DiGraph(), "anything") is None


def _labels(g_nx) -> set[str]:
    return {str(data.get("label") or "") for _, data in g_nx.nodes(data=True)}


class TestBuildGraphs:
    def test_pair_holds_both_sides_in_memory_with_matching_stamps(
        self, engine_change: Change, graph_pair: GraphPair
    ) -> None:
        assert graph_pair.base_stamp.commit_sha == engine_change.touched.base_sha
        assert graph_pair.head_stamp.commit_sha == engine_change.touched.head_sha
        base_labels = _labels(graph_pair.base.nx)
        head_labels = _labels(graph_pair.head.nx)
        # A deleted symbol exists only in the base graph; an added file's
        # symbols only in the head graph — both sides must stay live in memory
        # even though the second build overwrote the on-disk graph.json.
        assert "farewell()" in base_labels
        assert "farewell()" not in head_labels
        assert "alpha()" in head_labels
        assert "alpha()" not in base_labels
        assert graph_pair.base_stamp.node_count == graph_pair.base.nx.number_of_nodes()
        assert graph_pair.head_stamp.node_count == graph_pair.head.nx.number_of_nodes()

    def test_sha_mismatch_is_an_engine_error(
        self, engine_change: Change, matrix_scratch: Path
    ) -> None:
        doctored = Change(
            touched=ChangeSet(
                base_sha=engine_change.touched.head_sha,
                head_sha=engine_change.touched.head_sha,
                files=engine_change.touched.files,
            ),
            base_tree=engine_change.base_tree,
            head_tree=engine_change.head_tree,
        )
        with pytest.raises(EngineError):
            build_graphs(doctored)

    def test_build_graphs_survives_a_polluted_git_dir_env(
        self,
        engine_repo: Path,
        engine_change: Change,
        matrix_scratch: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Reproduces the live post-merge hook failure: git populates a hook
        subprocess's env with GIT_DIR/GIT_WORK_TREE pointing at the INVOKING
        repo, which (pre-fix) silently overrides the `-C <worktree>` targeting
        of version.py's HEAD read, so the base worktree's stamp reads the
        invoking repo's HEAD instead of the base SHA and build_graphs raises."""
        monkeypatch.setenv("GIT_DIR", str(engine_repo / ".git"))
        monkeypatch.setenv("GIT_WORK_TREE", str(engine_repo))
        pair = build_graphs(engine_change)  # must NOT raise EngineError
        assert pair.base_stamp.commit_sha == engine_change.touched.base_sha
        assert pair.head_stamp.commit_sha == engine_change.touched.head_sha

    def test_no_matrix_out_litter_outside_the_scratch(
        self, engine_repo: Path, engine_change: Change, graph_pair: GraphPair
    ) -> None:
        for probed in (
            Path.cwd(),
            engine_repo,
            engine_change.base_tree,
            engine_change.head_tree,
        ):
            assert not (probed / "matrix-out").exists()


EXPECTED_PAIRS = (
    ("app.py", None),
    ("app.py", "bye"),
    ("app.py", "caller"),
    ("lib.py", "Greeter.wave"),
    ("lib.py", "farewell"),
    ("lib.py", "greet"),
    ("lib.py", "orphan"),
    ("lib2.py", None),
    ("notes.txt", None),
    ("util.py", "alpha"),
    ("util.py", "beta"),
)


class TestAttributeSymbols:
    def test_exact_attribution_over_the_real_pair(self, touched_symbols) -> None:
        assert touched_symbols == EXPECTED_PAIRS

    def test_union_attributes_symbols_starting_inside_a_whole_file_span(
        self, touched_symbols
    ) -> None:
        # util.py is added whole: its span starts at line 1, before every
        # symbol node, so the enclosing half of the rule finds nothing and the
        # starting-within half must carry the attribution.
        assert ("util.py", "alpha") in touched_symbols
        assert ("util.py", "beta") in touched_symbols
        assert ("util.py", None) not in touched_symbols

    def test_file_own_node_is_never_read_as_a_symbol(self, touched_symbols) -> None:
        # lib2.py's change touches only the banner comment; the file's own
        # node (label == basename) is the only in-range node and must be
        # skipped, leaving an honest file-level entry.
        assert ("lib2.py", None) in touched_symbols
        assert all(
            symbol is None or "lib2" not in symbol
            for path, symbol in touched_symbols
            if path == "lib2.py"
        )

    def test_span_with_no_candidate_degrades_to_file_level(
        self, touched_symbols
    ) -> None:
        # notes.txt has no graph nodes at all; app.py's import-line span has
        # no candidate symbol even though its other spans attribute.
        assert ("notes.txt", None) in touched_symbols
        assert ("app.py", None) in touched_symbols
        assert ("app.py", "caller") in touched_symbols

    def test_removed_symbols_attributed_against_the_base_graph(
        self, touched_symbols, graph_pair: GraphPair
    ) -> None:
        assert ("lib.py", "farewell") in touched_symbols
        assert ("lib.py", "orphan") in touched_symbols
        assert "farewell()" not in _labels(graph_pair.head.nx)

    def test_method_symbol_is_qualified_class_dot_method(self, touched_symbols) -> None:
        assert ("lib.py", "Greeter.wave") in touched_symbols

    def test_deterministic_and_deduplicated(
        self, engine_change: Change, graph_pair: GraphPair, touched_symbols
    ) -> None:
        again = attribute_symbols(engine_change.touched, graph_pair)
        assert again == touched_symbols
        assert len(set(touched_symbols)) == len(touched_symbols)

    def test_pathological_node_degrades_to_a_file_entry_not_an_abort(
        self, matrix_scratch: Path
    ) -> None:
        import networkx as nx
        from matrix.model import Graph

        class _Booby:
            def __str__(self) -> str:
                raise RuntimeError("poisoned node data")

        base = nx.DiGraph()
        base.add_node(
            "poison_fn",
            label=_Booby(),
            source_file="poison.py",
            source_location="L2",
        )
        base.add_node("ok_fn", label="fn()", source_file="ok.py", source_location="L1")
        touched = ChangeSet(
            base_sha=SHA_A,
            head_sha=SHA_B,
            files=(
                ChangedFile(
                    path="poison.py",
                    status="modified",
                    added_spans=(),
                    removed_spans=((1, 3),),
                ),
                ChangedFile(
                    path="ok.py",
                    status="modified",
                    added_spans=(),
                    removed_spans=((1, 2),),
                ),
            ),
        )
        # Stamps are irrelevant to attribution; this unit test only needs the
        # two graph sides.
        graphs = GraphPair(
            base=Graph(base), head=Graph(nx.DiGraph()), base_stamp=None, head_stamp=None
        )
        assert attribute_symbols(touched, graphs) == (
            ("ok.py", "fn"),
            ("poison.py", None),
        )


class TestNodeChange:
    def test_drift_classes_on_the_real_pair(
        self, engine_change: Change, change_verdict
    ) -> None:
        drifts = {(s.path, s.symbol): s.drift for s in change_verdict.symbols}
        assert drifts[("lib.py", "greet")] == "breaking"
        assert drifts[("lib.py", "Greeter.wave")] == "breaking"
        assert drifts[("lib.py", "farewell")] == "removed"
        assert drifts[("lib.py", "orphan")] == "removed"
        assert drifts[("app.py", "caller")] == "unchanged"
        assert drifts[("app.py", "bye")] == "unchanged"
        assert drifts[("util.py", "alpha")] == "additive"
        assert drifts[("util.py", "beta")] == "additive"
        assert change_verdict.verdict == "stale"
        assert change_verdict.base_sha == engine_change.touched.base_sha
        assert change_verdict.head_sha == engine_change.touched.head_sha

    def test_reasons_flow_through_with_detail_suffixes_untouched(
        self, change_verdict
    ) -> None:
        reasons = {(s.path, s.symbol): s.reason for s in change_verdict.symbols}
        assert reasons[("lib.py", "farewell")].startswith("symbol_missing")
        assert ": " in reasons[("lib.py", "farewell")]
        assert reasons[("lib.py", "greet")].startswith("signature_changed")
        assert ": " in reasons[("lib.py", "greet")]

    def test_file_level_entries_produce_no_symbol_change(self, change_verdict) -> None:
        paths_with_symbols = {s.path for s in change_verdict.symbols}
        assert "notes.txt" not in paths_with_symbols
        assert "lib2.py" not in paths_with_symbols
