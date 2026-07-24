"""End-to-end incremental rebuild: build_graph then update on a temp repo.

Each test points MATRIX_OUT at a tmp dir so graph.json/manifest never touch the
real repo, builds a tiny Python project, then mutates it and asserts update()
reflects the add / modify / delete through the persisted artifacts.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    """A temp repo + an isolated MATRIX_OUT, with matrix.paths reloaded so the
    env override takes effect for this test."""
    out = tmp_path / "out"
    monkeypatch.setenv("MATRIX_OUT", str(out))
    # paths bakes MATRIX_OUT into a constant at import time; reload so the
    # override lands. build_graph/update resolve paths.default_* lazily at call
    # time, so reloading this one module is enough to redirect every artifact.
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)

    src = tmp_path / "src"
    src.mkdir()
    (src / "a.py").write_text(
        "def alpha():\n    return beta()\n\ndef beta():\n    return 1\n",
        encoding="utf-8",
    )
    (src / "b.py").write_text("def gamma():\n    return 2\n", encoding="utf-8")
    return src


def _labels(graph):
    return {n.label for n in graph.nodes()}


def _later_mtime(path: Path) -> None:
    """Guarantee a strictly-later mtime than now (avoids same-second collisions)."""
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def test_build_then_add_file(repo):
    import hive.matrix as matrix

    g0 = matrix.build_graph(repo)
    assert "gamma()" in _labels(g0)

    (repo / "c.py").write_text("def delta():\n    return 3\n", encoding="utf-8")
    g1 = matrix.update(repo)
    assert "delta()" in _labels(g1)  # addition reflected
    assert "alpha()" in _labels(g1)  # untouched file preserved


def test_build_then_modify_file(repo):
    import hive.matrix as matrix

    matrix.build_graph(repo)

    b = repo / "b.py"
    b.write_text("def gamma2():\n    return 22\n", encoding="utf-8")
    _later_mtime(b)

    g1 = matrix.update(repo)
    labels = _labels(g1)
    assert "gamma2()" in labels  # change reflected
    assert "gamma()" not in labels  # stale symbol gone


def test_build_then_delete_file(repo):
    import hive.matrix as matrix

    g0 = matrix.build_graph(repo)
    assert "gamma()" in _labels(g0)

    (repo / "b.py").unlink()
    g1 = matrix.update(repo)
    assert "gamma()" not in _labels(g1)  # deleted file's node gone
    assert "alpha()" in _labels(g1)  # the other file survives


def test_update_with_no_prior_manifest_falls_back_to_full_build(repo):
    """update() before any build_graph must not crash — it does a full derive."""
    import hive.matrix as matrix

    g = matrix.update(repo)
    assert "alpha()" in _labels(g)
    assert "gamma()" in _labels(g)


def test_update_is_idempotent_with_no_changes(repo):
    import hive.matrix as matrix

    matrix.build_graph(repo)
    g1 = matrix.update(repo)
    g2 = matrix.update(repo)
    assert (len(g1), len(g1.edges())) == (len(g2), len(g2.edges()))


# ── BUG-025: incremental update must preserve cross-file topology ────────────────
#
# The prior fixtures carried ZERO cross-file imports, which is why the loss law was
# never caught: extract() binds cross-file references eagerly against its chunk-local
# node table, so re-extracting a changed file ALONE permanently dropped every edge that
# file owns into (or out of) the rest of the corpus.


@pytest.fixture()
def xrepo(tmp_path, monkeypatch):
    """A repo WITH cross-file imports (chain f3 -> f2 -> f1, a subdir importer, and
    padding so the import closure stays under the full-rebuild valve)."""
    out = tmp_path / "out"
    monkeypatch.setenv("MATRIX_OUT", str(out))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)

    src = tmp_path / "proj"
    (src / "sub").mkdir(parents=True)
    (src / "f1.py").write_text("def base_fn():\n    return 1\n", encoding="utf-8")
    (src / "f2.py").write_text(
        "from f1 import base_fn\n\ndef mid_fn():\n    return base_fn()\n",
        encoding="utf-8",
    )
    (src / "f3.py").write_text(
        "from f2 import mid_fn\n\ndef top():\n    return mid_fn()\n", encoding="utf-8"
    )
    # The sub/ pair's import closure stays ENTIRELY inside sub/ — the case where an
    # inferred (non-pinned) chunk root forks from the corpus root and mints ghost ids.
    (src / "sub" / "helper.py").write_text(
        "def help_fn():\n    return 7\n", encoding="utf-8"
    )
    (src / "sub" / "inner.py").write_text(
        "from sub.helper import help_fn\n\ndef deep():\n    return help_fn()\n",
        encoding="utf-8",
    )
    for i in (1, 2, 3, 4):
        (src / f"pad{i}.py").write_text(
            f"def pad{i}():\n    return {i}\n", encoding="utf-8"
        )
    return src


def _node_id(g, source_file: str, label: str) -> str | None:
    for nid, d in g.nx.nodes(data=True):
        if d.get("source_file") == source_file and d.get("label") == label:
            return nid
    return None


def _has_relation(g, src_id, tgt_id, relation: str) -> bool:
    d = g.nx.get_edge_data(src_id, tgt_id)
    return bool(d) and d.get("relation") == relation


def _touch(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 10))


def test_update_preserves_callers_cross_file_edges_on_caller_edit(xrepo):
    """BUG-025 regression: editing ONLY the caller must not lose caller->callee edges."""
    import hive.matrix as matrix

    g0 = matrix.build_graph(xrepo)
    top0 = _node_id(g0, "f3.py", "top()")
    mid0 = _node_id(g0, "f2.py", "mid_fn()")
    assert top0 and mid0 and _has_relation(g0, top0, mid0, "calls")

    _touch(
        xrepo / "f3.py",
        "from f2 import mid_fn\n\ndef top():\n    x = 1\n    return mid_fn() + x\n",
    )
    g1 = matrix.update(xrepo)
    top1 = _node_id(g1, "f3.py", "top()")
    mid1 = _node_id(g1, "f2.py", "mid_fn()")
    assert top1 and mid1
    assert _has_relation(g1, top1, mid1, "calls")  # the cross-file call SURVIVES
    f3_file = _node_id(g1, "f3.py", "f3.py")
    f2_file = _node_id(g1, "f2.py", "f2.py")
    assert f3_file and f2_file
    assert _has_relation(
        g1, f3_file, f2_file, "imports_from"
    )  # file-level import intact


def test_update_preserves_callees_own_outgoing_edges_on_callee_edit(xrepo):
    """The loss law is general: a re-extracted CALLEE must keep ITS own outgoing edges."""
    import hive.matrix as matrix

    matrix.build_graph(xrepo)
    _touch(
        xrepo / "f2.py",
        "from f1 import base_fn\n\ndef mid_fn():\n    y = 2\n    return base_fn() + y\n",
    )
    g1 = matrix.update(xrepo)
    mid1 = _node_id(g1, "f2.py", "mid_fn()")
    base1 = _node_id(g1, "f1.py", "base_fn()")
    assert mid1 and base1
    assert _has_relation(g1, mid1, base1, "calls")  # f2's own outgoing edge survives


def test_update_rebinds_unchanged_importer_when_callee_adds_symbol(xrepo):
    """An UNCHANGED importer referencing a symbol the callee just gained must pick up the
    edge — importers of a changed file are re-merged, not just re-read as context."""
    import hive.matrix as matrix

    (xrepo / "f3.py").write_text(
        "from f2 import mid_fn\n\ndef top():\n    return mid_fn()\n\n"
        "def top2():\n    return late_fn()\n",
        encoding="utf-8",
    )
    g0 = matrix.build_graph(xrepo)
    assert _node_id(g0, "f2.py", "late_fn()") is None  # not defined yet

    _touch(
        xrepo / "f2.py",
        "from f1 import base_fn\n\ndef mid_fn():\n    return base_fn()\n\n"
        "def late_fn():\n    return 9\n",
    )
    g1 = matrix.update(xrepo)
    top2 = _node_id(g1, "f3.py", "top2()")
    late = _node_id(g1, "f2.py", "late_fn()")
    assert top2 and late
    assert _has_relation(g1, top2, late, "calls")  # unchanged f3 gained the binding


def test_update_removes_stale_cross_file_edge(xrepo):
    """Stale-edge cleanup stays intact: a removed call disappears on update."""
    import hive.matrix as matrix

    g0 = matrix.build_graph(xrepo)
    top0 = _node_id(g0, "f3.py", "top()")
    mid0 = _node_id(g0, "f2.py", "mid_fn()")
    assert _has_relation(g0, top0, mid0, "calls")

    _touch(xrepo / "f3.py", "def top():\n    return 3\n")  # import + call removed
    g1 = matrix.update(xrepo)
    top1 = _node_id(g1, "f3.py", "top()")
    mid1 = _node_id(g1, "f2.py", "mid_fn()")
    assert top1 and mid1
    assert not g1.nx.has_edge(top1, mid1)  # stale edge gone


def test_update_in_subdir_creates_no_ghost_duplicates(xrepo):
    """BUG-025 side-finding: a chunk whose files all live in a subdir used to infer the SUBDIR
    as its root and mint ghost duplicate node ids (and mis-rooted source_files that the merge
    then can't match). The update must land the edit under corpus-rooted ids."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(xrepo)
    _touch(
        xrepo / "sub" / "inner.py",
        "from sub.helper import help_fn\n\ndef deep2():\n    return help_fn()\n",
    )
    g1 = matrix.update(xrepo)
    deep2 = _node_id(g1, "sub/inner.py", "deep2()")
    helper = _node_id(g1, "sub/helper.py", "help_fn()")
    assert deep2 and helper  # corpus-rooted ids, edit landed
    assert _has_relation(g1, deep2, helper, "calls")
    assert _node_id(g1, "sub/inner.py", "deep()") is None  # stale symbol replaced
    ids_updated = set(g1.nx.nodes)
    g_scratch = matrix.build_graph(xrepo)
    assert ids_updated == set(g_scratch.nx.nodes)  # no ghosts, no missing nodes
    assert graph_sha256(g1) == graph_sha256(g_scratch)


def _edit_caller(src: Path) -> None:
    _touch(
        src / "f3.py",
        "from f2 import mid_fn\n\ndef top():\n    x = 1\n    return mid_fn() + x\n",
    )


def _edit_callee(src: Path) -> None:
    _touch(
        src / "f2.py",
        "from f1 import base_fn\n\ndef mid_fn():\n    return base_fn()\n\n"
        "def late_fn():\n    return 9\n",
    )


def _add_importer(src: Path) -> None:
    (src / "f9.py").write_text(
        "from f2 import mid_fn\n\ndef nine():\n    return mid_fn()\n", encoding="utf-8"
    )


def _delete_importer(src: Path) -> None:
    (src / "f3.py").unlink()


def _rename_caller(src: Path) -> None:
    text = (src / "f3.py").read_text(encoding="utf-8")
    (src / "f3.py").unlink()
    (src / "f3r.py").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "explicit_out_dir", [False, True], ids=["env-default", "explicit-out-dir"]
)
@pytest.mark.parametrize(
    "mutate",
    [
        _edit_caller,
        _edit_callee,
        _add_importer,
        _delete_importer,
        _rename_caller,
    ],
    ids=["caller-edit", "callee-edit", "add", "delete", "rename"],
)
def test_update_equals_scratch_build(xrepo, mutate, explicit_out_dir):
    """The equivalence CONTRACT: after any tree change, update() must produce the same
    canonical graph as a from-scratch build of the same tree (evidence-backed topology)
    — identically under the env-default artifact paths and an explicit out_dir."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    kwargs = {"out_dir": xrepo.parent / "explicit-out"} if explicit_out_dir else {}
    matrix.build_graph(xrepo, **kwargs)
    mutate(xrepo)
    g_updated = matrix.update(xrepo, **kwargs)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(
        xrepo, **kwargs
    )  # clobbers artifacts AFTER the update was hashed
    assert sha_updated == graph_sha256(g_scratch)


# ── BUG-036: incremental update on a SINGLE-SOURCE-ROOT corpus ───────────────────
#
# The xrepo fixtures above place source DIRECTLY under the repo root, so the corpus'
# common ancestor (matrix's inferred extraction root) equals the repo scan root and the
# incremental path's helpers happen to relativize identically either way. The corpus
# below hides that coincidence: its ENTIRE parsed source lives under one subdirectory
# (`src/`) with ROOTLESS imports (`from util import helper`), so the inferred root is
# `<repo>/src`, NOT the repo root. update() must still equal a from-scratch build — the
# incremental import-closure and prune must resolve against that inferred root, not the
# raw repo root, or a re-parsed file's cross-file edges vanish and a deleted file's nodes
# are never pruned.


@pytest.fixture()
def single_root_repo(tmp_path, monkeypatch):
    """A repo whose ENTIRE parsed source lives under one top-level dir (`src/`) with
    ROOTLESS imports (`from util import helper`), so _infer_root(discovered) == <repo>/src
    != repo_root — the layout the multi-rooted xrepo fixture never exercises (BUG-036)."""
    out = tmp_path / "out"  # OUTSIDE the scanned repo root
    monkeypatch.setenv("MATRIX_OUT", str(out))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    proj = tmp_path / "proj"
    src = proj / "src"
    src.mkdir(parents=True)
    (src / "util.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (src / "main.py").write_text(
        "from util import helper\n\ndef run():\n    return helper()\n", encoding="utf-8"
    )
    (src / "extra.py").write_text(
        "def solo():\n    return 9\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):  # padding: keep the closure under the half-corpus valve
        (src / f"pad{i}.py").write_text(
            f"def pad{i}():\n    return {i}\n", encoding="utf-8"
        )
    return proj  # repo root == proj; all source under proj/src


def _sr_edit_importer(src: Path) -> None:
    """Edit the importer (main.py) but KEEP the rootless import and the cross-file call."""
    _touch(
        src / "main.py",
        "from util import helper\n\ndef run():\n    x = 1\n    return helper() + x\n",
    )


def _sr_edit_callee(src: Path) -> None:
    """Edit the callee (util.py) — add a symbol; its importer is untouched."""
    _touch(
        src / "util.py", "def helper():\n    return 1\n\ndef helper2():\n    return 2\n"
    )


def _sr_add_file(src: Path) -> None:
    """A brand-new importer of the rootless callee — its out-import closure must resolve."""
    (src / "newmod.py").write_text(
        "from util import helper\n\ndef nine():\n    return helper()\n",
        encoding="utf-8",
    )


def _sr_delete_file(src: Path) -> None:
    (src / "extra.py").unlink()


def _sr_rename_importer(src: Path) -> None:
    """Delete+add of the importer — exercises the prune path AND a new file's closure."""
    text = (src / "main.py").read_text(encoding="utf-8")
    (src / "main.py").unlink()
    (src / "mainr.py").write_text(text, encoding="utf-8")


