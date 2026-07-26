"""BUG-077 — the anchor grammar has ONE server-side owner, and the write boundary
accepts only the form that can structurally join the census subject feed.

Drives the REAL surfaces end to end: ``HiveMCPServer.handle`` for both write verbs
over the real store, and a REAL sync tick (real ``python -m hive.census.cli build``
subprocess + real git origin) for the join half. Nothing is seeded — every
``change_outcome`` row asserted here is written by production code.

Intents covered:
  I1 — a dead anchor spelling is a loud refusal naming the clause, nothing stored;
       the canonical spellings (and colon-free prose) still admit unchanged.
  I2 — the accepted grammar tokenizes IDENTICALLY on both sides (the server-side
       splitter and the engine's ``_split_anchor``) and an accepted anchor reaches
       the outcome feed the memory needs to ever promote.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from hive.app.config import SyncConfig
from hive.app.mcp_server import MCPRequest, ServerIdentity
from hive.app.sync import SyncService
from hive.domain.change_evidence import ChangeEvidenceService
from tests.contract.conftest import load_module
from tests.mcp._helpers import build_real_server
from tests.sync.conftest import Origin

ANCHOR = "app.py::greet"
TEXT = "greet() must stay single-arg; callers pass positionally"


# ── the v3-only module guard (red = a clean assertion, never a collection error) ─


def grammar():
    """``hive.domain.anchor_grammar`` — the ONE server-side owner (plan §3.1(d))."""
    mod = load_module("hive.domain.anchor_grammar")
    assert mod is not None, (
        "hive/domain/anchor_grammar.py does not exist yet — the single owner the "
        "write gate, the census join and the sync backfill must all consume"
    )
    return mod


# ── rig ───────────────────────────────────────────────────────────────────────


def call(server, name, args, *, agent="agent-a"):
    return server.handle(
        MCPRequest(1, "tools/call", {"name": name, "arguments": args}),
        identity=ServerIdentity("t", agent),
    )


def envelope(resp) -> dict:
    """The tool-result payload as a dict. TOTAL over today's shapes so a wrong
    expectation fails on an ASSERTION rather than a decode error: a JSON-RPC error
    becomes ``{"_error": ...}`` and the schema belt's bare-string tool error
    becomes ``{"_raw": ...}``."""
    if resp.error is not None:
        return {"_error": resp.error}
    text = (resp.result or {}).get("content", [{}])[0].get("text", "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def make_syncer(store, tmp_path: Path, **cfg_kw):
    cfg = SyncConfig(mirror_dir=str(tmp_path / "mirrors"), **cfg_kw)
    evidence = ChangeEvidenceService(
        reader=store, appender=store, now=lambda: 424_242, ranges=store
    )
    return SyncService(cfg, store, evidence, threading.Lock())


@pytest.fixture
def rig(tmp_path):
    origin = Origin(tmp_path / "remote")
    server, clock = build_real_server(t0=1_000_000)
    server.store.repo_add(
        name="alpha", url=origin.url, canonical_ref="main", token_env="", added_ts=0
    )
    return origin, server, clock, make_syncer(server.store, tmp_path)


def n_episodes(server) -> int:
    return int(
        server.store.conn.execute("SELECT COUNT(*) AS n FROM episodes").fetchone()["n"]
    )


def n_anchor_rows(server) -> int:
    return int(
        server.store.conn.execute(
            "SELECT COUNT(*) AS n FROM episode_anchors"
        ).fetchone()["n"]
    )


# Every spelling the boundary must now REFUSE, with the clause fragment its
# message has to name. Each is dead in the census join, unresolvable in the drift
# engine, or a redundant/ambiguous form of a spelling that already works.
DEAD_SPELLINGS = [
    ("app.py:greet", "'::'"),  # the BUG-077 shape: joins nothing, resolves fine
    ("app.py:42", "'::'"),  # its line-number twin: joins nothing either
    ("a:b/c.py", "'::'"),  # colon-bearing path, no '::': lexically ambiguous
    ("app.py::", "no symbol"),  # a separator naming no symbol
    ("app.py::42", "line number"),  # a bare line number is not a symbol
    ("::greet", "empty path"),  # no path component at all
]

# Everything that must keep admitting — the canonical grammar plus the prose tail.
LIVE_SPELLINGS = [
    "app.py",  # file-scoped
    "app.py::greet",  # symbol-scoped
    "hive/app/sync.py::SyncService.tick",  # dotted method
    "src/lib.rs::Ns::Cls.method",  # nested '::' INSIDE the symbol (C++/Rust)
    "a:b/c.py::S",  # a colon-bearing path made unambiguous by '::'
    "app.py::f2",  # digits in a symbol that is not ONLY digits
    "some anchor // with spaces, ünïcode and no path shape",  # colon-free prose
]


# ── I1: the refusal ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("verb", ["hive_write", "hive_capture"])
@pytest.mark.parametrize("spelling, clause", DEAD_SPELLINGS)
def test_dead_anchor_spellings_are_refused_and_store_nothing(
    rig, verb, spelling, clause
):
    _origin, server, _clock, _syncer = rig
    before_eps, before_anchors = n_episodes(server), n_anchor_rows(server)

    resp = call(
        server,
        verb,
        {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": spelling}]},
    )
    env = envelope(resp)

    assert env.get("status") == "refused", (
        f"{verb} accepted the dead spelling {spelling!r}: {env}"
    )
    reason = str(env.get("reason", ""))
    assert "anchors[0].anchor" in reason, f"the refusal must name the entry: {reason}"
    assert clause in reason, (
        f"the refusal must name the violated clause ({clause!r}): {reason}"
    )
    assert not resp.result.get("isError"), "a grammar refusal is a clean envelope"
    assert (n_episodes(server), n_anchor_rows(server)) == (
        before_eps,
        before_anchors,
    ), "a refused write must store NOTHING"


def test_a_dead_spelling_refuses_the_whole_call(rig):
    """All-or-nothing: one bad binding refuses every binding in the array."""
    _origin, server, _clock, _syncer = rig
    before_eps, before_anchors = n_episodes(server), n_anchor_rows(server)

    env = envelope(
        call(
            server,
            "hive_write",
            {
                "text": TEXT,
                "anchors": [
                    {"repo": "alpha", "anchor": ANCHOR},  # valid
                    {"repo": "alpha", "anchor": "app.py:greet"},  # dead
                ],
            },
        )
    )
    assert env.get("status") == "refused", env
    assert "anchors[1].anchor" in str(env.get("reason", "")), env
    assert (n_episodes(server), n_anchor_rows(server)) == (before_eps, before_anchors)


# ── I1: the admission side (the gate must not over-refuse) ────────────────────


@pytest.mark.parametrize("spelling", LIVE_SPELLINGS)
def test_canonical_and_prose_anchors_still_admit(rig, spelling):
    _origin, server, _clock, _syncer = rig
    env = envelope(
        call(
            server,
            "hive_write",
            {
                "text": f"{TEXT} :: {spelling}",
                "anchors": [{"repo": "alpha", "anchor": spelling}],
            },
        )
    )
    assert env.get("status") in ("approved", "redacted"), (
        f"the gate over-refused a working spelling {spelling!r}: {env}"
    )
    stored = [
        r["anchor"]
        for r in server.store.conn.execute(
            "SELECT anchor FROM episode_anchors WHERE episode_id=?", (env["id"],)
        )
    ]
    assert stored == [spelling], (
        "an accepted anchor is stored VERBATIM, never rewritten"
    )


def test_the_entry_cap_boundary_is_unchanged(rig):
    """The cap clause fires on count, not on grammar — a full array of canonical
    anchors still lands, one more still refuses on the cap."""
    from hive.app.anchors import SCOPE_MAX_ENTRIES

    _origin, server, _clock, _syncer = rig
    full = [
        {"repo": "alpha", "anchor": f"pkg/f{i}.py::fn"}
        for i in range(SCOPE_MAX_ENTRIES)
    ]
    env = envelope(
        call(server, "hive_write", {"text": TEXT + " full", "anchors": full})
    )
    assert env.get("status") in ("approved", "redacted"), env

    over = full + [{"repo": "alpha", "anchor": "pkg/extra.py::fn"}]
    env = envelope(
        call(server, "hive_write", {"text": TEXT + " over", "anchors": over})
    )
    assert env.get("status") == "refused" and "max" in str(env["reason"]), env


# ── I1: the pre-existing refusal clauses are untouched ───────────────────────


@pytest.mark.parametrize(
    "anchors, clause",
    [
        ([{"repo": "alpha", "anchor": ""}], "non-empty code location"),
        ([{"repo": "ghost", "anchor": "app.py::greet"}], "unknown repo"),
        (
            [
                {"repo": "alpha", "anchor": "app.py::greet"},
                {"repo": "alpha", "anchor": "app.py::greet"},
            ],
            "duplicate anchor binding",
        ),
    ],
)
def test_pre_existing_gate_refusals_are_unchanged(rig, anchors, clause):
    """The clauses that already lived in ``normalize_anchors`` keep their exact
    wording — the grammar clause is an ADDITION, never a re-spelling."""
    _origin, server, _clock, _syncer = rig
    before = n_episodes(server)
    env = envelope(call(server, "hive_write", {"text": TEXT, "anchors": anchors}))
    assert env.get("status") == "refused", env
    assert clause in str(env["reason"]), env
    assert n_episodes(server) == before


@pytest.mark.parametrize(
    "anchors",
    [
        "app.py::greet",  # bare string, not an array
        42,
        {"repo": "alpha"},
        [["alpha", "app.py::greet"]],  # non-object entry
        [{"repo": "alpha", "anchor": 3}],  # non-string anchor
        [{"repo": "alpha", "anchor": None}],
        [{"repo": None, "anchor": "app.py::greet"}],
        [{"repo": "alpha", "anchor": "app.py::greet", "evil": 1}],  # unknown key
        [None],
        [True],
    ],
)
def test_pre_existing_schema_belt_refusals_are_unchanged(rig, anchors):
    """A hostile SHAPE is still caught by the pre-dispatch schema belt, before the
    grammar clause is ever consulted: a clean ``isError`` tool result, never a
    JSON-RPC error, never a crash, and nothing stored."""
    _origin, server, _clock, _syncer = rig
    before_eps, before_anchors = n_episodes(server), n_anchor_rows(server)
    resp = call(server, "hive_write", {"text": TEXT, "anchors": anchors})
    assert resp.error is None, f"the belt must not raise a JSON-RPC error: {resp.error}"
    assert resp.result.get("isError") is True, envelope(resp)
    assert "invalid arguments" in envelope(resp).get("_raw", ""), envelope(resp)
    assert (n_episodes(server), n_anchor_rows(server)) == (before_eps, before_anchors)


# ── I2: ONE grammar, both sides ──────────────────────────────────────────────


def test_the_canonical_grammar_tokenizes_identically_on_both_sides(rig):
    """CT-A5. Every spelling the boundary ADMITS must tokenize the same way in the
    server-side splitter and in the engine's reader-side ``_split_anchor`` (`""`
    and ``None`` are the same file-scoped meaning). The single-colon negative pin
    rides here too: it still joins nothing, and the engine still resolves it —
    that asymmetry is the reader-side leniency §3.1(c) deliberately KEEPS."""
    from hive.domain.change_evidence import TouchedSubject, _anchor_match_level
    from hive.edge.cli import _split_anchor

    split_anchor = grammar().split_anchor
    anchor_grammar_error = grammar().anchor_grammar_error

    for spelling in (
        "app.py::greet",
        "a.py::C.m",
        "a.py::Ns::C.m",
        "a:b/c.py::S",
        "a.py",
    ):
        assert anchor_grammar_error(spelling) is None, spelling
        path, symbol = split_anchor(spelling)
        edge_path, edge_symbol = _split_anchor(spelling)
        assert path == edge_path, f"{spelling}: {path!r} != {edge_path!r}"
        assert (symbol or None) == edge_symbol, (
            f"{spelling}: {symbol!r} != {edge_symbol!r}"
        )

    # the accepted symbol grammar joins at the SYMBOL tier
    assert (
        _anchor_match_level(
            ["app.py::greet"], TouchedSubject(path="app.py", symbol="greet")
        )
        == "symbol"
    )
    # ... and the accepted file grammar at the FILE tier
    assert (
        _anchor_match_level(["app.py"], TouchedSubject(path="app.py", symbol="greet"))
        == "file"
    )
    # the negative pin (BUG-058's anti-fork): the single-colon form joins NOTHING
    assert (
        _anchor_match_level(
            ["app.py:greet"], TouchedSubject(path="app.py", symbol="greet")
        )
        is None
    )
    # ... while the engine still resolves it — reader-side leniency, kept on purpose
    assert _split_anchor("app.py:greet") == ("app.py", "greet")
    # ... and the boundary is what stops a NEW one being written
    assert anchor_grammar_error("app.py:greet") is not None


def test_the_bare_line_number_fact_agrees_across_both_owners(rig):
    """``_LINE_NUMBER`` (engine, reader side) and the boundary clause (mint side)
    state the SAME fact — a bare number is not a symbol. Re-homed, not forked."""
    from hive.edge.cli import _split_anchor

    anchor_grammar_error = grammar().anchor_grammar_error
    for digits in ("42", "0", "007", "١٢"):
        assert _split_anchor(f"a.py::{digits}") == ("a.py", None), digits
        assert anchor_grammar_error(f"a.py::{digits}") is not None, digits
    for symbol in ("f2", "v2beta", "_1"):
        assert _split_anchor(f"a.py::{symbol}") == ("a.py", symbol), symbol
        assert anchor_grammar_error(f"a.py::{symbol}") is None, symbol


def test_an_accepted_anchor_reaches_the_change_outcome_feed(rig):
    """The end-to-end proof that the ACCEPTED grammar is the JOINABLE one: a real
    push touching the anchored symbol, a real census receipt, and a
    ``change_outcome`` row lands for that episode — the row whose ABSENCE is
    BUG-077's whole symptom (no outcome ⇒ no verified win ⇒ never established)."""
    origin, server, _clock, syncer = rig

    symbol_eid = envelope(
        call(
            server,
            "hive_write",
            {"text": TEXT, "anchors": [{"repo": "alpha", "anchor": ANCHOR}]},
        )
    )["id"]
    file_eid = envelope(
        call(
            server,
            "hive_write",
            {
                "text": TEXT + " (file scope)",
                "anchors": [{"repo": "alpha", "anchor": "app.py"}],
            },
        )
    )["id"]
    syncer.tick()  # clone + baseline; no historical receipt on first connect

    origin.commit(
        "app.py",
        'def greet(name, punct):\n    return "hi " + name + punct\n',
        "widen greet",
    )
    origin.push()
    syncer.tick()  # the ledger leg censuses the range and joins it to the anchors

    rows = [
        dict(r)
        for r in server.store.conn.execute(
            "SELECT episode_id, payload FROM evidence_events WHERE kind='change_outcome'"
        )
    ]
    by_eid = {r["episode_id"]: json.loads(r["payload"]) for r in rows}
    assert symbol_eid in by_eid, (
        "a canonically-anchored memory got no change_outcome row — the accepted "
        f"grammar does not join the census feed: {rows}"
    )
    assert by_eid[symbol_eid]["matched"] == {
        "path": "app.py",
        "symbol": "greet",
        "level": "symbol",
    }
    assert file_eid in by_eid, "the file tier must join too"
    assert by_eid[file_eid]["matched"]["level"] == "file"
