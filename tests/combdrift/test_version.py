"""ADD-3: VerifierVersion + verifier_version() — SHA + verifier-generation stamp.

Binds a change verdict to the commit SHAs and to both code generations
(combdrift semver + fingerprint token format), so a consumer can detect a stale
receipt by comparison rather than trusting it silently. Pure — no IO, no git.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from hive.combdrift.fingerprint import FINGERPRINT_VERSION
from hive.combdrift.version import COMBDRIFT_VERSION, VerifierVersion, verifier_version


def test_stamp_binds_both_shas():
    v = verifier_version(base_sha="aaa", head_sha="bbb")
    assert isinstance(v, VerifierVersion)
    assert v.base_sha == "aaa"
    assert v.head_sha == "bbb"


def test_stamp_carries_both_generations():
    v = verifier_version(base_sha="a", head_sha="b")
    assert v.combdrift_version == COMBDRIFT_VERSION
    assert v.fingerprint_version == FINGERPRINT_VERSION


def test_fingerprint_version_tracks_the_format_owner():
    # Single source: the stamp reads the format owner's constant, not a copy.
    assert (
        verifier_version(base_sha="a", head_sha="b").fingerprint_version
        == FINGERPRINT_VERSION
    )


def test_stamp_is_frozen():
    v = verifier_version(base_sha="a", head_sha="b")
    with pytest.raises(FrozenInstanceError):
        v.head_sha = "x"  # type: ignore[misc]


def test_shas_are_keyword_only():
    # Keyword-only prevents a silent base/head positional swap at the call site.
    with pytest.raises(TypeError):
        verifier_version("a", "b")  # type: ignore[call-arg]
