"""Contract tests for the institutional-memory recall client.

The unit tier runs against an in-process fake hive speaking the real wire
contract — one plain JSON-RPC 2.0 ``tools/call`` POST per recall, the tool
payload riding as a JSON string inside ``result.content[0].text`` — so every
envelope shape, misbehavior, and cap is exercised without a live server (the
live loopback door belongs to the in-situ tier). The pins: labels pass
through verbatim with no tier filtered, an abstaining recall contributes
nothing, caps and dedupe hold, and ANY transport or envelope fault degrades
to an empty result with one stderr note — never a raise, never a partial
report.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hive.census.memory import (
    _TEXT_CAP,
    MemoryContext,
    fetch_institutional_context,
    order_subjects,
)


def _hit(episode_id: int, text: str, **overrides) -> dict:
    hit = {
        "episode_id": episode_id,
        "text": text,
        "sim": 0.87,
        "trust": "established",
        "ts": 1751800000,
        "polarity": "do",
        "kind": "gotcha",
        "anchor": "pkg/lib.py::greet",
    }
    hit.update(overrides)
    return hit


def _payload(hits: list, abstained: bool = False, **overrides) -> dict:
    payload = {"abstained": abstained, "reference_context": hits}
    payload.update(overrides)
    return payload


class _SilentServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):  # noqa: ARG002
        pass  # a handler cut off mid-sleep must not spray tracebacks


class _FakeHive:
    """In-process double of the hive's MCP door.

    ``respond`` maps a recall query to a tool payload dict, or to a wire
    misbehavior: ("status", code) | ("raw", bytes) | ("envelope", dict) |
    ("sleep", seconds). Every received JSON-RPC body is recorded.
    """

    def __init__(self, respond):
        self.respond = respond
        self.requests: list[dict] = []
        hive = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except Exception:
                    body = {}
                hive.requests.append(body if isinstance(body, dict) else {})
                params = body.get("params") if isinstance(body, dict) else None
                arguments = (
                    params.get("arguments") if isinstance(params, dict) else None
                )
                query = arguments.get("query") if isinstance(arguments, dict) else ""
                action = hive.respond(query)
                if isinstance(action, tuple) and action[0] == "sleep":
                    time.sleep(action[1])
                    action = _payload([])
                if isinstance(action, tuple) and action[0] == "status":
                    self.send_response(action[1])
                    self.end_headers()
                    return
                if isinstance(action, tuple) and action[0] == "raw":
                    out = action[1]
                elif isinstance(action, tuple) and action[0] == "envelope":
                    out = json.dumps(action[1]).encode("utf-8")
                else:
                    out = json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": body.get("id", 1) if isinstance(body, dict) else 1,
                            "result": {
                                "content": [
                                    {"type": "text", "text": json.dumps(action)}
                                ],
                                "isError": False,
                            },
                        }
                    ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):  # noqa: ARG002
                pass

        self._server = _SilentServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}/mcp"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@contextmanager
def fake_hive(respond):
    hive = _FakeHive(respond)
    try:
        yield hive
    finally:
        hive.close()


def _closed_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class TestOrderSubjects:
    def test_priority_band_then_lexicographic_deduped(self) -> None:
        triples = (
            ("z.py", "zeta", "additive"),
            ("a.py", "alpha", "breaking"),
            ("b.py", "beta", "unchanged"),
            ("a.py", "alpha", "breaking"),
            ("m.py", "mu", "removed"),
            ("a.py", "aaa", ""),
        )
        assert order_subjects(triples) == [
            ("a.py", "alpha"),
            ("m.py", "mu"),
            ("a.py", "aaa"),
            ("b.py", "beta"),
            ("z.py", "zeta"),
        ]

    def test_subject_with_both_drifts_keeps_its_priority_slot(self) -> None:
        triples = (
            ("d.py", "delta", "additive"),
            ("c.py", "gamma", ""),
            ("d.py", "delta", "removed"),
        )
        assert order_subjects(triples) == [("d.py", "delta"), ("c.py", "gamma")]

    def test_empty_is_empty(self) -> None:
        assert order_subjects(()) == []


class TestFetchServedHits:
    def test_entries_carry_labels_verbatim_across_all_tiers(self) -> None:
        hits = [
            _hit(
                41,
                "greet's punct is relied on by templating callers",
                trust="established",
                polarity="dont",
                kind="gotcha",
            ),
            _hit(
                42,
                "prefer explicit greeting arity",
                trust="provisional",
                polarity="do",
                kind="decision",
                anchor="",
                sim=0.61,
                ts=1750000000,
            ),
        ]
        with fake_hive(lambda q: _payload(hits)) as hive:
            entries = fetch_institutional_context(hive.url, [("pkg/lib.py", "greet")])
        assert entries == [
            MemoryContext(
                episode_id=41,
                trust="established",
                polarity="dont",
                kind="gotcha",
                anchor="pkg/lib.py::greet",
                ts=1751800000,
                sim=0.87,
                text="greet's punct is relied on by templating callers",
            ),
            MemoryContext(
                episode_id=42,
                trust="provisional",
                polarity="do",
                kind="decision",
                anchor="",
                ts=1750000000,
                sim=0.61,
                text="prefer explicit greeting arity",
            ),
        ]
        # Label-blind pass-through: the non-established tier rides too,
        # labelled — never filtered at this edge.
        assert {entry.trust for entry in entries} == {"established", "provisional"}

    def test_wire_shape_is_one_tools_call_per_subject(self) -> None:
        with fake_hive(lambda q: _payload([])) as hive:
            fetch_institutional_context(
                hive.url, [("pkg/a.py", "alpha"), ("pkg/b.py", "beta")]
            )
            requests = list(hive.requests)
        assert [r["params"]["arguments"]["query"] for r in requests] == [
            "pkg/a.py alpha",
            "pkg/b.py beta",
        ]
        for request in requests:
            assert request["jsonrpc"] == "2.0"
            assert request["method"] == "tools/call"
            assert request["params"]["name"] == "hive_recall"

    def test_cap_subjects_limits_queries(self) -> None:
        subjects = [(f"p{i}.py", "s") for i in range(10)]
        with fake_hive(lambda q: _payload([])) as hive:
            fetch_institutional_context(hive.url, subjects, cap_subjects=2)
            assert len(hive.requests) == 2

    def test_cap_total_truncates_and_stops_querying(self) -> None:
        def respond(query: str) -> dict:
            index = int(query[1])
            return _payload(
                [_hit(10 * index + j, f"lesson {index}.{j}") for j in (1, 2)]
            )

        subjects = [("p1.py", "s"), ("p2.py", "s"), ("p3.py", "s")]
        with fake_hive(respond) as hive:
            entries = fetch_institutional_context(hive.url, subjects, cap_total=4)
            request_count = len(hive.requests)
        assert len(entries) == 4
        assert (
            request_count == 2
        )  # the cap was reached; the third subject is never asked

    def test_duplicate_subjects_are_queried_once(self) -> None:
        with fake_hive(lambda q: _payload([])) as hive:
            fetch_institutional_context(
                hive.url, [("a.py", "f"), ("a.py", "f"), ("b.py", "g")]
            )
            assert len(hive.requests) == 2

    def test_abstained_recall_contributes_nothing_even_with_hits(self) -> None:
        def respond(query: str) -> dict:
            if query.startswith("mute"):
                # Adversarial: an abstaining envelope carrying hits anyway.
                return _payload(
                    [_hit(1, "never trust an abstained hit")], abstained=True
                )
            return _payload([_hit(2, "served lesson")])

        with fake_hive(respond) as hive:
            entries = fetch_institutional_context(
                hive.url, [("mute.py", "m"), ("loud.py", "l")]
            )
        assert [entry.episode_id for entry in entries] == [2]

    def test_dedupe_by_episode_id_across_subjects(self) -> None:
        with fake_hive(
            lambda q: _payload([_hit(7, "one lesson, two subjects")])
        ) as hive:
            entries = fetch_institutional_context(
                hive.url, [("a.py", "f"), ("b.py", "g")]
            )
        assert [entry.episode_id for entry in entries] == [7]

    def test_text_capped(self) -> None:
        with fake_hive(lambda q: _payload([_hit(1, "x" * 1000)])) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        assert len(entries[0].text) == _TEXT_CAP

    def test_no_subjects_or_zero_caps_ask_nothing(self) -> None:
        with fake_hive(lambda q: _payload([_hit(1, "t")])) as hive:
            assert fetch_institutional_context(hive.url, []) == []
            assert (
                fetch_institutional_context(hive.url, [("a.py", "f")], cap_subjects=0)
                == []
            )
            assert (
                fetch_institutional_context(hive.url, [("a.py", "f")], cap_total=0)
                == []
            )
            assert hive.requests == []


class TestFetchDefensiveParse:
    def test_malformed_hits_skipped_and_extra_keys_tolerated(self) -> None:
        hits = [
            "not a dict",
            {"episode_id": 1},  # no text
            _hit(2, ""),  # blank text
            {**_hit(3, "text ok"), "episode_id": "3"},  # non-int identity
            {**_hit(4, "bool identity"), "episode_id": True},
            _hit(
                5,
                "good with future enrichments",
                meta={"combdrift/fp": "combdrift-fp/1:function(a)"},
                remediation="this memory's anchor no longer matches the code",
            ),
            _hit(
                6,
                "labels degrade safe",
                trust=None,
                polarity=7,
                kind=[],
                anchor={},
                ts="old",
                sim="high",
            ),
        ]
        with fake_hive(lambda q: _payload(hits)) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        assert [entry.episode_id for entry in entries] == [5, 6]
        degraded = entries[1]
        assert (degraded.trust, degraded.polarity, degraded.kind, degraded.anchor) == (
            "",
            "",
            "",
            "",
        )
        assert degraded.ts == 0
        assert degraded.sim == 0.0

    def test_nonfinite_sim_sanitized(self) -> None:
        raw = (
            "raw",
            (
                '{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", '
                '"text": "{\\"abstained\\": false, \\"reference_context\\": '
                '[{\\"episode_id\\": 9, \\"text\\": \\"nan sim\\", \\"sim\\": NaN}]}"}], '
                '"isError": false}}'
            ).encode("utf-8"),
        )
        with fake_hive(lambda q: raw) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        assert [entry.episode_id for entry in entries] == [9]
        assert entries[0].sim == 0.0

    def test_missing_reference_context_is_no_entries_not_a_fault(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        with fake_hive(lambda q: {"abstained": False}) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        assert entries == []
        assert capsys.readouterr().err == ""


class TestFetchFailsOpen:
    def _assert_empty_with_one_note(self, entries, capsys) -> None:
        assert entries == []
        err = capsys.readouterr().err
        assert err.count("\n") == 1
        assert "institutional memory unavailable" in err

    def test_connection_refused(self, capsys: pytest.CaptureFixture) -> None:
        url = f"http://127.0.0.1:{_closed_port()}/mcp"
        entries = fetch_institutional_context(url, [("a.py", "f")])
        self._assert_empty_with_one_note(entries, capsys)

    def test_http_error(self, capsys: pytest.CaptureFixture) -> None:
        with fake_hive(lambda q: ("status", 500)) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        self._assert_empty_with_one_note(entries, capsys)

    def test_jsonrpc_error_envelope(self, capsys: pytest.CaptureFixture) -> None:
        envelope = (
            "envelope",
            {"jsonrpc": "2.0", "id": 1, "error": {"code": -32600, "message": "bad"}},
        )
        with fake_hive(lambda q: envelope) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        self._assert_empty_with_one_note(entries, capsys)

    def test_tool_iserror_envelope(self, capsys: pytest.CaptureFixture) -> None:
        envelope = (
            "envelope",
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "content": [{"type": "text", "text": "boom"}],
                    "isError": True,
                },
            },
        )
        with fake_hive(lambda q: envelope) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        self._assert_empty_with_one_note(entries, capsys)

    def test_unparseable_body(self, capsys: pytest.CaptureFixture) -> None:
        with fake_hive(lambda q: ("raw", b"<!doctype html>nope")) as hive:
            entries = fetch_institutional_context(hive.url, [("a.py", "f")])
        self._assert_empty_with_one_note(entries, capsys)

    def test_timeout(self, capsys: pytest.CaptureFixture) -> None:
        with fake_hive(lambda q: ("sleep", 1.0)) as hive:
            entries = fetch_institutional_context(
                hive.url, [("a.py", "f")], timeout_s=0.2
            )
        self._assert_empty_with_one_note(entries, capsys)

    def test_fault_after_a_good_subject_still_empties_the_result(
        self, capsys: pytest.CaptureFixture
    ) -> None:
        def respond(query: str):
            if query.startswith("good"):
                return _payload([_hit(1, "served before the door failed")])
            return ("status", 502)

        with fake_hive(respond) as hive:
            entries = fetch_institutional_context(
                hive.url, [("good.py", "g"), ("bad.py", "b")]
            )
        # All-or-nothing: a half-healthy door never half-reports.
        self._assert_empty_with_one_note(entries, capsys)
