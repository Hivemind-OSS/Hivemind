"""Container tests boot the entrypoint, which calls configure_json_logging() and binds a
stderr handler onto the global `hive` logger (with propagate=False). Snapshot + restore
that logger around every container test so the global mutation cannot leak into other test
files (e.g. break a caplog assertion on `hive.*` elsewhere)."""

from __future__ import annotations

import logging

import pytest


@pytest.fixture(autouse=True)
def _isolate_hive_logger():
    lg = logging.getLogger("hive")
    saved_handlers, saved_level, saved_propagate = (
        list(lg.handlers),
        lg.level,
        lg.propagate,
    )
    try:
        yield
    finally:
        lg.handlers[:] = saved_handlers
        lg.setLevel(saved_level)
        lg.propagate = saved_propagate
