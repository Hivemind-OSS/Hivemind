"""Report ingestion — the verifier's pure parse boundary.

Every ingester has the uniform shape ``(raw: bytes, *, exit_code: int) -> Ingested``
and either returns a fully parsed ``Ingested`` or raises ``ReportParseError``:
"unparsed but green" is unconstructable. Safe direction: a report that cannot be
read becomes a raise the orchestrator maps to ``errored`` — never silent zero
counts, never a KeyError escaping a malformed document.
"""

from __future__ import annotations

from collections.abc import Callable

from hive.verifier.result import Ingested


class ReportParseError(Exception):
    """A report was absent, empty, or malformed. Never swallowed into zero counts."""


Ingester = Callable[..., Ingested]

# These imports need ReportParseError bound above (the submodules import it back).
from hive.verifier.ingest.fallbacks import (  # noqa: E402
    ingest_cargo,
    ingest_gotext,
    ingest_native_exit,
    ingest_pyright,
    ingest_tsc,
)
from hive.verifier.ingest.junit import ingest_junit  # noqa: E402
from hive.verifier.ingest.sarif import ingest_sarif  # noqa: E402
from hive.verifier.ingest.tap import ingest_tap  # noqa: E402

INGESTERS: dict[str, Ingester] = {
    "junit": ingest_junit,
    "tap": ingest_tap,
    "sarif": ingest_sarif,
    "tsc": ingest_tsc,
    "pyright": ingest_pyright,
    "gotest": ingest_gotext,
    "cargo": ingest_cargo,
    "native-exit": ingest_native_exit,
}

# The capability registry keys tool recipes by these report formats; validating
# the dispatch here makes a missing ingester an import failure, never a
# KeyError in the middle of a verification run.
_EXPECTED_FORMATS = frozenset(
    {"junit", "tap", "sarif", "tsc", "pyright", "gotest", "cargo", "native-exit"}
)
if set(INGESTERS) != _EXPECTED_FORMATS or not all(
    callable(ingester) for ingester in INGESTERS.values()
):
    raise RuntimeError("INGESTERS must cover exactly the known report formats")

__all__ = [
    "INGESTERS",
    "Ingester",
    "ReportParseError",
    "ingest_cargo",
    "ingest_gotext",
    "ingest_junit",
    "ingest_native_exit",
    "ingest_pyright",
    "ingest_sarif",
    "ingest_tap",
    "ingest_tsc",
]
