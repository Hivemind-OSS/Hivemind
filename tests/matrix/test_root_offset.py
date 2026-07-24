"""root_offset — the public inverse of the extraction-root inference.

The graph relativizes every node ``source_file`` against ``_infer_root``'s
corpus common ancestor; ``root_offset`` answers what prefix that dropped
relative to a consumer's base dir, so path-joining consumers (hive-edge
anchors, hive-census receipt subjects) can re-root graph paths without
re-implementing the inference. Multi-rooted corpora yield "" (byte-inert
bridge); failures fail OPEN to "" — the pre-bridge repo-dialect assumption.
"""

from __future__ import annotations

from pathlib import Path

from hive.matrix import root_offset


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def f():\n    return 0\n", encoding="utf-8")
    return path


def test_multi_root_corpus_offsets_to_empty(tmp_path):
    paths = [_touch(tmp_path / "main.py"), _touch(tmp_path / "pkg" / "mod.py")]
    assert root_offset(tmp_path, paths) == ""


def test_single_root_corpus_names_the_dropped_prefix(tmp_path):
    paths = [_touch(tmp_path / "src" / "one.py"), _touch(tmp_path / "src" / "two.py")]
    assert root_offset(tmp_path, paths) == "src"


def test_nested_single_root_names_the_full_dropped_prefix(tmp_path):
    paths = [
        _touch(tmp_path / "src" / "pkg" / "one.py"),
        _touch(tmp_path / "src" / "pkg" / "two.py"),
    ]
    assert root_offset(tmp_path, paths) == "src/pkg"


def test_one_file_corpus_offsets_to_the_file_parent(tmp_path):
    paths = [_touch(tmp_path / "pkg" / "only.py")]
    assert root_offset(tmp_path, paths) == "pkg"


def test_empty_corpus_fails_open_to_empty(tmp_path):
    assert root_offset(tmp_path, []) == ""


def test_foreign_corpus_fails_open_to_empty(tmp_path):
    foreign = tmp_path / "elsewhere"
    paths = [_touch(foreign / "src" / "one.py"), _touch(foreign / "src" / "two.py")]
    assert root_offset(tmp_path / "base", paths) == ""


def test_garbage_base_fails_open_to_empty(tmp_path):
    paths = [_touch(tmp_path / "src" / "one.py")]
    assert root_offset(object(), paths) == ""  # type: ignore[arg-type]


def test_agrees_with_the_extraction_the_engine_actually_ran(tmp_path):
    """The load-bearing agreement: the offset joined to a built graph's
    source_file reproduces the base-relative path — the bridge IS the engine's
    own inference, not a lookalike."""
    from hive.matrix import build_graph

    _touch(tmp_path / "src" / "one.py")
    _touch(tmp_path / "src" / "two.py")
    graph = build_graph(tmp_path, out_dir=tmp_path.parent / "out")
    offset = root_offset(
        tmp_path, [tmp_path / "src" / "one.py", tmp_path / "src" / "two.py"]
    )
    assert offset == "src"
    source_files = {node.source_file for node in graph.nodes()}
    assert "one.py" in source_files  # the graph dialect dropped src/
    rejoined = {f"{offset}/{sf}" for sf in source_files}
    assert "src/one.py" in rejoined and "src/two.py" in rejoined
