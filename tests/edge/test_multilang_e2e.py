"""Edge-level mint -> edit -> verify E2E across the non-Python languages.

The Python edge path is exercised in `test_cli.py` / `test_subgraph_fp.py`; this
module extends the SAME mint/verify seam (`hive-edge mint` -> `_mint_core` U
`_subgraph_fp_core`; `hive-edge verify` -> `_verify_core`) to SQL, Go, Rust, C,
and C++ — one parametrized suite over a real tmp source tree per language.

Two tiers ride every case, and the split between them is the point:

  - the VERDICT is Tier-1 combdrift (`verify_record`): current / stale
    (signature_changed or symbol_missing) / unverifiable. For Go/Rust/C/C++ the
    interface fingerprint drives it (a breaking arity change goes stale, a purely
    additive one stays current); SQL carries no Layer-B interface, so existence
    at table granularity is its only staleness signal (a dropped table is stale).
  - the RADIUS is the Tier-2 matrix `subgraph_fp` advisory. It moves when the
    anchor's dependency NEIGHBORHOOD moves (a caller's signature, a new
    referencing table) and holds when an unrelated sibling changes — but it NEVER
    moves the Tier-1 verdict in either direction. `test_radius_advisory_never_...`
    is the load-bearing assertion of that separation.

Portability (device-invariance): the same tree at two different absolute roots
mints the IDENTICAL token — no checkout path leaks in — and a token whose engine
version this build cannot read degrades to `unverifiable` (incomparable), never a
false `stale`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from hive.edge.cli import main


@dataclass
class LangCase:
    """One language's real source tree plus the edits each tier reacts to.

    `tree` maps a repo-relative filename to its source; it holds the anchor's
    file AND (for the fingerprinted languages) a caller file, so the anchor has a
    genuine dependency neighborhood for the radius tier. Every edit is a
    `(filename, old, new)` string substitution except `drop`, which replaces a
    file wholesale to delete the anchor symbol.
    """

    lang: str
    anchor: str
    tree: dict[str, str]
    fingerprinted: bool  # True iff combdrift mints an interface fingerprint (not SQL)
    drop: tuple[
        str, str
    ]  # (filename, replacement source) that deletes the anchor symbol
    neighbor: tuple[
        str, str, str
    ]  # an edit inside the cone: moves the radius, not the anchor
    breaking: tuple[str, str, str] | None = (
        None  # an anchor edit that breaks its call shape
    )
    nonbreaking: tuple[str, str, str] | None = None  # an anchor edit that does not
    sibling: tuple[str, str, str] | None = (
        None  # an edit OUTSIDE the cone: radius must hold
    )


_GO = LangCase(
    lang="go",
    anchor="svc.go:Greet",
    tree={
        "svc.go": 'package main\n\nfunc Greet(name string) string {\n\treturn "hi " + name\n}\n\n'
        "func lonely(a int) int {\n\treturn a\n}\n",
        "main.go": 'package main\n\nfunc Run() string {\n\treturn Greet("x")\n}\n',
    },
    fingerprinted=True,
    # A new REQUIRED parameter strands the minimal previously-valid call -> breaking.
    breaking=(
        "svc.go",
        "func Greet(name string) string {",
        "func Greet(name string, times int) string {",
    ),
    # A trailing variadic absorbs the old call unchanged -> additive, not breaking.
    nonbreaking=(
        "svc.go",
        "func Greet(name string) string {",
        "func Greet(name string, extra ...string) string {",
    ),
    drop=("svc.go", "package main\n\nfunc lonely(a int) int {\n\treturn a\n}\n"),
    neighbor=("main.go", "func Run() string {", "func Run(n int) string {"),
    sibling=("svc.go", "func lonely(a int) int {", "func lonely(a int, b int) int {"),
)

_RUST = LangCase(
    lang="rust",
    anchor="svc.rs:greet",
    tree={
        "svc.rs": 'fn greet(name: &str) -> String {\n    format!("hi {}", name)\n}\n\n'
        "fn lonely(a: i32) -> i32 {\n    a\n}\n",
        "main.rs": 'fn run() -> String {\n    greet("x")\n}\n',
    },
    fingerprinted=True,
    breaking=(
        "svc.rs",
        "fn greet(name: &str) -> String {",
        "fn greet(name: &str, times: u32) -> String {",
    ),
    # Genericizing the function adds a `<T>` type parameter, which lives outside the
    # call arity — combdrift treats it as non-drift, so the verdict stays current.
    nonbreaking=(
        "svc.rs",
        "fn greet(name: &str) -> String {",
        "fn greet<T>(name: &str) -> String {",
    ),
    drop=("svc.rs", "fn lonely(a: i32) -> i32 {\n    a\n}\n"),
    neighbor=("main.rs", "fn run() -> String {", "fn run(n: i32) -> String {"),
    sibling=(
        "svc.rs",
        "fn lonely(a: i32) -> i32 {",
        "fn lonely(a: i32, b: i32) -> i32 {",
    ),
)

_C = LangCase(
    lang="c",
    anchor="svc.c:addup",
    tree={
        "svc.c": "int addup(int a) {\n    return a;\n}\n\nint lonely(int a) {\n    return a;\n}\n",
        "main.c": "extern int addup(int);\n\nint run(void) {\n    return addup(1);\n}\n",
    },
    fingerprinted=True,
    breaking=("svc.c", "int addup(int a) {", "int addup(int a, int b) {"),
    # A trailing `...` variadic absorbs the old call unchanged -> additive.
    nonbreaking=("svc.c", "int addup(int a) {", "int addup(int a, ...) {"),
    drop=("svc.c", "int lonely(int a) {\n    return a;\n}\n"),
    neighbor=("main.c", "int run(void) {", "int run(int z) {"),
    sibling=("svc.c", "int lonely(int a) {", "int lonely(int a, int b) {"),
)

_CPP = LangCase(
    lang="cpp",
    anchor="svc.cpp:addup",
    tree={
        "svc.cpp": "int addup(int a) {\n    return a;\n}\n\nint lonely(int a) {\n    return a;\n}\n",
        "main.cpp": "int addup(int);\n\nint run() {\n    return addup(1);\n}\n",
    },
    fingerprinted=True,
    breaking=("svc.cpp", "int addup(int a) {", "int addup(int a, int b) {"),
    # A defaulted parameter raises max but not required arity -> additive.
    nonbreaking=("svc.cpp", "int addup(int a) {", "int addup(int a, int b = 0) {"),
    drop=("svc.cpp", "int lonely(int a) {\n    return a;\n}\n"),
    neighbor=("main.cpp", "int run() {", "int run(int z) {"),
    sibling=("svc.cpp", "int lonely(int a) {", "int lonely(int a, int b) {"),
)

_SQL = LangCase(
    lang="sql",
    anchor="db/schema.sql:orders",
    tree={
        "db/schema.sql": "CREATE TABLE users (\n    id INTEGER PRIMARY KEY,\n    name TEXT\n);\n\n\n"
        "CREATE TABLE orders (\n    id INTEGER PRIMARY KEY,\n    user_id INTEGER REFERENCES users\n);\n",
    },
    fingerprinted=False,
    drop=(
        "db/schema.sql",
        "CREATE TABLE users (\n    id INTEGER PRIMARY KEY,\n    name TEXT\n);\n",
    ),
    # A new table that REFERENCES orders joins orders' neighborhood -> radius moves.
    neighbor=(
        "db/schema.sql",
        "CREATE TABLE users (",
        "CREATE TABLE audit (\n    id INTEGER PRIMARY KEY,\n    order_id INTEGER REFERENCES orders\n);\n\n\nCREATE TABLE users (",
    ),
)

_ALL_CASES = [_SQL, _GO, _RUST, _C, _CPP]
_FP_CASES = [c for c in _ALL_CASES if c.fingerprinted]


def _ids(cases):
    return [c.lang for c in cases]


# ── helpers ──────────────────────────────────────────────────────────────────


def _run(argv, capsys):
    """Invoke the CLI, returning (exit_code, parsed_stdout_json_or_raw)."""
    code = main(argv)
    cap = capsys.readouterr()
    try:
        return code, json.loads(cap.out)
    except json.JSONDecodeError:
        return code, cap.out


def _materialize(tmp_path: Path, case: LangCase) -> Path:
    """Write `case.tree` under a fresh repo root and return it."""
    repo = tmp_path / f"{case.lang}repo"
    for rel, src in case.tree.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(src, encoding="utf-8")
    return repo


def _apply(repo: Path, edit: tuple[str, str, str]) -> None:
    """Apply a (filename, old, new) substitution, asserting the anchor text exists."""
    filename, old, new = edit
    path = repo / filename
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} not in {filename}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _mint(repo: Path, case: LangCase, capsys) -> dict:
    """Drive `hive-edge mint` and return its fingerprint-union map."""
    code, out = _run(["mint", "--repo", str(repo), "--anchor", case.anchor], capsys)
    assert code == 0 and isinstance(out, dict)
    return out


def _verify(
    repo: Path, case: LangCase, capsys, *, fp: str | None = None, sub: str | None = None
) -> dict:
    """Drive `hive-edge verify` (verdict + optional radius) and return its map."""
    argv = ["verify", "--repo", str(repo), "--anchor", case.anchor]
    if fp is not None:
        argv += ["--fp", fp]
    if sub is not None:
        argv += ["--subgraph-fp", sub]
    code, out = _run(argv, capsys)
    assert code == 0 and isinstance(out, dict)
    return out


# ── mint -> verify (fresh) ───────────────────────────────────────────────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_ids(_ALL_CASES))
def test_mint_then_verify_live_is_current(tmp_path, capsys, case):
    # Every language mints a matrix/subgraph_fp; the fingerprinted ones also mint
    # a combdrift/fp interface token (SQL carries no Layer-B interface, so its
    # union is the neighborhood token alone). Re-verifying the untouched tree is
    # current — never a false stale on fresh code.
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    assert "matrix/subgraph_fp" in minted
    fp = minted.get("combdrift/fp")
    assert (fp is not None) == case.fingerprinted
    out = _verify(repo, case, capsys, fp=fp)
    assert out["verdict"] == "current" and out["reason"] == "ok"
    assert "remediation" not in out


# ── mint -> breaking edit -> stale (interface languages) ─────────────────────


@pytest.mark.parametrize("case", _FP_CASES, ids=_ids(_FP_CASES))
def test_mint_then_breaking_edit_is_stale_with_delta(tmp_path, capsys, case):
    # The headline mint -> edit -> verify: a breaking arity change to the anchor's
    # own signature makes the recorded fingerprint no longer satisfiable, so the
    # Tier-1 verdict is stale (signature_changed) and carries the breaking delta.
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    fp = minted["combdrift/fp"]
    _apply(repo, case.breaking)
    out = _verify(repo, case, capsys, fp=fp)
    assert out["verdict"] == "stale" and out["reason"] == "signature_changed"
    assert out["delta"]["old_token"] == fp
    assert out["delta"]["breaking"] is True
    assert "remediation" in out


@pytest.mark.parametrize("case", _FP_CASES, ids=_ids(_FP_CASES))
def test_mint_then_nonbreaking_edit_stays_current(tmp_path, capsys, case):
    # An additive / non-arity-breaking edit to the anchor breaks no previously-valid
    # call, so the verdict stays current — the false-stale floor for a real edit.
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    fp = minted["combdrift/fp"]
    _apply(repo, case.nonbreaking)
    out = _verify(repo, case, capsys, fp=fp)
    assert out["verdict"] == "current"
    assert "remediation" not in out and "delta" not in out


# ── mint -> delete the anchor symbol -> stale (existence; SQL's only signal) ──


@pytest.mark.parametrize("case", _ALL_CASES, ids=_ids(_ALL_CASES))
def test_deleting_the_anchor_symbol_is_stale(tmp_path, capsys, case):
    # Existence staleness, uniform across the tiers: a deleted function (Go/Rust/
    # C/C++) or a dropped table (SQL) is provably absent from its file -> stale
    # (symbol_missing). This is SQL's ONLY staleness signal — it has no interface.
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    fp = minted.get("combdrift/fp")
    (repo / case.drop[0]).write_text(case.drop[1], encoding="utf-8")
    out = _verify(repo, case, capsys, fp=fp)
    assert out["verdict"] == "stale" and out["reason"] == "symbol_missing"
    assert "remediation" in out


# ── portability: identical token across checkout roots ───────────────────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_ids(_ALL_CASES))
def test_portable_identical_token_across_checkout_roots(tmp_path, capsys, case):
    # Device-invariance: minting the identical tree at two DIFFERENT absolute roots
    # yields byte-identical tokens for BOTH fingerprints — no checkout path rides
    # into a token, so a token minted on one device verifies on another.
    repo_a = _materialize(tmp_path / "root-a", case)
    repo_b = _materialize(tmp_path / "root-b", case)
    minted_a = _mint(repo_a, case, capsys)
    minted_b = _mint(repo_b, case, capsys)
    assert minted_a == minted_b
    for token in minted_a.values():
        assert str(repo_a) not in token and "root-a" not in token


# ── portability: an unreadable engine version degrades safely ────────────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_ids(_ALL_CASES))
def test_unknown_fingerprint_version_degrades_to_unverifiable(tmp_path, capsys, case):
    # The distributed-topology safety valve: a stored token whose engine/grammar
    # version this build cannot parse is INCOMPARABLE, never a false break. Verify
    # reports unverifiable (bias toward keep), NEVER stale. Modeled by bumping the
    # minted token's version tag to an unknown one; a schema anchor rejects any
    # fingerprint the same way (it carries no comparable interface).
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    if case.fingerprinted:
        stored = minted["combdrift/fp"].replace(
            "combdrift-fp/1:", "combdrift-fp/999:", 1
        )
    else:
        stored = (
            "combdrift-fp/999:func(req=1,max=1,star=0,kw=0,kwo=0,gen=0,dec=,base=0)"
        )
    out = _verify(repo, case, capsys, fp=stored)
    assert out["verdict"] == "unverifiable"
    assert out["reason"] == "fingerprint_version_mismatch"
    assert out["verdict"] != "stale" and "remediation" not in out


# ── the radius tier is a Tier-2 advisory that never moves the verdict ────────


@pytest.mark.parametrize("case", _ALL_CASES, ids=_ids(_ALL_CASES))
def test_radius_advisory_never_flips_verdict(tmp_path, capsys, case):
    # The load-bearing separation. With both fingerprints stored:
    #   1. fresh tree            -> verdict current, radius current;
    #   2. neighbor moves (a caller signature / a new referencing table) while the
    #      anchor's OWN shape is untouched -> radius CHANGED, yet verdict stays
    #      current: a changed neighborhood is never promoted into a stale verdict;
    #   3. an unrelated sibling outside the cone moves -> radius stays current
    #      (the advisory is scoped, not whole-tree noise).
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    fp = minted.get("combdrift/fp")
    sub = minted["matrix/subgraph_fp"]

    fresh = _verify(repo, case, capsys, fp=fp, sub=sub)
    assert fresh["verdict"] == "current" and fresh["radius"] == "current"

    _apply(repo, case.neighbor)
    moved = _verify(repo, case, capsys, fp=fp, sub=sub)
    # // the radius advisory changed but the Tier-1 verdict did NOT follow it.
    assert moved["verdict"] == "current" and moved["radius"] == "changed"
    assert "remediation" not in moved

    if case.sibling is not None:
        repo2 = _materialize(tmp_path / "sib", case)
        _apply(repo2, case.sibling)
        held = _verify(repo2, case, capsys, fp=fp, sub=sub)
        assert held["verdict"] == "current" and held["radius"] == "current"


@pytest.mark.parametrize("case", _FP_CASES, ids=_ids(_FP_CASES))
def test_stale_verdict_survives_a_changed_radius(tmp_path, capsys, case):
    # The other direction of the separation: the anchor's own signature breaks
    # (Tier-1 stale) AND its neighborhood moves. The stale payload — verdict,
    # reason, remediation, breaking delta — stays fully intact; the changed radius
    # neither suppresses nor rewrites it.
    minted = _mint(repo := _materialize(tmp_path, case), case, capsys)
    fp = minted["combdrift/fp"]
    sub = minted["matrix/subgraph_fp"]
    _apply(repo, case.breaking)  # break the anchor itself
    _apply(repo, case.neighbor)  # and move the neighborhood
    out = _verify(repo, case, capsys, fp=fp, sub=sub)
    assert out["verdict"] == "stale" and out["reason"] == "signature_changed"
    assert out["radius"] == "changed"
    assert out["delta"]["breaking"] is True
    assert "remediation" in out
