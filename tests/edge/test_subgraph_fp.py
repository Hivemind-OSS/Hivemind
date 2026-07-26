"""The subgraph-fingerprint contracts — these ARE the feature's spec.

`_subgraph_fp_core` mints one opaque token for a `path:symbol` anchor's dependency
NEIGHBORHOOD (the D1 union cone: seed ∪ forward reach ∪ reverse reach over the
dependency relations). The token moves iff something inside that neighborhood
changed — a callee's signature (forward), a caller's (reverse), an in-place member
signature, a same-shape rename — and NEVER for an unrelated sibling edit, an
unrelated same-named symbol elsewhere (F4), or a checkout at a different absolute
path (BUG-021). Prose anchors gate to `{}` (BUG-022); uncovered languages degrade
member shapes to `identity` but keep membership live (F7). Fail-open throughout:
the core never raises.

Two fixture families pin the path-dialect bridge from both sides. matrix infers
its extraction root as the corpus' common ancestor, so node `source_file` paths
are root-relative, while anchors are ALWAYS repo-root-relative. The multi-root
trees (two top-level entries — the inferred root IS the repo root, offset "")
pin that the bridge is byte-inert there; the single-root trees (ALL parsed
source under one top dir — `source_file` drops that prefix) pin that a natural
repo-root-relative anchor still mints, moves, and verifies identically.
"""

from __future__ import annotations

import json
from pathlib import Path

import hive.combdrift as combdrift
import pytest

from hive.edge import cli
from hive.edge.cli import main

_ANCHOR = "pkg/svc.py:mid_fn"

_MAIN_PY = """\
from pkg.svc import mid_fn


def entry():
    return mid_fn(1)
"""

_SVC_PY = """\
def helper(x):
    return x * 2


class Service:
    def run(self, a, b):
        return helper(a) + b


def mid_fn(v):
    return helper(v)


def lonely(a):
    return a
"""

_SCHEMA_SQL = """\
CREATE TABLE users (
    id INTEGER PRIMARY KEY,
    name TEXT
);


CREATE TABLE orders (
    id INTEGER PRIMARY KEY,
    user_id INTEGER REFERENCES users
);
"""


