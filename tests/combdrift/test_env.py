"""Chunk 6: fail-fast secrets helper. Missing/empty -> RuntimeError naming the var."""

from __future__ import annotations

import pytest

from hive.combdrift.env import require_env


def test_require_env_returns_present_value(monkeypatch):
    monkeypatch.setenv("MSV_TEST_SECRET", "s3cr3t")
    assert require_env("MSV_TEST_SECRET") == "s3cr3t"


def test_require_env_raises_when_unset(monkeypatch):
    monkeypatch.delenv("MSV_TEST_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="MSV_TEST_SECRET"):
        require_env("MSV_TEST_SECRET")


def test_require_env_raises_when_empty(monkeypatch):
    # An empty string is treated as missing — fail fast, do not silently accept.
    monkeypatch.setenv("MSV_TEST_SECRET", "")
    with pytest.raises(RuntimeError, match="MSV_TEST_SECRET"):
        require_env("MSV_TEST_SECRET")
