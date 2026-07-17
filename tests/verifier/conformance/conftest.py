"""Conformance-tier pytest wiring; the machinery itself lives in _harness.

Keeps three duties: never collect the fixture repos (their test files are
payloads for the spawned runners — every red variant fails on purpose), mark
this tier for deselection, and gate each leg on its toolchain per the
skip-or-strict-fail contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.verifier.conformance import _harness

collect_ignore = ["fixtures"]


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "requires_tool(*tools): skip (or fail under "
        f"{_harness.STRICT_ENV}=1) when a named toolchain is absent",
    )
    config.addinivalue_line(
        "markers",
        "conformance: real-tool conformance tier (deselect with -m 'not conformance')",
    )


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(item.fspath).parents:
            item.add_marker(pytest.mark.conformance)


def pytest_runtest_setup(item: pytest.Item) -> None:
    marker = item.get_closest_marker("requires_tool")
    if marker is None:
        return
    unknown = [tool for tool in marker.args if tool not in _harness.PROBES]
    if unknown:  # a typo'd marker must never silently gate nothing
        raise RuntimeError(f"requires_tool names unknown tool(s): {unknown}")
    _harness.require_tools(*marker.args)


@pytest.fixture()
def ts_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _harness.node_tools(monkeypatch, "typescript", ("tsc", "vitest"))


@pytest.fixture()
def js_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    _harness.node_tools(monkeypatch, "javascript", ("eslint",))


@pytest.fixture()
def pg_env() -> tuple[tuple[str, str], ...]:
    return _harness.pg_connection_env()


@pytest.fixture()
def matrix_scratch() -> Path:
    """Pin MATRIX_OUT to a per-run scratch dir before the test imports matrix,
    so real build_graph runs never litter graph artifacts into the CWD."""
    return _harness.ensure_scratch_matrix_out()
