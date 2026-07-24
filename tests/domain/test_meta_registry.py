"""The meta-key registry ratchets (the U2 envelope law's committed teeth).

`hive/domain/meta_registry.py` is the ONE catalog of every episode-meta key. It is a
governance artifact, never a runtime gate — so its enforcement lives HERE: each row is a
deliberate independent copy of facts the owning code states, and these tests red on any
drift between the two copies (the cross-copy-tripwire idiom):

  - coverage: every key the mint cores emit has a registry row (a new key minted without
    its row in the same change reds);
  - agreement: each row's current_version / token_prefix equals the owning code constant
    (a code bump without a registry edit reds, and vice versa);
  - retention: every registered known version stays READABLE by the current reader
    (deleting a historical parser while its version is registered reds — known_versions
    grows, never shrinks);
  - validity: an illegal row is unconstructable (`__post_init__` raises).

The coverage/agreement/retention ratchets drive the real engine cores, so they bind to the
first-party engines (`hive.combdrift`, `hive.edge.cli`) and skip until those are importable;
the validity / keys-projection / server-row ratchets are pure-row checks that always run.
"""

from __future__ import annotations

import pytest

from hive.domain import meta_registry
from hive.domain.meta_registry import KEYS, REGISTRY, MetaKeySpec

_BY_KEY = {spec.key: spec for spec in REGISTRY}


# ── fixtures: both engines resolve these trees (multi-root + a plain py tree) ──────

_MAIN_PY = """\
from pkg.svc import mid_fn


def entry():
    return mid_fn(1)
"""

_SVC_PY = """\
def helper(x):
    return x * 2


def mid_fn(v):
    return helper(v)
"""


# A well-formed v1 combdrift token, FROZEN as a literal (never re-rendered): when the
# fingerprint version bumps, `render` mints the NEW version, but this golden keeps
# probing that v1 stays readable for the corpus already minted under it.
_COMBDRIFT_GOLDENS = {
    "1": "combdrift-fp/1:func(req=1,max=2,star=0,kw=0,kwo=0,gen=0,dec=,base=0)",
}


# ── coverage: every minted key has its row (and no row is a ghost) ────────────────


def test_registry_covers_every_minted_key(tmp_path, monkeypatch, capsys):
    # the mint surfaces live in the first-party edge CLI (absorbed from hive_edge);
    # this ratchet runs once that package is importable.
    cli = pytest.importorskip("hive.edge.cli")
    import json
    import subprocess

    # keep the cli state / error-log off the real ~/.hive-edge — the edge suite's
    # autouse `edge_home`, re-homed locally since this ratchet moved out of tests/edge/.
    monkeypatch.setattr(cli, "CONFIG_DIR", tmp_path / "dot-hive-edge")

    # a plain (non-git) tree with one resolvable callable: pkg/f.py:foo(a, b)
    py_repo = tmp_path / "pyrepo"
    (py_repo / "pkg").mkdir(parents=True)
    (py_repo / "pkg" / "f.py").write_text(
        "def foo(a, b):\n    return a + b\n", encoding="utf-8"
    )

    # a multi-root tree: main.py:entry -> pkg/svc.py:mid_fn -> helper
    graph_repo = tmp_path / "graphrepo"
    (graph_repo / "pkg").mkdir(parents=True)
    (graph_repo / "main.py").write_text(_MAIN_PY, encoding="utf-8")
    (graph_repo / "pkg" / "svc.py").write_text(_SVC_PY, encoding="utf-8")

    minted: set[str] = set()
    minted |= set(cli._mint_core(str(py_repo), "pkg/f.py:foo"))
    minted |= set(cli._subgraph_fp_core(str(py_repo), "pkg/f.py:foo"))
    minted |= set(cli._mint_core(str(graph_repo), "pkg/svc.py:mid_fn"))
    minted |= set(cli._subgraph_fp_core(str(graph_repo), "pkg/svc.py:mid_fn"))
    # the third mint surface: the opt-in --branch-scope tag rides the mint VERB only
    # (never a core, never the hook) — drive the real verb on a real branch.
    repo = tmp_path / "registryrepo"
    (repo / "pkg").mkdir(parents=True)
    (repo / "pkg" / "f.py").write_text(
        "def foo(a, b):\n    return a + b\n", encoding="utf-8"
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "add f"], check=True)
    assert (
        cli.main(
            ["mint", "--repo", str(repo), "--anchor", "pkg/f.py:foo", "--branch-scope"]
        )
        == 0
    )
    minted |= set(json.loads(capsys.readouterr().out))
    # every mint surface resolved: the union covers every EDGE-minted key, no edge key
    # uncatalogued and no edge row a ghost. The one server-minted row (hive-sync/minted,
    # owned under hive/app/) is catalogued here by the envelope law but minted by the
    # hivemind server, so this suite can never observe it minting.
    edge_minted = {
        spec.key for spec in REGISTRY if not spec.owner.startswith("hive/app/")
    }
    assert minted == edge_minted, (
        f"minted keys {sorted(minted)} != edge-owned registry keys {sorted(edge_minted)}"
        f" — a new meta key lands WITH its meta_registry.py row in the same change"
    )
    assert minted <= KEYS