def _write_tree(root: Path, *, with_sql: bool = False) -> Path:
    """main.py:entry -> pkg/svc.py:mid_fn -> helper; Service.run -> helper; `lonely` is the
    unrelated sibling. Three inert pad files keep the corpus large enough that a one-file edit
    stays on matrix's INCREMENTAL merge path (a tiny corpus trips the >half-corpus rebuild
    escape and would make the update-vs-scratch contract vacuous). `with_sql` adds a schema
    (`db/schema.sql`: orders --FK--> users) — combdrift resolves SQL only for table+column
    EXISTENCE and carries no Layer-B interface fingerprint for it, so its members render the
    `identity` shape while membership stays live."""
    (root / "pkg").mkdir(parents=True)
    (root / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    (root / "pkg" / "svc.py").write_text(_SVC_PY, encoding="utf-8")
    for name in ("pad_a", "pad_b", "pad_c"):
        (root / f"{name}.py").write_text(
            f"def {name}():\n    return 0\n", encoding="utf-8"
        )
    if with_sql:
        (root / "db").mkdir()
        (root / "db" / "schema.sql").write_text(_SCHEMA_SQL, encoding="utf-8")
    return root


@pytest.fixture
def graph_repo(tmp_path):
    return _write_tree(tmp_path / "graphrepo")


def _edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"fixture drift: {old!r} not in {rel}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def _fp_map(repo: Path, anchor: str = _ANCHOR) -> dict:
    return cli._subgraph_fp_core(str(repo), anchor)


def _fp(repo: Path, anchor: str = _ANCHOR) -> str:
    out = _fp_map(repo, anchor)
    assert set(out) == {"matrix/subgraph_fp"}, out
    return out["matrix/subgraph_fp"]


def _anchor_fp(repo: Path, anchor: str = _ANCHOR) -> str:
    """The ANCHOR tier's own interface fingerprint, minted from the current tree — the `--fp`
    every radius test stores beside its subgraph token. An anchor carrying no fingerprint has
    no call shape to compare against, so it reads `unverifiable`; only a genuinely COMPARED
    anchor reads `current`, which is what the radius tier must be shown not to move."""
    path, symbol = cli._split_anchor(anchor)
    token = combdrift.fingerprint_anchor(str(repo), combdrift.Anchor(path, symbol))
    assert isinstance(token, str), f"no interface fingerprint for {anchor}"
    return token


def _tokens(repo: Path, anchor: str = _ANCHOR) -> list[str]:
    graph = cli._load_graph(str(repo))
    assert graph is not None
    tokens = cli._subgraph_member_tokens(str(repo), graph, anchor)
    assert tokens is not None
    return tokens


def _run(argv, capsys):
    """Invoke the CLI, returning (exit_code, parsed_stdout_json_or_raw, stderr)."""
    code = main(argv)
    cap = capsys.readouterr()
    try:
        parsed = json.loads(cap.out)
    except json.JSONDecodeError:
        parsed = cap.out
    return code, parsed, cap.err


# ── the change half: the fp MOVES when the neighborhood changes ──────────────────


def test_subgraph_fp_changes_when_a_callee_signature_changes(graph_repo):
    # The §1/F3 case: mid_fn CALLS helper — a CALLEE signature change must move the
    # anchor's fp. Reverse-only reachability provably misses it; the forward cone catches it.
    before = _fp(graph_repo)
    _edit(graph_repo, "pkg/svc.py", "def helper(x):", "def helper(x, y=0):")
    assert _fp(graph_repo) != before


def test_subgraph_fp_changes_when_a_caller_changes(graph_repo):
    # Reverse reach retained under the union: entry CALLS mid_fn, so entry's signature
    # change moves mid_fn's fp.
    before = _fp(graph_repo)
    _edit(graph_repo, "main.py", "def entry():", "def entry(n=0):")
    assert _fp(graph_repo) != before


def test_subgraph_fp_changes_on_inplace_member_signature_change_with_constant_membership(
    graph_repo,
):
    # The sharpened T4/T6: no member added or removed — the SAME member set, one member's
    # signature changed in place. Only a live label→combdrift bridge (F2) can see it: with
    # the bridge dead every member degrades to `identity` and this change is invisible.
    anchor = "pkg/svc.py:Service.run"

    def names(tokens):
        return sorted(":".join(t.split(":")[:2]) for t in tokens)

    tokens_before = _tokens(graph_repo, anchor)
    before = _fp(graph_repo, anchor)
    _edit(graph_repo, "pkg/svc.py", "def run(self, a, b):", "def run(self, a, b, c=0):")
    tokens_after = _tokens(graph_repo, anchor)
    assert names(tokens_after) == names(tokens_before)  # membership did NOT churn
    assert _fp(graph_repo, anchor) != before  # yet the fp moved (the shape half)


def test_subgraph_fp_detects_same_shape_rename_inside_radius(graph_repo):
    # F5's identity half: renaming helper -> helper2 (same call shape, call sites updated)
    # keeps the shape MULTISET identical — only a name-carrying member token can move.
    def shapes(tokens):
        return sorted(t.split(":", 2)[2] for t in tokens)

    tokens_before = _tokens(graph_repo)
    before = _fp(graph_repo)
    svc = graph_repo / "pkg" / "svc.py"
    svc.write_text(
        svc.read_text(encoding="utf-8").replace("helper", "helper2"), encoding="utf-8"
    )
    tokens_after = _tokens(graph_repo)
    assert shapes(tokens_after) == shapes(
        tokens_before
    )  # an anonymous multiset sees nothing
    assert _fp(graph_repo) != before


# ── the stability half: the fp HOLDS through unrelated change ────────────────────


def test_subgraph_fp_unchanged_when_an_unrelated_sibling_changes(graph_repo):
    # `lonely` shares mid_fn's file but not its dependency neighborhood: its signature
    # change must NOT move the fp (unrelated edits move the global stamp, never this token).
    before = _fp(graph_repo)
    _edit(graph_repo, "pkg/svc.py", "def lonely(a):", "def lonely(a, b=1):")
    assert _fp(graph_repo) == before


def test_subgraph_fp_unchanged_when_an_unrelated_file_adds_a_same_named_symbol(
    graph_repo,
):
    # F4/T7: a same-named `mid_fn` appearing in an UNRELATED file must neither move the fp
    # nor make the anchor ambiguous — seed resolution is scoped to the anchor's own file.
    before = _fp(graph_repo)
    (graph_repo / "other").mkdir()
    (graph_repo / "other" / "x.py").write_text(
        "def mid_fn(q):\n    return q\n", encoding="utf-8"
    )
    assert _fp(graph_repo) == before


def test_subgraph_fp_stable_across_update_and_scratch_builds(
    graph_repo, tmp_path, monkeypatch
):
    # The fp-level BUG-025 regression guard: after an edit, the fp minted from the
    # INCREMENTALLY updated graph equals the fp minted from a from-scratch build.
    import hive.matrix as matrix

    before = _fp_map(graph_repo)  # first build lands the persistent cache
    _edit(graph_repo, "pkg/svc.py", "def helper(x):", "def helper(x, y):")
    real_build = matrix.build_graph
    with monkeypatch.context() as patch:  # prove the update took the merge path
        patch.setattr(
            matrix,
            "build_graph",
            lambda *a, **k: pytest.fail("update fell back to a scratch build"),
        )
        g_updated = cli._load_graph(str(graph_repo))
    assert g_updated is not None
    fp_updated = cli._subgraph_fp_core(str(graph_repo), _ANCHOR, graph=g_updated)
    g_scratch = real_build(Path(graph_repo), out_dir=tmp_path / "scratch-out")
    fp_scratch = cli._subgraph_fp_core(str(graph_repo), _ANCHOR, graph=g_scratch)
    assert fp_updated == fp_scratch
    assert fp_updated != before  # and the callee edit registered at all


def test_subgraph_fp_is_portable_across_checkout_paths(tmp_path):
    # BUG-021/T8: the identical tree at two absolute paths mints the identical token — for the
    # Python anchor and a SQL schema anchor alike. Portability is by construction: a member
    # token is built from the member's label + its REPO-ROOT-relative source_file, NEVER the
    # node_id (matrix node ids can encode the absolute checkout path), so no checkout path may
    # ride into a token or fork the fp across checkouts.
    checkout_a = _write_tree(tmp_path / "checkout-a", with_sql=True)
    checkout_b = _write_tree(tmp_path / "checkout-b", with_sql=True)
    assert _fp(checkout_a) == _fp(checkout_b)
    sql_anchor = "db/schema.sql:orders"
    assert _fp(checkout_a, sql_anchor) == _fp(checkout_b, sql_anchor)
    for token in _tokens(checkout_a, sql_anchor):
        assert str(checkout_a) not in token
        assert "checkout-a" not in token and "checkout_a" not in token


def test_subgraph_fp_is_line_ending_invariant(tmp_path):
    # Cross-OS device-invariance: the same tree checked out CRLF (Windows git autocrlf) vs LF
    # must mint the identical matrix/subgraph_fp, so a mixed-OS fleet agrees on the stored token.
    lf_checkout = _write_tree(tmp_path / "lf-checkout")
    crlf_checkout = _write_tree(tmp_path / "crlf-checkout")
    for path in crlf_checkout.rglob("*.py"):
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert _fp(lf_checkout) == _fp(crlf_checkout)


# ── gates and fallbacks ──────────────────────────────────────────────────────────


def test_subgraph_fp_prose_anchor_returns_empty(graph_repo, monkeypatch):
    # BUG-022: a prose anchor is NOT code — `{}` without ever paying a graph build.
    loads = []
    monkeypatch.setattr(cli, "_load_graph", lambda repo: loads.append(repo))
    assert cli._subgraph_fp_core(str(graph_repo), "the auth refresh flow") == {}
    assert cli._subgraph_fp_core(str(graph_repo), "auth: refresh the token") == {}
    assert cli._subgraph_fp_core(None, _ANCHOR) == {}  # no repo is equally fail-open
    assert loads == []


def test_subgraph_fp_falls_back_to_identity_on_uncovered_language_member(tmp_path):
    # F7: combdrift carries no Layer-B interface fingerprint for SQL (it resolves SQL only for
    # table+column EXISTENCE), so every SQL schema member takes the `identity` shape — yet the
    # composite still mints AND a membership add still moves the fp through the name half.
    repo = _write_tree(tmp_path / "sqlrepo", with_sql=True)
    sql_anchor = "db/schema.sql:orders"
    tokens = _tokens(repo, sql_anchor)
    assert "db/schema.sql:orders:identity" in tokens
    assert "db/schema.sql:users:identity" in tokens  # the forward-cone FK reference
    before = _fp(repo, sql_anchor)
    # A new table joins orders' neighborhood via a fresh FK reference: membership churns, so
    # the composite fp moves even though no SQL member carries a resolvable call shape.
    _edit(
        repo,
        "db/schema.sql",
        "    user_id INTEGER REFERENCES users\n",
        "    user_id INTEGER REFERENCES users,\n    coupon_id INTEGER REFERENCES coupons\n",
    )
    schema = repo / "db" / "schema.sql"
    schema.write_text(
        schema.read_text(encoding="utf-8")
        + "\n\nCREATE TABLE coupons (\n    id INTEGER PRIMARY KEY,\n    code TEXT\n);\n",
        encoding="utf-8",
    )
    assert _fp(repo, sql_anchor) != before


def test_subgraph_fp_mints_for_a_dotted_method_anchor(graph_repo):
    # F4/E6: the engine's own resolver cannot see dotted symbols; ours must — via the
    # `.run()` label variant plus the incoming `method` edge from the `Service` class node.
    single = _fp_map(graph_repo, "pkg/svc.py:Service.run")
    double = _fp_map(graph_repo, "pkg/svc.py::Service.run")
    assert set(single) == {"matrix/subgraph_fp"}
    assert double == single


def test_member_token_carries_class_qualified_symbol_and_real_shape(graph_repo):
    # F2/E7: a method member's token embeds the class-qualified symbol AND a real combdrift
    # shape — a dead bridge would leave `.run()` unresolvable and degrade it to identity.
    graph = cli._load_graph(str(graph_repo))
    assert graph is not None
    run_nodes = [
        node
        for node in graph.nodes()
        if node.source_file == "pkg/svc.py" and node.label == ".run()"
    ]
    assert len(run_nodes) == 1
    token = cli._member_token(str(graph_repo), graph, run_nodes[0].id)
    assert token is not None
    assert token.startswith("pkg/svc.py:Service.run:combdrift-fp/")
    assert "identity" not in token


def test_combine_tokens_is_order_independent_and_version_prefixed():
    ordered = cli._combine_tokens(["m1:s1:t1", "m2:s2:t2", "m3:s3:t3"])
    shuffled = cli._combine_tokens(["m3:s3:t3", "m1:s1:t1", "m2:s2:t2"])
    # // marker: dropping the sorted() in _combine_tokens (hashing arrival order) is a
    # mutation — this equality reds.
    assert ordered == shuffled
    assert ordered.startswith("matrix-subgraph-fp/1:")
    assert ordered != cli._combine_tokens(["m1:s1:t1", "m2:s2:t2"])  # content-sensitive


# ── the mint union (the capture seam prints ONE map) ─────────────────────────────


def test_mint_verb_prints_union_of_both_fp_keys(graph_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(graph_repo), "--anchor", _ANCHOR], capsys
    )
    assert code == 0
    assert set(out) == {"combdrift/fp", "matrix/subgraph_fp"}
    assert out["combdrift/fp"].startswith("combdrift-fp/")
    assert out["matrix/subgraph_fp"].startswith("matrix-subgraph-fp/1:")


