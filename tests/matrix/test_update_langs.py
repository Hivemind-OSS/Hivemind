"""BUG-036 part 2: the incremental closure must preserve NAME-resolved cross-file `calls`
edges for languages outside Python+JS (Go, C, C++, …).

`closure._file_imports` resolves cross-file imports only for `.py` and the JS family, so the
incremental extraction chunk pulls NO callee context for Go/C — and those extractors emit a
name-resolved cross-file `calls` edge (the corpus-wide unique-definition pass), which is dropped
on every `update()` that touches the caller alone. These fixtures place two same-language files
that cross-call by NAME (no import to resolve) at the repo root (a multi-root layout — the
inferred root IS the repo root), plus same-language padding so a one-file edit stays on the
INCREMENTAL merge path rather than the >half-corpus rebuild escape. update() must equal a
from-scratch build for every mutation, and the exact `b:g --calls--> a:f` edge must survive an
importer edit.

Kept in its OWN module so it never co-resides with hive-census in one pytest process (census
pins a process-global MATRIX_OUT before importing matrix).
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


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


def _isolate_matrix_out(tmp_path, monkeypatch) -> None:
    """Point MATRIX_OUT at a tmp dir OUTSIDE the scanned repo and reload matrix.paths so the
    override lands (paths bakes MATRIX_OUT into a constant at import time)."""
    monkeypatch.setenv("MATRIX_OUT", str(tmp_path / "out"))
    import hive.matrix.paths

    importlib.reload(hive.matrix.paths)


# ── Go: same-package cross-file call, repo-root (multi-root) layout ──────────────


@pytest.fixture()
def go_repo(tmp_path, monkeypatch):
    """a.go:f and b.go:g in one package at the repo root — g calls f by NAME (same package, no
    import). c.go is the delete target; the pads keep the corpus on the incremental path."""
    _isolate_matrix_out(tmp_path, monkeypatch)
    repo = tmp_path / "goproj"
    repo.mkdir()
    (repo / "a.go").write_text(
        "package p\n\nfunc f() int {\n\treturn 1\n}\n", encoding="utf-8"
    )
    (repo / "b.go").write_text(
        "package p\n\nfunc g() int {\n\treturn f()\n}\n", encoding="utf-8"
    )
    (repo / "c.go").write_text(
        "package p\n\nfunc solo() int {\n\treturn 9\n}\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):
        (repo / f"pad{i}.go").write_text(
            f"package p\n\nfunc pad{i}() int {{\n\treturn {i}\n}}\n", encoding="utf-8"
        )
    return repo


def _go_edit_importer(repo: Path) -> None:
    _touch(
        repo / "b.go", "package p\n\nfunc g() int {\n\tx := 1\n\treturn f() + x\n}\n"
    )


def _go_edit_callee(repo: Path) -> None:
    _touch(
        repo / "a.go",
        "package p\n\nfunc f() int {\n\treturn 1\n}\n\nfunc h() int {\n\treturn 2\n}\n",
    )


def _go_add_file(repo: Path) -> None:
    (repo / "d.go").write_text(
        "package p\n\nfunc nine() int {\n\treturn f()\n}\n", encoding="utf-8"
    )


def _go_delete_file(repo: Path) -> None:
    (repo / "c.go").unlink()


def _go_rename_importer(repo: Path) -> None:
    text = (repo / "b.go").read_text(encoding="utf-8")
    (repo / "b.go").unlink()
    (repo / "br.go").write_text(text, encoding="utf-8")


def _noop(repo: Path) -> None:
    pass


@pytest.mark.parametrize(
    "mutate",
    [
        _go_edit_importer,
        _go_edit_callee,
        _go_add_file,
        _go_delete_file,
        _go_rename_importer,
        _noop,
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
def test_update_equals_scratch_build_go(go_repo, mutate):
    """Go name-resolved cross-file calls: update() must equal a from-scratch build for every
    mutation (RED pre-fix on edit_importer/add_file/rename_importer)."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(go_repo)
    mutate(go_repo)
    g_updated = matrix.update(go_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(go_repo)
    assert sha_updated == graph_sha256(g_scratch)


def test_go_preserves_name_call_edge(go_repo):
    """Go's b.go:g --calls--> a.go:f binds by NAME; an importer edit must keep it."""
    import hive.matrix as matrix

    matrix.build_graph(go_repo)
    _go_edit_importer(go_repo)
    g = matrix.update(go_repo)
    g_node = _node_id(g, "b.go", "g()")
    f_node = _node_id(g, "a.go", "f()")
    assert g_node and f_node
    assert _has_relation(g, g_node, f_node, "calls")  # RED pre-fix


# ── C: cross-file call across a forward declaration, repo-root layout ────────────


@pytest.fixture()
def c_repo(tmp_path, monkeypatch):
    """a.c:f and b.c:g at the repo root — g calls f (forward-declared in b.c), bound by NAME to
    the sole definition in a.c. c.c is the delete target; the pads keep the incremental path."""
    _isolate_matrix_out(tmp_path, monkeypatch)
    repo = tmp_path / "cproj"
    repo.mkdir()
    (repo / "a.c").write_text("int f(void) {\n\treturn 1;\n}\n", encoding="utf-8")
    (repo / "b.c").write_text(
        "int f(void);\n\nint g(void) {\n\treturn f();\n}\n", encoding="utf-8"
    )
    (repo / "c.c").write_text(
        "int solo(void) {\n\treturn 9;\n}\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):
        (repo / f"pad{i}.c").write_text(
            f"int pad{i}(void) {{\n\treturn {i};\n}}\n", encoding="utf-8"
        )
    return repo


def _c_edit_importer(repo: Path) -> None:
    _touch(
        repo / "b.c",
        "int f(void);\n\nint g(void) {\n\tint x = 1;\n\treturn f() + x;\n}\n",
    )


def _c_edit_callee(repo: Path) -> None:
    _touch(
        repo / "a.c", "int f(void) {\n\treturn 1;\n}\n\nint h(void) {\n\treturn 2;\n}\n"
    )


def _c_add_file(repo: Path) -> None:
    (repo / "d.c").write_text(
        "int f(void);\n\nint nine(void) {\n\treturn f();\n}\n", encoding="utf-8"
    )


def _c_delete_file(repo: Path) -> None:
    (repo / "c.c").unlink()


def _c_rename_importer(repo: Path) -> None:
    text = (repo / "b.c").read_text(encoding="utf-8")
    (repo / "b.c").unlink()
    (repo / "br.c").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        _c_edit_importer,
        _c_edit_callee,
        _c_add_file,
        _c_delete_file,
        _c_rename_importer,
        _noop,
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
def test_update_equals_scratch_build_c(c_repo, mutate):
    """C name-resolved cross-file calls: update() must equal a from-scratch build for every
    mutation (RED pre-fix on edit_importer/add_file/rename_importer)."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(c_repo)
    mutate(c_repo)
    g_updated = matrix.update(c_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(c_repo)
    assert sha_updated == graph_sha256(g_scratch)


def test_c_preserves_name_call_edge(c_repo):
    """C's b.c:g --calls--> a.c:f binds by NAME; an importer edit must keep it."""
    import hive.matrix as matrix

    matrix.build_graph(c_repo)
    _c_edit_importer(c_repo)
    g = matrix.update(c_repo)
    g_node = _node_id(g, "b.c", "g()")
    f_node = _node_id(g, "a.c", "f()")
    assert g_node and f_node
    assert _has_relation(g, g_node, f_node, "calls")  # RED pre-fix


# ── SQL: cross-file table reference (FK + view), repo-root layout ────────────────


@pytest.fixture()
def sql_repo(tmp_path, monkeypatch):
    """a.sql defines `users`; b.sql defines `orders` (whose FK REFERENCES users) and a
    view `user_orders` (which reads FROM users) — both cross-file references bound by
    NAME (SQL emits no import and no raw_call). c.sql is the delete target; the pads
    keep the corpus on the incremental merge path rather than the >half-corpus rebuild."""
    _isolate_matrix_out(tmp_path, monkeypatch)
    repo = tmp_path / "sqlproj"
    repo.mkdir()
    (repo / "a.sql").write_text(
        "CREATE TABLE users (\n    id INTEGER PRIMARY KEY,\n    name TEXT\n);\n",
        encoding="utf-8",
    )
    (repo / "b.sql").write_text(
        "CREATE TABLE orders (\n    id INTEGER PRIMARY KEY,\n"
        "    user_id INTEGER REFERENCES users(id)\n);\n\n"
        "CREATE VIEW user_orders AS\nSELECT u.name FROM users u;\n",
        encoding="utf-8",
    )
    (repo / "c.sql").write_text(
        "CREATE TABLE solo (\n    id INTEGER PRIMARY KEY\n);\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):
        (repo / f"pad{i}.sql").write_text(
            f"CREATE TABLE pad{i} (\n    id INTEGER PRIMARY KEY\n);\n", encoding="utf-8"
        )
    return repo


def _sql_edit_importer(repo: Path) -> None:
    _touch(
        repo / "b.sql",
        "CREATE TABLE orders (\n    id INTEGER PRIMARY KEY,\n    note TEXT,\n"
        "    user_id INTEGER REFERENCES users(id)\n);\n\n"
        "CREATE VIEW user_orders AS\nSELECT u.name FROM users u;\n",
    )


def _sql_edit_callee(repo: Path) -> None:
    _touch(
        repo / "a.sql",
        "CREATE TABLE users (\n    id INTEGER PRIMARY KEY,\n    name TEXT\n);\n\n"
        "CREATE TABLE audit (\n    id INTEGER PRIMARY KEY\n);\n",
    )


def _sql_add_file(repo: Path) -> None:
    (repo / "d.sql").write_text(
        "CREATE TABLE invoices (\n    id INTEGER PRIMARY KEY,\n"
        "    user_id INTEGER REFERENCES users(id)\n);\n",
        encoding="utf-8",
    )


def _sql_delete_file(repo: Path) -> None:
    (repo / "c.sql").unlink()


def _sql_rename_importer(repo: Path) -> None:
    text = (repo / "b.sql").read_text(encoding="utf-8")
    (repo / "b.sql").unlink()
    (repo / "br.sql").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        _sql_edit_importer,
        _sql_edit_callee,
        _sql_add_file,
        _sql_delete_file,
        _sql_rename_importer,
        _noop,
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
def test_update_equals_scratch_build_sql(sql_repo, mutate):
    """SQL cross-file table references: update() must equal a from-scratch build for
    every mutation. RED against sql.py fixed but the closure seed absent (the re-extracted
    referencer's stub cannot rebind without its table-defining file in the chunk)."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(sql_repo)
    mutate(sql_repo)
    g_updated = matrix.update(sql_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(sql_repo)
    assert sha_updated == graph_sha256(g_scratch)


def test_sql_preserves_cross_file_reference_edge(sql_repo):
    """b.sql:orders --references--> a.sql:users binds by NAME across files; an importer
    edit must keep it bound to the REAL cross-file table node, not a phantom id."""
    import hive.matrix as matrix

    matrix.build_graph(sql_repo)
    _sql_edit_importer(sql_repo)
    g = matrix.update(sql_repo)
    orders = _node_id(g, "b.sql", "orders")
    users = _node_id(g, "a.sql", "users")
    assert orders and users
    assert _has_relation(g, orders, users, "references")  # RED pre-fix (phantom target)


# ── Rust: same-crate cross-file call by name, repo-root (multi-root) layout ──────


@pytest.fixture()
def rust_repo(tmp_path, monkeypatch):
    """a.rs:f and b.rs:g at the repo root — g calls f() by NAME, bound to the sole definition in
    a.rs (matrix's corpus resolver binds the bare cross-file call — the extractor needs no `use`).
    c.rs is the delete target; the pads keep the corpus on the incremental path."""
    _isolate_matrix_out(tmp_path, monkeypatch)
    repo = tmp_path / "rustproj"
    repo.mkdir()
    (repo / "a.rs").write_text("fn f() -> i32 {\n    1\n}\n", encoding="utf-8")
    (repo / "b.rs").write_text("fn g() -> i32 {\n    f()\n}\n", encoding="utf-8")
    (repo / "c.rs").write_text(
        "fn solo() -> i32 {\n    9\n}\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):
        (repo / f"pad{i}.rs").write_text(
            f"fn pad{i}() -> i32 {{\n    {i}\n}}\n", encoding="utf-8"
        )
    return repo


def _rust_edit_importer(repo: Path) -> None:
    _touch(repo / "b.rs", "fn g() -> i32 {\n    let x = 1;\n    f() + x\n}\n")


def _rust_edit_callee(repo: Path) -> None:
    _touch(repo / "a.rs", "fn f() -> i32 {\n    1\n}\n\nfn h() -> i32 {\n    2\n}\n")


def _rust_add_file(repo: Path) -> None:
    (repo / "d.rs").write_text("fn nine() -> i32 {\n    f()\n}\n", encoding="utf-8")


def _rust_delete_file(repo: Path) -> None:
    (repo / "c.rs").unlink()


def _rust_rename_importer(repo: Path) -> None:
    text = (repo / "b.rs").read_text(encoding="utf-8")
    (repo / "b.rs").unlink()
    (repo / "br.rs").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        _rust_edit_importer,
        _rust_edit_callee,
        _rust_add_file,
        _rust_delete_file,
        _rust_rename_importer,
        _noop,
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
def test_update_equals_scratch_build_rust(rust_repo, mutate):
    """Rust name-resolved cross-file calls: update() must equal a from-scratch build for every
    mutation."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(rust_repo)
    mutate(rust_repo)
    g_updated = matrix.update(rust_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(rust_repo)
    assert sha_updated == graph_sha256(g_scratch)


def test_rust_preserves_name_call_edge(rust_repo):
    """Rust's b.rs:g --calls--> a.rs:f binds by NAME; an importer edit must keep it."""
    import hive.matrix as matrix

    matrix.build_graph(rust_repo)
    _rust_edit_importer(rust_repo)
    g = matrix.update(rust_repo)
    g_node = _node_id(g, "b.rs", "g()")
    f_node = _node_id(g, "a.rs", "f()")
    assert g_node and f_node
    assert _has_relation(g, g_node, f_node, "calls")


# ── C++: cross-file call across a forward declaration, repo-root layout ──────────


@pytest.fixture()
def cpp_repo(tmp_path, monkeypatch):
    """a.cpp:f and b.cpp:g at the repo root — g calls f (forward-declared in b.cpp), bound by NAME
    to the sole definition in a.cpp. c.cpp is the delete target; the pads keep the incremental path."""
    _isolate_matrix_out(tmp_path, monkeypatch)
    repo = tmp_path / "cppproj"
    repo.mkdir()
    (repo / "a.cpp").write_text("int f() {\n\treturn 1;\n}\n", encoding="utf-8")
    (repo / "b.cpp").write_text(
        "int f();\n\nint g() {\n\treturn f();\n}\n", encoding="utf-8"
    )
    (repo / "c.cpp").write_text(
        "int solo() {\n\treturn 9;\n}\n", encoding="utf-8"
    )  # delete target
    for i in (1, 2, 3, 4):
        (repo / f"pad{i}.cpp").write_text(
            f"int pad{i}() {{\n\treturn {i};\n}}\n", encoding="utf-8"
        )
    return repo


def _cpp_edit_importer(repo: Path) -> None:
    _touch(
        repo / "b.cpp", "int f();\n\nint g() {\n\tint x = 1;\n\treturn f() + x;\n}\n"
    )


def _cpp_edit_callee(repo: Path) -> None:
    _touch(repo / "a.cpp", "int f() {\n\treturn 1;\n}\n\nint h() {\n\treturn 2;\n}\n")


def _cpp_add_file(repo: Path) -> None:
    (repo / "d.cpp").write_text(
        "int f();\n\nint nine() {\n\treturn f();\n}\n", encoding="utf-8"
    )


def _cpp_delete_file(repo: Path) -> None:
    (repo / "c.cpp").unlink()


def _cpp_rename_importer(repo: Path) -> None:
    text = (repo / "b.cpp").read_text(encoding="utf-8")
    (repo / "b.cpp").unlink()
    (repo / "br.cpp").write_text(text, encoding="utf-8")


@pytest.mark.parametrize(
    "mutate",
    [
        _cpp_edit_importer,
        _cpp_edit_callee,
        _cpp_add_file,
        _cpp_delete_file,
        _cpp_rename_importer,
        _noop,
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
def test_update_equals_scratch_build_cpp(cpp_repo, mutate):
    """C++ name-resolved cross-file calls: update() must equal a from-scratch build for every
    mutation."""
    import hive.matrix as matrix
    from hive.matrix.version import graph_sha256

    matrix.build_graph(cpp_repo)
    mutate(cpp_repo)
    g_updated = matrix.update(cpp_repo)
    sha_updated = graph_sha256(g_updated)
    g_scratch = matrix.build_graph(cpp_repo)
    assert sha_updated == graph_sha256(g_scratch)


def test_cpp_preserves_name_call_edge(cpp_repo):
    """C++'s b.cpp:g --calls--> a.cpp:f binds by NAME; an importer edit must keep it."""
    import hive.matrix as matrix

    matrix.build_graph(cpp_repo)
    _cpp_edit_importer(cpp_repo)
    g = matrix.update(cpp_repo)
    g_node = _node_id(g, "b.cpp", "g()")
    f_node = _node_id(g, "a.cpp", "f()")
    assert g_node and f_node
    assert _has_relation(g, g_node, f_node, "calls")