def _sr_noop(src: Path) -> None:
    pass


@pytest.mark.parametrize(
    "mutate",
    [
        _sr_edit_importer,
        _sr_edit_callee,
        _sr_add_file,
        _sr_delete_file,
        _sr_rename_importer,
        _sr_noop,
    ],
    ids=[
        "edit_importer",
        "edit_callee",
        "add_file",
        "delete_file",
        "rename_importer",
        "noop",
    ],
)
def test_update_equals_scratch_build_single_root(single_root_repo, mutate):
    """The BUG-036 equivalence leg: on a single-source-root rootless corpus, update() must
    still produce the identical canonical graph a from-scratch build produces."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(single_root_repo)
    mutate(single_root_repo / "src")
    g_updated = matrix.update(single_root_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(
        single_root_repo
    )  # clobbers artifacts AFTER the update was hashed
    assert sha_updated == graph_sha256(g_scratch)


def test_single_root_update_preserves_rootless_import_and_call_edges(single_root_repo):
    """BUG-036: editing the importer on a single-root rootless corpus must keep the exact two
    edges the scratch build has — main--imports-->util_helper and main_run--calls-->util_helper."""
    import hive.matrix as matrix

    matrix.build_graph(single_root_repo)
    _touch(
        single_root_repo / "src" / "main.py",
        "from util import helper\n\ndef run():\n    x = 1\n    return helper() + x\n",
    )
    g = matrix.update(single_root_repo)
    main = _node_id(g, "main.py", "main.py")
    helper = _node_id(g, "util.py", "helper()")
    run = _node_id(g, "main.py", "run()")
    assert main and helper and run
    assert _has_relation(g, main, helper, "imports")  # RED pre-fix
    assert _has_relation(g, run, helper, "calls")  # RED pre-fix


# ── BUG-036, second dialect: a QUALIFIED-import single-tree corpus ────────────────
#
# The divergence is a property of the LAYOUT (the inferred root is a subdir), not the
# import dialect: a repo whose source all lives under one top dir but uses PACKAGE-
# QUALIFIED imports (`src/pkg/b.py` doing `from pkg.a import f`) diverges the same way.
# For the qualified form to resolve, the inferred root must be `src` (its parent), so the
# corpus keeps padding at the `src/` level while the package lives under `src/pkg/`. The
# fix threads the inferred root regardless of dialect, so it must drive this leg GREEN too.


@pytest.fixture()
def single_root_pkg_repo(tmp_path, monkeypatch):
    """Source all under `src/` with a `pkg/` subpackage and QUALIFIED imports
    (`from pkg.a import f`); pads at the `src/` level force _infer_root == <repo>/src
    (not <repo>/src/pkg), so the qualified imports resolve — yet the inferred root is
    still a SUBDIR of repo_root, the BUG-036 trigger."""
    out = tmp_path / "out"
    monkeypatch.setenv("MATRIX_OUT", str(out))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    proj = tmp_path / "proj"
    src = proj / "src"
    pkg = src / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import f\n\ndef g():\n    return f()\n", encoding="utf-8"
    )
    (pkg / "c.py").write_text(
        "def solo():\n    return 9\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):  # pads at src/ level pull the inferred root up to src
        (src / f"pad{i}.py").write_text(
            f"def pad{i}():\n    return {i}\n", encoding="utf-8"
        )
    return proj


def _srp_edit_importer(pkg: Path) -> None:
    _touch(
        pkg / "b.py", "from pkg.a import f\n\ndef g():\n    x = 1\n    return f() + x\n"
    )


def _srp_edit_callee(pkg: Path) -> None:
    _touch(pkg / "a.py", "def f():\n    return 1\n\ndef f2():\n    return 2\n")


def _srp_add_file(pkg: Path) -> None:
    (pkg / "d.py").write_text(
        "from pkg.a import f\n\ndef nine():\n    return f()\n", encoding="utf-8"
    )


def _srp_delete_file(pkg: Path) -> None:
    (pkg / "c.py").unlink()


def _srp_rename_importer(pkg: Path) -> None:
    text = (pkg / "b.py").read_text(encoding="utf-8")
    (pkg / "b.py").unlink()
    (pkg / "br.py").write_text(text, encoding="utf-8")


def _srp_noop(pkg: Path) -> None:
    pass


@pytest.mark.parametrize(
    "mutate",
    [
        _srp_edit_importer,
        _srp_edit_callee,
        _srp_add_file,
        _srp_delete_file,
        _srp_rename_importer,
        _srp_noop,
    ],
    ids=[
        "edit_importer",
        "edit_callee",
        "add_file",
        "delete_file",
        "rename_importer",
        "noop",
    ],
)
def test_update_equals_scratch_build_single_root_pkg(single_root_pkg_repo, mutate):
    """BUG-036 is layout-complete, not rootless-specific: the qualified-import single-tree
    corpus must also satisfy update() == build_graph()."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(single_root_pkg_repo)
    mutate(single_root_pkg_repo / "src" / "pkg")
    g_updated = matrix.update(single_root_pkg_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(single_root_pkg_repo)
    assert sha_updated == graph_sha256(g_scratch)


# ── BUG-036 part 2: a NON-resolving qualified-import single-tree corpus ───────────
#
# The fixture above pads at the `src/` level, so _infer_root == <repo>/src and `from pkg.a
# import f` RESOLVES on disk — the callee file is pulled into the chunk by the import-closure.
# Drop the pads DOWN into `src/pkg/` and _infer_root == <repo>/src/pkg, so the qualified import
# resolves to a nonexistent <root>/pkg/a.py and NEVER pulls the callee file. The scratch build
# still binds b.py:g --calls--> a.py:f purely by NAME (f is the unique definition corpus-wide),
# so the incremental update must re-seed that callee-defining file from the caller's CURRENT call
# names or the name-bound edge is dropped on every touch of the importer — update() != build().


@pytest.fixture()
def single_root_pkg_repo_flat(tmp_path, monkeypatch):
    """Source ALL under `src/pkg/`, pads INCLUDED, so _infer_root == <repo>/src/pkg and the
    qualified import `from pkg.a import f` does NOT resolve on disk (it would need
    <root>/pkg/a.py). The scratch build binds b_g->a_f by name; the padding keeps the corpus
    large enough (7 files) that a one-file edit stays on the INCREMENTAL merge path rather than
    tripping the >half-corpus rebuild escape (which would make the equivalence contract vacuous)."""
    out = tmp_path / "out"
    monkeypatch.setenv("MATRIX_OUT", str(out))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)
    proj = tmp_path / "proj"
    src = proj / "src"
    pkg = src / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (pkg / "b.py").write_text(
        "from pkg.a import f\n\ndef g():\n    return f()\n", encoding="utf-8"
    )
    (pkg / "c.py").write_text(
        "def solo():\n    return 9\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):  # pads UNDER src/pkg keep _infer_root == src/pkg
        (pkg / f"pad{i}.py").write_text(
            f"def pad{i}():\n    return {i}\n", encoding="utf-8"
        )
    return proj


@pytest.mark.parametrize(
    "mutate",
    [
        _srp_edit_importer,
        _srp_edit_callee,
        _srp_add_file,
        _srp_delete_file,
        _srp_rename_importer,
        _srp_noop,
    ],
    ids=[
        "edit_importer",
        "edit_callee",
        "add_file",
        "delete_file",
        "rename_importer",
        "noop",
    ],
)
def test_update_equals_scratch_build_single_root_pkg_flat(
    single_root_pkg_repo_flat, mutate
):
    """The BUG-036 part-2 equivalence leg: on a single-source-root corpus whose qualified import
    does NOT resolve, update() must still produce the identical canonical graph a from-scratch
    build produces — the name-bound cross-file call is re-seeded, not dropped."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(single_root_pkg_repo_flat)
    mutate(single_root_pkg_repo_flat / "src" / "pkg")
    g_updated = matrix.update(single_root_pkg_repo_flat)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(single_root_pkg_repo_flat)
    assert sha_updated == graph_sha256(g_scratch)


def test_single_root_pkg_flat_preserves_name_call_edge(single_root_pkg_repo_flat):
    """The exact edge the non-resolving corpus binds by NAME: b.py:g --calls--> a.py:f (no import
    resolves, so only the corpus-wide unique-definition name pass forms it). An importer edit must
    keep it — the incremental closure re-seeds the callee file so the name re-binds."""
    import hive.matrix as matrix

    matrix.build_graph(single_root_pkg_repo_flat)
    _srp_edit_importer(single_root_pkg_repo_flat / "src" / "pkg")
    g = matrix.update(single_root_pkg_repo_flat)
    g_node = _node_id(g, "b.py", "g()")
    f_node = _node_id(g, "a.py", "f()")
    assert g_node and f_node
    assert _has_relation(
        g, g_node, f_node, "calls"
    )  # RED pre-fix (dropped on the incremental update)
