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

EVERY ROW HAS LOST ITS MINTER: `combdrift/fp`, `matrix/subgraph_fp` and `git/branches`
lost theirs when the git-native staleness change deleted the drift engine's CLI, and
`hive-sync/minted` lost its with the mint backfill that stamped it. Each says so in its
own `owner` field rather than naming a symbol that no longer exists. The rows STAY —
the key namespace is add-only and readers must keep
ignoring unknown keys, so removing a row would be exactly the rewrite the envelope law
forbids (THEORY §5 clause 1/6). What changes is what can be ENFORCED about them: a key
nothing mints cannot be driven through a mint core, so coverage/agreement/retention for
those three are historical-support facts about STORED tokens rather than live
cross-copy tripwires. They are pinned here as literal expectations, which is the
honest weaker guarantee — a skipped `importorskip` would have looked like a passing
gate while asserting nothing. `combdrift/fp`'s agreement ratchet survives intact,
because `hive.combdrift` is still first-party (the census uses it).
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


def test_the_server_side_minting_door_is_gone():
    """The coverage ratchet's replacement. It used to drive the real mint cores and
    require a registry row for every key they emitted. Those cores are gone with the
    drift engine, and so is the ONE store verb that merged a minted key into an
    episode's anchor carrier (``fill_anchor_fp``) — so there is no server-side door
    left through which an unregistered key could reach the corpus.

    Asserted on the store SURFACE rather than by scanning for token literals: a token
    is built by f-string, which no constant scan can see, but a key cannot be
    PERSISTED without a write verb, and this is the absence of the only one."""
    from hive.adapters.store_sqlite import SqliteEpisodeStore

    for verb in ("fill_anchor_fp", "anchors_lacking_fp", "anchor_carriers"):
        assert not hasattr(SqliteEpisodeStore, verb), (
            f"{verb} is back — an episode-meta key can reach the corpus again, so "
            "the coverage ratchet must be restored with the minting core it drives"
        )


def test_every_registry_row_is_reachable_from_the_keys_projection():
    """No row is a ghost: the projection every consumer reads is exactly the rows."""
    assert set(KEYS) == {spec.key for spec in REGISTRY}
    assert len(KEYS) == len(REGISTRY), "the projection deduplicated a row away"


# ── agreement: row literals == the owning code constants ─────────────────────────


def test_registry_current_versions_match_code():
    """``combdrift/fp``'s owner is still first-party, so this stays a live tripwire."""
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
    assert _BY_KEY["combdrift/fp"].current_version == cd_fp.FINGERPRINT_VERSION


def test_registry_token_prefixes_match_code():
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
    assert _BY_KEY["combdrift/fp"].token_prefix == cd_fp._PREFIX


def test_the_reader_less_rows_are_pinned_as_historical_support():
    """The three rows whose minter AND reader are gone. Their versions and prefixes
    can no longer be derived from any live code, so they are pinned literally: the
    point is that they never CHANGE, because a stored token from that era must keep
    reading the same way forever (and today reads as silence — no reader means no
    annotation, which is the failure direction the envelope law names)."""
    for key, version, prefix in (
        ("matrix/subgraph_fp", "1", "matrix-subgraph-fp/"),
        ("git/branches", "1", "git-branches/"),
    ):
        row = _BY_KEY[key]
        assert row.current_version == version, (
            f"{key}: a key nothing mints cannot get a NEW current version"
        )
        assert row.token_prefix == prefix
        assert version in row.known_versions


# ── retention: every registered known version stays readable ─────────────────────


def test_registry_known_versions_stay_readable():
    cd_fp = pytest.importorskip("hive.combdrift.fingerprint")
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
    # the two reader-less rows: their tokens now route to SILENCE, which is exactly
    # what clause 5 prescribes for an unreadable version — never a false verdict. The
    # ONE reader left that touches them is the opacity carve-out (the version prefix,
    # for the health histogram), so that is what stays pinned readable.
    from hive.domain.meta import token_version

    for key, prefix in (
        ("matrix/subgraph_fp", "matrix-subgraph-fp"),
        ("git/branches", "git-branches"),
    ):
        for version in _BY_KEY[key].known_versions:
            token = f"{prefix}/{version}:{'0' * 64}"
            assert token_version(token) == version, (
                f"{key} v{version} lost even its ENVELOPE parse while still "
                "registered — the aggregate histogram is its last live reader"
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
    # hive-sync backfill provenance (U1), now RETIRED with the leg that stamped it:
    # the ONE catalog still lives in this repo and the row is pinned
    # literal-for-literal, because a stamp already stored must keep reading the same
    # way forever even though nothing writes a new one.
    spec = _BY_KEY["hive-sync/minted"]
    assert spec.token_prefix == "hive-sync-minted/"
    assert spec.current_version == "1"
    assert spec.known_versions == ("1",)
    assert spec.owner == "retired: hive/app/sync.py:_backfill"
    assert spec.compare == "advisory"
    assert spec.tier == "structural"
    assert spec.on_unreadable == "provenance only — never affects any verdict"
