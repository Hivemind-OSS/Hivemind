"""The mirror directory NAME contract — the one owner of a mirror's identity.

A mirror is a cache of exactly ONE remote, so the directory it lives in is a
function of BOTH the registry name and the URL. Two invariants carry the whole
design. A changed URL must give a changed directory, so a name re-registered
against a different remote cannot reach the dead incarnation's checkout at all.
And the result must stay inside the reconciliation prune's candidate grammar — a
directory name the prune cannot recognise as its own would never be reaped, so a
deregistered repo's mirror would leak forever.
"""

from __future__ import annotations

import os
import subprocess
import sys

from hypothesis import given, settings
from hypothesis import strategies as st

from hive.app.sync import _REPO_SLUG_RE, mirror_dirname

URL = "https://example.invalid/o/r.git"
# the registry's own name grammar (store_sqlite.repo_add's gate)
SLUG_CHARS = "abcdefghijklmnopqrstuvwxyz0123456789._-"


def test_mirror_dirname_is_deterministic():
    first = mirror_dirname("alpha", URL)
    assert [mirror_dirname("alpha", URL) for _ in range(3)] == [first] * 3


def test_distinct_urls_under_one_name_give_distinct_dirs():
    """The load-bearing property: a re-registration at a new remote lands somewhere
    else, so reusing the previous repository's checkout is unconstructable rather
    than merely detected."""
    assert mirror_dirname("alpha", "https://example.invalid/o/a.git") != mirror_dirname(
        "alpha", "https://example.invalid/o/b.git"
    )


def test_one_url_under_distinct_names_gives_distinct_dirs():
    """Two registry rows may legitimately name the SAME remote; the name stays in
    the path so they keep separate checkouts and the prune needs no refcount."""
    assert mirror_dirname("alpha", URL) != mirror_dirname("beta", URL)


def test_the_registry_name_stays_the_prefix():
    """Mirrors stay greppable by registry name — the operator workflow and the
    ``sync.mirror_cloned`` / ``sync.mirror_pruned`` log lines both assume it."""
    assert mirror_dirname("alpha", URL).startswith("alpha-")


def test_mirror_dirname_is_stable_across_processes():
    """No salt, no ``hash()``: a value that moved between interpreter runs would
    orphan every mirror at each boot and re-clone the whole fleet."""
    code = (
        "from hive.app.sync import mirror_dirname;"
        f"print(mirror_dirname('alpha', {URL!r}))"
    )
    seen = set()
    for seed in ("0", "12345"):
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONHASHSEED": seed},
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip())
    assert seen == {mirror_dirname("alpha", URL)}


@settings(max_examples=200, deadline=None)
@given(
    name=st.text(alphabet=SLUG_CHARS, min_size=1, max_size=40),
    url=st.text(max_size=200),
)
def test_the_result_is_always_a_prune_candidate(name: str, url: str) -> None:
    """The prune only ever considers children matching the registry slug grammar,
    so a name outside it would be UNREAPABLE — a deregistered repo's mirror would
    survive every tick in silence. The URL half of the input space is arbitrary
    (an operator can register any string), which is why this is generated rather
    than a handful of rows."""
    assert _REPO_SLUG_RE.fullmatch(mirror_dirname(name, url)) is not None