# ── agreement: row literals == the owning code constants ─────────────────────────


def test_registry_current_versions_match_code():
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
    cli = pytest.importorskip("hive.edge.cli")
    assert _BY_KEY["combdrift/fp"].current_version == cd_fp.FINGERPRINT_VERSION
    assert _BY_KEY["matrix/subgraph_fp"].current_version == cli.SUBGRAPH_FP_VERSION
    assert _BY_KEY["git/branches"].current_version == cli.BRANCHES_VERSION


def test_registry_token_prefixes_match_code():
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
    cli = pytest.importorskip("hive.edge.cli")
    assert _BY_KEY["combdrift/fp"].token_prefix == cd_fp._PREFIX
    assert _BY_KEY["matrix/subgraph_fp"].token_prefix == cli._SUBGRAPH_FP_PREFIX
    assert _BY_KEY["git/branches"].token_prefix == cli._BRANCHES_PREFIX


# ── retention: every registered known version stays readable ─────────────────────


def test_registry_known_versions_stay_readable():
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
    cli = pytest.importorskip("hive.edge.cli")
    probe_iface = cd_fp.Interface(
        category="func",
        is_generator=False,
        req_positional=1,
        max_positional=2,
        has_star=False,
        has_kw=False,
        req_kwonly=0,
        contract_decorators=frozenset(),
        base_count=0,
    )
    # structural tier (combdrift/fp): every known version has a frozen golden token the
    # CURRENT reader still fully compares (never "incomparable").
    for version in _BY_KEY["combdrift/fp"].known_versions:
        golden = _COMBDRIFT_GOLDENS.get(version)
        assert golden is not None, (
            f"no golden token frozen for registered combdrift version {version!r} — "
            f"add it beside the version in the same change"
        )
        assert cd_fp.matches(golden, probe_iface) != "incomparable", (
            f"combdrift v{version} tokens became unreadable while still registered"
        )
    # opaque-hash tier (matrix/subgraph_fp): every known version is still RECOGNIZED by
    # the envelope parse (recognize-old-version => silence, never a false `changed`).
    for version in _BY_KEY["matrix/subgraph_fp"].known_versions:
        token = f"matrix-subgraph-fp/{version}:{'0' * 64}"
        assert cli._subgraph_fp_version(token) == version, (
            f"matrix/subgraph_fp v{version} no longer recognized while still registered"
        )
    # structural tier (git/branches): every known version's token still parses to its
    # branch-name set with the CURRENT reader (never "incomparable" while registered).
    for version in _BY_KEY["git/branches"].known_versions:
        token = f"git-branches/{version}:dev main"
        assert cli._branch_scope(token) == frozenset({"dev", "main"}), (
            f"git/branches v{version} tokens became unreadable while still registered"
        )


# ── validity: an illegal row is unconstructable ──────────────────────────────────


def test_registry_rows_reject_illegal_states():
    good = dict(
        key="tool/attr",
        token_prefix="tool-attr/",
        current_version="1",
        known_versions=("1",),
        owner="pkg/mod.py:mint",
        compare="equality",
        tier="opaque-hash",
        on_unreadable="incomparable -> omit",
    )
    MetaKeySpec(**good)  # the legal row constructs
    with pytest.raises(ValueError, match="meta grammar"):
        MetaKeySpec(**{**good, "key": "NoSlash"})
    with pytest.raises(ValueError, match="token_prefix"):
        MetaKeySpec(**{**good, "token_prefix": "tool-attr"})
    with pytest.raises(ValueError, match="known_versions"):
        MetaKeySpec(**{**good, "current_version": "2"})
    with pytest.raises(ValueError, match="compare"):
        MetaKeySpec(**{**good, "compare": "fuzzy"})
    with pytest.raises(ValueError, match="tier"):
        MetaKeySpec(**{**good, "tier": "frozen-algo"})


def test_registry_keys_projection_matches_rows():
    assert KEYS == frozenset(
        {
            "combdrift/fp",
            "matrix/subgraph_fp",
            "git/branches",
            "hive-sync/minted",
        }
    )
    assert len(REGISTRY) == len(KEYS)  # no duplicate rows
    assert meta_registry.KEYS is KEYS


# ── the server-side row: catalogued here, minted by hivemind ──────────────────────


def test_registry_carries_the_hive_sync_minted_row_verbatim():
    # hive-sync backfill provenance (U1): the ONE catalog still lives in this repo,
    # so the server-minted key's row is pinned literal-for-literal — hivemind's
    # sync builder is built AGAINST these values, drift here is contract drift.
    spec = _BY_KEY["hive-sync/minted"]
    assert spec.token_prefix == "hive-sync-minted/"
    assert spec.current_version == "1"
    assert spec.known_versions == ("1",)
    assert spec.owner == "hive/app/sync.py:_backfill"
    assert spec.compare == "advisory"
    assert spec.tier == "structural"
    assert spec.on_unreadable == "provenance only — never affects any verdict"
