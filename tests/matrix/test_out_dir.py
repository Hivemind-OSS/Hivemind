"""Explicit ``out_dir`` on build_graph/update — per-call artifact placement.

The env default (``MATRIX_OUT``) is read at import time and is process-global,
so two repos built in one process used to clobber each other's
graph.json/manifest. An explicit ``out_dir`` pins both artifacts per call;
``out_dir=None`` keeps the env-default behavior exactly. Every test redirects
MATRIX_OUT at a decoy tmp dir (reloading matrix.paths) so a regression that
ignores ``out_dir`` writes into the decoy — asserted untouched — never into the
working tree.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


@pytest.fixture()
def decoy_default(tmp_path, monkeypatch):
    """Point the env-default output dir at a decoy so any write that ignores
    an explicit ``out_dir`` is observable (and harmless)."""
    decoy = tmp_path / "decoy-default"
    monkeypatch.setenv("MATRIX_OUT", str(decoy))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    return decoy


def _make_repo(base: Path, name: str, body: str) -> Path:
    src = base / name
    src.mkdir()
    (src / "mod.py").write_text(body, encoding="utf-8")
    return src


def _labels_on_disk(out: Path) -> set[str]:
    data = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    return {n.get("label") for n in data["nodes"]}


def _later_mtime(path: Path) -> None:
    """Guarantee a strictly-later mtime (avoids same-second collisions)."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def test_out_dir_isolates_two_repos_in_one_process(tmp_path, decoy_default):
    """Two repos, one process, one env: each keeps its own artifacts, and an
    incremental update on one never touches the other's cache (the F6 fix)."""
    import hive.matrix as matrix

    repo1 = _make_repo(tmp_path, "repo1", "def one_fn():\n    return 1\n")
    repo2 = _make_repo(tmp_path, "repo2", "def two_fn():\n    return 2\n")
    out1 = tmp_path / "out1"
    out2 = tmp_path / "out2"

    matrix.build_graph(repo1, out_dir=out1)
    matrix.build_graph(repo2, out_dir=out2)

    assert "one_fn()" in _labels_on_disk(out1)
    assert "two_fn()" not in _labels_on_disk(out1)
    assert "two_fn()" in _labels_on_disk(out2)
    assert (out1 / "manifest.json").is_file() and (out2 / "manifest.json").is_file()

    # An incremental update on repo1 reads/writes ONLY repo1's cache.
    before2 = (out2 / "graph.json").read_bytes()
    mod = repo1 / "mod.py"
    mod.write_text(
        "def one_fn():\n    return 1\n\ndef one_more():\n    return 11\n",
        encoding="utf-8",
    )
    _later_mtime(mod)
    g1 = matrix.update(repo1, out_dir=out1)
    assert "one_more()" in {n.label for n in g1.nodes()}
    assert "one_more()" in _labels_on_disk(out1)
    assert (out2 / "graph.json").read_bytes() == before2  # repo2 untouched

    # Nothing leaked into the env-default location.
    assert not (decoy_default / "graph.json").exists()
    assert not (decoy_default / "manifest.json").exists()


def test_out_dir_none_writes_the_default_paths(tmp_path, decoy_default):
    """``out_dir=None`` keeps today's env-default artifact locations — and the
    bytes written there equal an explicit ``out_dir`` pointed at the same dir."""
    import hive.matrix as matrix
    import hive.matrix.paths as paths

    repo = _make_repo(tmp_path, "repo", "def solo_fn():\n    return 1\n")

    matrix.build_graph(repo)
    default_graph = Path(paths.default_graph_json())
    default_manifest = Path(paths.default_manifest())
    assert default_graph == decoy_default / "graph.json"  # exactly the default location
    assert default_graph.is_file() and default_manifest.is_file()
    g_none = default_graph.read_bytes()
    m_none = default_manifest.read_bytes()

    default_graph.unlink()
    default_manifest.unlink()
    matrix.build_graph(repo, out_dir=decoy_default)
    assert default_graph.read_bytes() == g_none  # byte-identical artifacts
    assert default_manifest.read_bytes() == m_none

    # update() with out_dir=None also stays on the default paths.
    g = matrix.update(repo)
    assert "solo_fn()" in {n.label for n in g.nodes()}
    assert default_graph.is_file() and default_manifest.is_file()


def test_update_fallback_threads_out_dir(tmp_path, decoy_default):
    """update() with no prior manifest under ``out_dir`` full-builds INTO
    ``out_dir`` — never into the env default."""
    import hive.matrix as matrix

    repo = _make_repo(tmp_path, "repo", "def fresh_fn():\n    return 1\n")
    out = tmp_path / "out"

    g = matrix.update(repo, out_dir=out)  # no manifest yet -> build_graph fallback
    assert "fresh_fn()" in {n.label for n in g.nodes()}
    assert "fresh_fn()" in _labels_on_disk(out)
    assert (out / "manifest.json").is_file()
    assert not (decoy_default / "graph.json").exists()
    assert not (decoy_default / "manifest.json").exists()


def test_update_half_corpus_escape_threads_out_dir(tmp_path, decoy_default):
    """The >half-corpus escape (incremental premise gone -> scratch rebuild)
    also lands its artifacts under ``out_dir``."""
    import hive.matrix as matrix

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "a.py").write_text(
        "from b import bee\n\ndef aye():\n    return bee()\n", encoding="utf-8"
    )
    (repo / "b.py").write_text("def bee():\n    return 2\n", encoding="utf-8")
    out = tmp_path / "out"
    matrix.build_graph(repo, out_dir=out)

    # Editing b pulls importer a into the closure: extraction set is 2 of 2
    # discovered files > half the corpus, so the escape fires.
    b = repo / "b.py"
    b.write_text("def bee2():\n    return 22\n", encoding="utf-8")
    _later_mtime(b)
    g = matrix.update(repo, out_dir=out)
    assert "bee2()" in {n.label for n in g.nodes()}
    assert "bee2()" in _labels_on_disk(out)  # fresh, not stale
    assert not (decoy_default / "graph.json").exists()