def test_mint_union_prose_anchor_prints_empty(graph_repo, capsys):
    code, out, _ = _run(
        ["mint", "--repo", str(graph_repo), "--anchor", "the auth refresh flow"], capsys
    )
    assert code == 0 and out == {}


# ── the single-source-root layout (the path-dialect bridge) ──────────────────────
# When ALL parsed source lives under ONE top-level dir, matrix roots `source_file`
# at that subdirectory ("src/two.py" -> "two.py") while anchors stay repo-root-
# relative. The bridge owns the translation in BOTH directions; a natural anchor
# must behave exactly as it does on a multi-root tree.

_SROOT_ONE_PY = """\
from two import beta


def alpha():
    return beta(1)
"""

_SROOT_TWO_PY = """\
def beta(v):
    return v * 2
"""


def _write_single_root_tree(root: Path) -> Path:
    """ALL parsed source under src/ (no second top-level code entry), so matrix infers
    src as the extraction root and flattens `source_file`. src/one.py:alpha calls
    src/two.py:beta; the pads keep a one-file edit on the incremental merge path."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "one.py").write_text(_SROOT_ONE_PY, encoding="utf-8")
    (root / "src" / "two.py").write_text(_SROOT_TWO_PY, encoding="utf-8")
    for name in ("pad_a", "pad_b", "pad_c"):
        (root / "src" / f"{name}.py").write_text(
            f"def {name}():\n    return 0\n", encoding="utf-8"
        )
    return root


@pytest.fixture
def single_root_repo(tmp_path):
    return _write_single_root_tree(tmp_path / "srepo")


def test_subgraph_fp_mints_for_a_natural_anchor_on_single_root_repo(
    single_root_repo, capsys
):
    # The repo-root-relative anchor (the dialect combdrift and every served example use)
    # must mint here too — the graph's flattened dialect is the bridge's problem, never
    # the agent's.
    token = _fp(single_root_repo, "src/two.py:beta")
    assert token.startswith("matrix-subgraph-fp/1:")
    # And the capture seam yields BOTH keys for it — one union map.
    code, out, _ = _run(
        ["mint", "--repo", str(single_root_repo), "--anchor", "src/two.py:beta"], capsys
    )
    assert code == 0
    assert set(out) == {"combdrift/fp", "matrix/subgraph_fp"}


def test_subgraph_fp_changes_on_callee_signature_change_on_single_root_repo(
    single_root_repo,
):
    # The §1 callee case on the layout that used to fail open to {}: alpha CALLS beta,
    # so beta's signature change must move alpha's fp.
    anchor = "src/one.py:alpha"
    before = _fp(single_root_repo, anchor)
    _edit(single_root_repo, "src/two.py", "def beta(v):", "def beta(v, w=0):")
    assert _fp(single_root_repo, anchor) != before


def test_member_token_carries_real_shape_on_single_root_repo(single_root_repo):
    # No blanket identity degradation: member paths are repo-root-relative — so
    # combdrift resolves them — and every callable member carries a REAL shape.
    tokens = _tokens(single_root_repo, "src/one.py:alpha")
    assert all(token.startswith("src/") for token in tokens), tokens
    shapes = {":".join(t.split(":")[:2]): t.split(":", 2)[2] for t in tokens}
    assert shapes["src/one.py:alpha"].startswith("combdrift-fp/")
    assert shapes["src/two.py:beta"].startswith("combdrift-fp/")


def test_subgraph_fp_portable_across_checkout_paths_on_single_root_repo(tmp_path):
    # BUG-021 on this layout: the identical tree at two absolute paths mints the
    # identical token, and no member token embeds a checkout path.
    checkout_a = _write_single_root_tree(tmp_path / "checkout-a")
    checkout_b = _write_single_root_tree(tmp_path / "checkout-b")
    anchor = "src/two.py:beta"
    assert _fp(checkout_a, anchor) == _fp(checkout_b, anchor)
    for token in _tokens(checkout_a, anchor):
        assert "checkout_a" not in token and str(checkout_a) not in token


def test_verify_radius_works_for_natural_anchor_on_single_root_repo(
    single_root_repo, capsys
):
    # The recall seam end-to-end on the single-root layout: an unchanged tree is
    # current/current — NEVER a false stale — and a neighborhood edit flips ONLY the
    # advisory radius.
    anchor = "src/two.py:beta"
    token = _fp(single_root_repo, anchor)
    # Both tiers store a real token: beta's own call shape (so `current` is a COMPARED
    # verdict) beside its neighborhood fp. The edit below moves only alpha — beta's shape
    # is untouched — so the anchor tier holds while the radius tier flips.
    fp = _anchor_fp(single_root_repo, anchor)
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(single_root_repo),
            "--anchor",
            anchor,
            "--fp",
            fp,
            "--subgraph-fp",
            token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert out["radius"] == "current"
    _edit(single_root_repo, "src/one.py", "def alpha():", "def alpha(n=0):")
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(single_root_repo),
            "--anchor",
            anchor,
            "--fp",
            fp,
            "--subgraph-fp",
            token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"  # the anchor itself is untouched
    assert out["radius"] == "changed"  # its dependency neighborhood moved


# ── the verify radius tier (a separate ADVISORY field — never the verdict) ───────


def test_verify_radius_current_when_neighborhood_unchanged(graph_repo, capsys):
    # The stored `--fp` is mid_fn's real call shape, so `current` is a verdict the anchor
    # tier actually COMPUTED rather than the absence of anything to compare.
    token = _fp(graph_repo)
    fp = _anchor_fp(graph_repo)
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert out["radius"] == "current"
    assert set(out) == {
        "verdict",
        "reason",
        "radius",
    }  # radius adds ONE key, nothing else


def test_verify_radius_changed_when_dependency_changes_and_anchor_still_current(
    graph_repo, capsys
):
    # The headline: helper (a CALLEE of mid_fn) changes while mid_fn itself is untouched —
    # the anchor verdict stays `current` AND the radius reports the moved neighborhood. The
    # two are separate channels: a changed neighborhood does not mean the memory is wrong,
    # so no remediation (and no notice text) rides the verify output.
    token = _fp(graph_repo)
    # mid_fn's own shape is stored too, so the anchor tier COMPARES it against the post-edit
    # tree and finds it unmoved — the edit lands on helper, never on mid_fn's signature.
    fp = _anchor_fp(graph_repo)
    _edit(graph_repo, "pkg/svc.py", "def helper(x):", "def helper(x, y=0):")
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            token,
        ],
        capsys,
    )
    assert code == 0
    # // marker: promoting radius-changed into the anchor verdict (stale) reds this line.
    assert out["verdict"] == "current"
    assert out["radius"] == "changed"
    assert set(out) == {"verdict", "reason", "radius"}  # advisory only — no remediation


def test_verify_radius_key_omitted_without_stored_token(
    graph_repo, capsys, monkeypatch
):
    # An old memory carries no subgraph token: the output shape must be byte-identical to
    # the pre-radius contract — no radius key, and NO graph work paid at all. It still
    # carries the anchor's own fingerprint, so `current` is the compared verdict.
    fp = _anchor_fp(graph_repo)
    monkeypatch.setattr(
        cli,
        "_load_graph",
        lambda repo: pytest.fail("graph loaded with no stored token"),
    )
    code, out, _ = _run(
        ["verify", "--repo", str(graph_repo), "--anchor", _ANCHOR, "--fp", fp], capsys
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert set(out) == {"verdict", "reason"}


def test_verify_radius_key_omitted_when_recompute_fails(
    graph_repo, capsys, monkeypatch
):
    stored = f"matrix-subgraph-fp/1:{'0' * 64}"
    fp = _anchor_fp(graph_repo)  # the anchor tier's answer must be a COMPARED `current`
    # A dead graph engine yields {} from the fp core: the key is OMITTED — never a bogus
    # "changed" — and the anchor tier still answers (fail-open, bias toward keep).
    monkeypatch.setattr(cli, "_load_graph", lambda repo: None)
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            stored,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert "radius" not in out
    # An unverifiable (prose) anchor is equally byte-inert: the prose gate returns {}.
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            "the auth refresh flow",
            "--subgraph-fp",
            stored,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "unverifiable"
    assert "radius" not in out


def test_verify_radius_omitted_when_reader_is_ahead_of_stored_token(
    graph_repo, capsys, monkeypatch
):
    # BUG-037 (the meta envelope law): a stored v1 token under a bumped reader is
    # INCOMPARABLE — the radius key is OMITTED (silence), NEVER a false `changed` on
    # every old memory — and the expensive graph recompute is not even attempted.
    token = _fp(graph_repo)  # a real v1 token, minted pre-bump
    fp = _anchor_fp(graph_repo)  # the anchor tier's own shape — unaffected by the bump
    monkeypatch.setattr(cli, "SUBGRAPH_FP_VERSION", "2")
    monkeypatch.setattr(
        cli,
        "_load_graph",
        lambda repo: pytest.fail("graph loaded for an incomparable token"),
    )
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert "radius" not in out
    assert set(out) == {"verdict", "reason"}


def test_verify_radius_omitted_on_future_version_token(graph_repo, capsys):
    # The mirror direction: a FUTURE token (minted by a newer edge) under the real
    # reader is equally incomparable — omit, never compare across versions.
    stored = "matrix-subgraph-fp/999:deadbeef"
    fp = _anchor_fp(graph_repo)  # the anchor tier answers on its own compared token
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            stored,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert "radius" not in out


def test_verify_radius_omitted_on_malformed_subgraph_token(graph_repo, capsys):
    # A malformed stored token (no recognized prefix / no version) has no envelope to
    # read: omit the radius; the anchor verdict/reason are untouched — and they are a real
    # comparison against the anchor's own stored shape.
    fp = _anchor_fp(graph_repo)
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            fp,
            "--subgraph-fp",
            "garbage",
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "current"
    assert set(out) == {"verdict", "reason"}


def test_verify_stale_verdict_untouched_by_radius_tier(graph_repo, capsys):
    # The anchor tier's stale machinery (verdict, reason, remediation, delta) is NEVER
    # touched by the radius tier — in EITHER direction.
    old_fp = combdrift.fingerprint_anchor(
        str(graph_repo), combdrift.Anchor("pkg/svc.py", "mid_fn")
    )
    old_token = _fp(graph_repo)
    _edit(
        graph_repo, "pkg/svc.py", "def mid_fn(v):", "def mid_fn(v, w):"
    )  # breaking drift
    fresh_token = _fp(graph_repo)
    assert fresh_token != old_token

    # Radius CURRENT beside a stale anchor: a matching neighborhood never downgrades stale.
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            old_fp,
            "--subgraph-fp",
            fresh_token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "stale" and out["reason"] == "signature_changed"
    assert out["radius"] == "current"
    assert "remediation" in out

    # Radius CHANGED beside a stale anchor: the stale payload stays fully intact.
    code, out, _ = _run(
        [
            "verify",
            "--repo",
            str(graph_repo),
            "--anchor",
            _ANCHOR,
            "--fp",
            old_fp,
            "--subgraph-fp",
            old_token,
        ],
        capsys,
    )
    assert code == 0
    assert out["verdict"] == "stale" and out["reason"] == "signature_changed"
    assert out["radius"] == "changed"
    assert "remediation" in out and out["delta"]["breaking"] is True


def test_radius_notice_is_advisory_wording_only():
    # The single-owner constant the recall relay consumes: re-verify guidance ONLY — no
    # retirement vocabulary (that is REMEDIATION_TEXT's job, and radius is not a verdict).
    notice = cli.RADIUS_NOTICE
    assert "dependency neighborhood has changed" in notice
    assert "re-verify" in notice
    assert "may still be correct" in notice
    for verb in ("hive_prune", "hive_supersede", "hive_outcome", "retire"):
        assert verb not in notice, verb
