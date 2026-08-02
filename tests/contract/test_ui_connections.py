"""CT — the operator console's CONNECTIONS surface: repos + doors.

The console is a projection of what the server is connected to. These contracts pin the
four operator intents end to end — connect a repo, view every connection with the sync
daemon's observed state, disconnect one, and read the door an agent connects through —
against the REAL loopback socket, the REAL ``repoctl``, and a REAL SQLite registry.

Two properties make this a contract suite rather than a router unit test:

- **Arrival evidence on the consumer's side.** A route's effect is asserted as a ROW in a
  real registry file, read back store-side — never as "we emitted an add". The ``run``
  seam replaces Docker, not the tool: every ``python -m hive.tools.repoctl`` the console
  execs is dispatched to ``repoctl.main`` against the same db file.
- **No restated server vocabulary.** The sync field set is derived from
  ``hive.app.sync_keys`` and the served document is compared against
  ``census_health_report`` itself, so a field this file names by hand cannot pass.

Assertions name the OBSERVABLE outcome — served JSON and rendered page text — never an
exit code, a stream, or a byte offset.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from hive.adapters.sqlite_db import connect
from hive.adapters.store_sqlite import SqliteEpisodeStore
from hive.app.census_health import census_health_report
from hive.app.sync_keys import (
    COUNTER_FIELDS,
    FLEET_KEY_BUILDERS,
    FLEET_STR_FIELDS,
    KEY_BUILDERS,
    STR_FIELDS,
)
from hive.tools import cli, repoctl, ui, ui_page

URL = "https://example.invalid/team/octo.git"
OTHER_URL = "https://example.invalid/team/other.git"
SECRET = "gh-token-value-XYZ-that-must-never-ride-the-wire"


def _seq_in(argv, *words) -> bool:
    """True iff `words` appears as a CONTIGUOUS subsequence of argv (order-exact)."""
    need = list(words)
    return any(list(argv[i : i + len(need)]) == need for i in range(len(argv) + 1))


def _completed(argv, rc=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(argv), rc, stdout=stdout, stderr=stderr)


class ConsoleRun:
    """The console's `run` seam with DOCKER removed and the tools kept real.

    `docker compose ps hive-server` answers from `server_up`; every `python -m
    hive.tools.repoctl …` exec is dispatched to the REAL `repoctl.main` against a REAL
    SQLite file (a fresh connection per call, exactly as a fresh `docker exec` gets).
    `script` entries (predicate, CompletedProcess) are tried FIRST, so a test can force
    an upstream fault without faking the happy path.
    """

    def __init__(self, db_path, *, server_up=True, child_env=None, script=()):
        self.db_path = db_path
        self.server_up = server_up
        self.child_env = dict(child_env or {})
        self.script = list(script)
        self.calls: list[list[str]] = []

    def __call__(self, argv, env=None, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        for pred, result in self.script:
            if pred(argv):
                return result
        if _seq_in(argv, "ps", cli.SERVICE):
            return _completed(
                argv,
                0 if self.server_up else 1,
                stdout="hive-server   Up (healthy)\n" if self.server_up else "",
            )
        if _seq_in(argv, "-m", "hive.tools.repoctl"):
            i = argv.index("hive.tools.repoctl")
            return self._repoctl(argv, argv[i + 1 :])
        return _completed(argv, 0)

    def _repoctl(self, argv, sub):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = repoctl.main(["--db", self.db_path, *sub], env=self.child_env, out=out)
        return _completed(argv, rc, stdout=out.getvalue(), stderr=err.getvalue())

    @property
    def execs(self) -> list[list[str]]:
        """Only the IN-CONTAINER children — the ones a down stack must never spawn."""
        return [c for c in self.calls if _seq_in(c, "exec")]


class Console:
    """One running operator console: a real loopback socket over the ConsoleRun seam."""

    def __init__(self, port: int, run: ConsoleRun, db_path: str):
        self.port = port
        self.run = run
        self.db_path = db_path

    # ── the browser's side of the wire ──
    def get(self, path: str):
        return self._send(urllib.request.Request(self._url(path), method="GET"))

    def post(self, path: str, obj=None):
        req = urllib.request.Request(
            self._url(path),
            data=json.dumps(obj).encode() if obj is not None else b"",
            method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Origin", f"http://127.0.0.1:{self.port}")
        return self._send(req)

    def _url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def _send(self, req):
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as err:
            return err.code, json.loads(err.read() or b"null")

    def connections(self) -> dict:
        """The served connections document, guarded so a route that does not answer
        fails as a STATED contract rather than as a KeyError three lines later."""
        status, body = self.get("/api/repos")
        assert status == 200, (
            f"GET /api/repos must serve the connections document: {body}"
        )
        return body

    # ── the store's side of the wire (arrival evidence) ──
    def store(self) -> SqliteEpisodeStore:
        return SqliteEpisodeStore(connect(self.db_path))

    def registry(self) -> dict:
        """The DURABLE registry as it really is — the consumer-side read."""
        return {
            row["name"]: dict(row)
            for row in self.store().conn.execute("SELECT * FROM repos ORDER BY name")
        }

    def write_sync(self, repo: str, **fields: str) -> None:
        """Stand in for a tick: write per-repo sync meta through the GRAMMAR's own
        builders, so this file names no meta key."""
        store = self.store()
        for field, value in fields.items():
            store.meta_set(KEY_BUILDERS[field](repo), value)

    def write_fleet(self, **fields: str) -> None:
        """Stand in for the tick SHELL: the fleet keys, through their own builders."""
        store = self.store()
        for field, value in fields.items():
            store.meta_set(FLEET_KEY_BUILDERS[field](), value)


@pytest.fixture()
def console(tmp_path):
    """Factory for a live console. Each one gets its own real SQLite registry file and a
    real ThreadingHTTPServer on an ephemeral loopback port; all are torn down after."""
    made: list = []
    counter = {"n": 0}

    def _make(*, server_up=True, env=None, child_env=None, script=()):
        counter["n"] += 1
        db_path = str(tmp_path / f"shared-{counter['n']}.db")
        SqliteEpisodeStore(connect(db_path))  # materialize the v3 schema
        run = ConsoleRun(
            db_path, server_up=server_up, child_env=child_env, script=script
        )
        httpd = ThreadingHTTPServer(
            ("127.0.0.1", 0), ui._build_handler(run, dict(env or {}))
        )
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        made.append((httpd, thread))
        return Console(httpd.server_address[1], run, db_path)

    try:
        yield _make
    finally:
        for httpd, thread in made:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)


# ── the page's own script, sliced per top-level function ──────────────────────

_JS = ui_page.PAGE_HTML.split("<script>", 1)[-1].split("</script>", 1)[0]


def _function_containing(needle: str) -> str:
    """The source of the ONE page-script function that mentions `needle`, from its
    declaration to the next top-level member of the IIFE. Lets a page-level contract be
    stated without naming the implementation's own identifiers."""
    starts = [m.start() for m in re.finditer(r"\n  (?:async )?function ", _JS)]
    assert starts, "the page script has no top-level functions"
    bounds = list(zip(starts, starts[1:] + [len(_JS)]))
    hits = [_JS[a:b] for a, b in bounds if needle in _JS[a:b]]
    assert len(hits) == 1, (
        f"expected exactly ONE page function mentioning {needle!r}, found {len(hits)}"
    )
    return hits[0]


