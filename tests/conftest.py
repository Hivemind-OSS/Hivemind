"""Session-wide MATRIX_OUT pin — taken before any test module can import matrix.

matrix reads the ``MATRIX_OUT`` env var exactly once, at import time
(``hive/matrix/paths.py``), so whatever pins it must win that race. In per-suite
isolation the first census/verifier fixture pins it in time; but in the combined
``make check`` run pytest imports EVERY test module during collection before it
runs any test, so collecting ``tests/matrix/*`` imports ``hive.matrix`` first —
and a later per-suite pin is then refused by the census engine guard
(``EngineError: matrix was imported before MATRIX_OUT was pinned``), taking all of
``tests/census`` down with it.

pytest imports this root conftest before it collects any test module, so pinning
here is the earliest hook that wins the race for the whole session. The pin runs
through census's own process-wide ``ensure_scratch_matrix_out`` helper rather than
setting the env var directly, so there is a single source of the value: census's
``_scratch`` and the env var stay one absolute scratch dir, which census's own
``matrix_scratch`` fixture returns idempotently and the verifier-conformance
harness adopts. Importing ``hive.census.engines`` does not import ``hive.matrix``
(every engine import there is lazy), so this call itself never loses the race.

This is a test-harness fix only: it changes nothing about matrix's read-once
laziness or hive.census's call-time import laziness (both load-bearing), and it
does not weaken the guard — it just guarantees the guard's precondition holds.
"""

from __future__ import annotations

from hive.census.engines import ensure_scratch_matrix_out

# Import-time, module scope: this executes while pytest is loading conftests,
# before it collects (imports) any test module under tests/.
ensure_scratch_matrix_out()