class _NoRun:
    """A `run` seam that must never be called — `connect` is a purely local verb."""

    def __call__(self, *a, **k):
        raise AssertionError("`hive connect` must spawn no child")


def _connect_stdout(env: dict) -> str:
    """What `hive connect` PRINTS for this environment — the byte-exact line the doors
    card must be identical to."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.main(["connect"], run=_NoRun(), out=out, env=env)
    assert rc == cli.EX_OK
    return out.getvalue().strip()


# ══ intent 1 · connect a repo ═════════════════════════════════════════════════


def test_add_nominal_registers_and_lists(console):
    c = console()
    status, body = c.post("/api/repos", {"url": URL})
    assert status == 200, body
    # arrival evidence: the row is in the DURABLE registry, read back store-side.
    assert c.registry()["octo"]["url"] == URL
    status, listed = c.get("/api/repos")
    assert status == 200
    assert [r["name"] for r in listed["repos"]] == ["octo"]


def test_add_full_form_persists_every_field(console):
    c = console()
    status, body = c.post(
        "/api/repos",
        {"url": URL, "name": "octo", "branch": "trunk", "token_env": "OCTO_TOKEN"},
    )
    assert status == 200, body
    row = c.registry()["octo"]
    assert row["url"] == URL
    assert row["canonical_ref"] == "trunk"
    assert row["token_env"] == "OCTO_TOKEN"
    _, listed = c.get("/api/repos")
    served = {r["name"]: r for r in listed["repos"]}["octo"]
    assert served["canonical_ref"] == "trunk" and served["token_env"] == "OCTO_TOKEN"


def test_add_blank_url_is_400_before_any_child(console):
    c = console()
    for payload in ({"url": "   "}, {}, {"name": "octo"}):
        c.run.calls.clear()
        status, body = c.post("/api/repos", payload)
        assert status == 400 and body["error"] == "bad_request", payload
        assert c.run.calls == [], (
            f"a blank url is refused before ANY child, including the stack "
            f"up-check: {payload}"
        )
    assert c.registry() == {}


def test_add_invalid_slug_reason_reaches_operator_not_502(console):
    c = console()
    status, body = c.post("/api/repos", {"url": URL, "name": "Bad Name!"})
    assert status == 400, (
        "an operator-FIXABLE refusal must be distinguishable from an infrastructure "
        f"failure — a generic 502 tells the operator nothing they can act on: {body}"
    )
    assert "slug" in body["reason"], body
    assert c.registry() == {}


def test_add_duplicate_name_reason_reaches_operator(console):
    c = console()
    assert c.post("/api/repos", {"url": URL, "name": "octo"})[0] == 200
    status, body = c.post("/api/repos", {"url": OTHER_URL, "name": "octo"})
    assert status == 400, body
    assert "already registered" in body["reason"], body
    assert c.registry()["octo"]["url"] == URL, "the existing row is untouched"


def test_add_token_env_is_a_name_never_a_value(console):
    # The env CARRIES the secret; naming the var must move the NAME and nothing else —
    # and naming an UNSET var is a valid registration (the daemon resolves at tick time).
    c = console(child_env={"OCTO_TOKEN": SECRET})
    named = c.post(
        "/api/repos", {"url": URL, "name": "octo", "token_env": "OCTO_TOKEN"}
    )
    assert named[0] == 200, named
    unset = c.post(
        "/api/repos",
        {"url": OTHER_URL, "name": "other", "token_env": "NEVER_SET_TOKEN"},
    )
    assert unset[0] == 200, "naming an unset env var is a valid posture, not an error"

    rows = c.registry()
    assert rows["octo"]["token_env"] == "OCTO_TOKEN"
    assert rows["other"]["token_env"] == "NEVER_SET_TOKEN"
    dumped = " ".join(str(v) for row in rows.values() for v in row.values())
    assert SECRET not in dumped, "no secret byte may land in the registry"
    _, listed = c.get("/api/repos")
    assert SECRET not in json.dumps(listed), "no secret byte may ride the served list"


def test_add_when_server_down_spawns_no_child(console):
    c = console(server_up=False)
    status, body = c.post("/api/repos", {"url": URL})
    assert status in (400, 503), body
    assert body["error"] == "server_down", body
    assert c.run.execs == [], "an unreachable stack costs ZERO in-container exec"
    assert c.registry() == {}


# ══ intent 2 · view every connection + its live state ═════════════════════════


def test_list_serves_registry_plus_daemon_state(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo", "branch": "trunk"})
    c.write_sync(
        "octo",
        tracked_ref="trunk",
        last_tip="a" * 40,
        last_sync_ts="1700000000",
        backfilled_total="3",
    )
    status, body = c.get("/api/repos")
    assert status == 200, body
    (row,) = body["repos"]
    assert row["name"] == "octo" and row["url"] == URL
    assert row["canonical_ref"] == "trunk"
    assert row["sync"]["tracked_ref"] == "trunk"
    assert row["sync"]["last_tip"] == "a" * 40
    assert row["sync"]["last_sync_ts"] == "1700000000"
    assert row["sync"]["backfilled_total"] == 3
    assert "days_since_last_change_outcome" in row
    assert set(body["fleet"]) == set(FLEET_STR_FIELDS), (
        "the fleet block is ALWAYS there"
    )


def test_empty_registry_is_not_an_error(console):
    c = console()
    status, body = c.get("/api/repos")
    assert status == 200, body
    assert body["repos"] == []
    assert set(body["fleet"]) == set(FLEET_STR_FIELDS)
    low = ui_page.PAGE_HTML.lower()
    assert "no repos connected yet" in low, (
        "an empty registry reads as empty, not broken"
    )


def test_registered_never_synced_reads_absent_not_zero(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    body = c.connections()
    (row,) = body["repos"]
    assert row["days_since_last_change_outcome"] is None
    assert "sync" not in row, (
        "a registered-but-never-synced repo has NO observed state — absent, never a "
        "confident 0 or an invented verdict"
    )
    assert "healthy" not in json.dumps(row).lower()


def test_faulted_repo_shows_last_error_verbatim(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    c.post("/api/repos", {"url": OTHER_URL, "name": "other"})
    fault = "fetch failed: token env var OCTO_TOKEN is not set"
    c.write_sync("octo", last_error=fault, tracked_ref="main")
    c.write_sync("other", tracked_ref="main", last_sync_ts="1700000000")
    body = c.connections()
    rows = {r["name"]: r for r in body["repos"]}
    assert rows["octo"]["sync"]["last_error"] == fault, "verbatim — never summarized"
    assert rows["other"]["sync"]["last_error"] is None, "one repo's fault is its own"
    assert rows["other"]["sync"]["last_sync_ts"] == "1700000000"


def test_dead_daemon_surfaces_in_the_fleet_block(console):
    # BUG-062's shape: a shell fault leaves every per-repo block at its LAST-HEALTHY
    # values, so the fleet block is the only place the operator can see the daemon died.
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    c.write_sync("octo", tracked_ref="main", last_sync_ts="1700000000")
    c.write_fleet(last_error="tick failed: registry read")
    body = c.connections()
    (row,) = body["repos"]
    assert row["sync"]["last_sync_ts"] == "1700000000"  # looks healthy per-repo…
    assert body["fleet"]["last_error"] == "tick failed: registry read"  # …but is not


def test_cli_registered_repo_appears_in_the_console(console, capsys):
    # cross-source: the registry has ONE truth, so a repo registered from the CLI is a
    # first-class row in the console — the console is a projection, not a second store.
    c = console()
    rc = cli.main(
        ["repo", "add", URL, "--name", "octo"], run=c.run, out=io.StringIO(), env={}
    )
    capsys.readouterr()
    assert rc == cli.EX_OK
    assert [r["name"] for r in c.connections()["repos"]] == ["octo"]


def test_list_when_server_down_spawns_no_child(console):
    c = console(server_up=False)
    status, body = c.get("/api/repos")
    assert status in (400, 503), body
    assert body["error"] == "server_down", body
    assert c.run.execs == [], "an unreachable stack costs ZERO in-container exec"


def test_list_upstream_failure_is_502(console):
    c = console(
        script=[(lambda a: _seq_in(a, "hive.tools.repoctl"), _completed([], rc=70))]
    )
    status, body = c.get("/api/repos")
    assert status == 502 and body == {"error": "upstream_failed"}, body


def test_list_malformed_json_is_502(console):
    c = console(
        script=[
            (
                lambda a: _seq_in(a, "hive.tools.repoctl"),
                _completed([], rc=0, stdout="not json"),
            )
        ]
    )
    status, body = c.get("/api/repos")
    assert status == 502 and body == {"error": "upstream_failed"}, body


def test_served_fields_come_from_sync_keys_not_literals(console):
    # The served live-state field set has ONE owner. It is asserted here by CONSTRUCTING
    # the meta through the grammar's own builders and comparing the served document to
    # census_health_report's own output — a field named by hand in the console (or here)
    # cannot pass, and a new field the daemon starts writing flows through untouched.
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    c.write_sync(
        "octo",
        **{f: ("7" if f in COUNTER_FIELDS else f"<{f}>") for f in KEY_BUILDERS},
    )
    body = c.connections()
    (row,) = body["repos"]

    expected = census_health_report(c.store())
    assert row["sync"] == expected["repos"]["octo"]["sync"]
    assert body["fleet"] == expected["fleet"]

    assert set(KEY_BUILDERS) <= set(row["sync"]), (
        "every field the daemon has a WRITER for is served"
    )
    for field in STR_FIELDS:
        assert row["sync"][field] == f"<{field}>"
    for field in COUNTER_FIELDS:
        assert row["sync"][field] == 7


# ══ intent 3 · disconnect a repo ══════════════════════════════════════════════


def test_remove_nominal_deregisters(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    assert "octo" in c.registry()
    status, body = c.post("/api/repos/remove", {"name": "octo"})
    assert status == 200, body
    assert c.registry() == {}, "arrival evidence: the row is really gone"
    _, listed = c.get("/api/repos")
    assert listed["repos"] == []


def test_remove_unknown_name_is_a_stated_refusal(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    status, body = c.post("/api/repos/remove", {"name": "ghost"})
    assert status == 400, body
    assert "ghost" in body["reason"], body
    assert "octo" in c.registry(), "an unknown name destroys nothing"


def test_remove_blank_name_is_400_before_any_child(console):
    c = console()
    c.post("/api/repos", {"url": URL, "name": "octo"})
    for payload in ({"name": "  "}, {}):
        c.run.calls.clear()
        status, body = c.post("/api/repos/remove", payload)
        assert status == 400 and body["error"] == "bad_request", payload
        assert c.run.calls == [], "refused before ANY child, incl. the stack up-check"
    assert "octo" in c.registry()


def test_remove_requires_typed_name_before_any_request():
    # Page-level: a cancel or a mistyped name must send NOTHING. Asserted structurally
    # here (the confirm and its early return precede the request in the one function
    # that disconnects); the real browser proof is the live cycle.
    fn = _function_containing("/api/repos/remove")
    prompt_at = fn.find("window.prompt(")
    request_at = fn.index("/api/repos/remove")
    assert 0 <= prompt_at < request_at, (
        "the typed confirmation must be asked BEFORE the request is built"
    )
    guard = fn[prompt_at:request_at]
    assert re.search(r"!==|!=", guard), "the typed value must be COMPARED to the name"
    assert "return" in guard, "a mismatch or cancel must return without sending"


def test_page_states_memories_are_kept_on_disconnect():
    low = ui_page.PAGE_HTML.lower()
    assert "memories are kept" in low, (
        "disconnect stops the feed and prunes the mirror — the operator must be told "
        "the memories survive, so the affordance is not read as destructive"
    )


# ══ intent 4 · the doors an agent connects through ════════════════════════════


def test_doors_local_line_is_byte_identical_to_connect(console):
    env: dict = {}
    c = console(env=env)
    status, body = c.get("/api/doors")
    assert status == 200, body
    assert body["line"] == _connect_stdout(env)
    assert "Authorization: Bearer" not in body["line"], "the loopback door is tokenless"
    assert c.run.calls == [], "reading the doors spawns no child at all"
    assert "/api/doors" in ui_page.PAGE_HTML, "the page reads the line from the server"


def test_doors_public_line_is_byte_identical_to_connect(console):
    for given in (
        "https://hive.example.dev",
        "https://hive.example.dev/",
        "https://hive.example.dev/mcp",
    ):
        env = {"HIVE_PUBLIC_URL": given}
        c = console(env=env)
        status, body = c.get("/api/doors")
        assert status == 200, body
        assert body["line"] == _connect_stdout(env), given
        assert "<seat-token>" in body["line"], "a literal placeholder, never a token"
        assert "https://hive.example.dev/mcp" in body["line"], given
        assert "/mcp/mcp" not in body["line"], "the endpoint path is never doubled"


def test_doors_tunnel_line_is_byte_identical_to_connect(console):
    env = {"NGROK_DOMAIN": "brain.ngrok.app"}
    c = console(env=env)
    status, body = c.get("/api/doors")
    assert status == 200, body
    assert body["line"] == _connect_stdout(env)
    assert "https://brain.ngrok.app/mcp" in body["line"]
    assert "<seat-token>" in body["line"]


def test_doors_public_url_wins_over_ngrok(console):
    env = {
        "HIVE_PUBLIC_URL": "https://hive.example.dev",
        "NGROK_DOMAIN": "brain.ngrok.app",
    }
    c = console(env=env)
    status, body = c.get("/api/doors")
    assert status == 200, body
    assert body["line"] == _connect_stdout(env)
    assert "hive.example.dev" in body["line"] and "ngrok" not in body["line"]


def test_doors_never_emit_expansion_syntax_or_a_token(console):
    # The line is copy-pasted on an unknown OS/shell, so ANY expansion syntax would be
    # wrong on two of three; and a real token must never reach a rendered surface.
    for posture in (
        {},
        {"HIVE_PUBLIC_URL": "https://hive.example.dev"},
        {"NGROK_DOMAIN": "brain.ngrok.app"},
    ):
        c = console(env=dict(posture, HIVE_SEAT_TOKEN="hive_real_token_value"))
        status, body = c.get("/api/doors")
        assert status == 200, body
        # Expansion syntax is judged on the LINE — the only part that is ever pasted
        # into a shell. (The note is prose, and quotes a CLI verb in backticks.)
        for expansion in ("${", "$env:", "%HIVE", "`"):
            assert expansion not in body["line"], (posture, expansion)
        # A real token must never reach ANY rendered surface, note included.
        assert "hive_real_token_value" not in json.dumps(body), posture

    low = ui_page.PAGE_HTML.lower()
    assert "http://" not in low and "https://" not in low, (
        "the connect line arrives as RUNTIME DATA — the page stays hermetic and gains "
        "no URL literal of its own"
    )
