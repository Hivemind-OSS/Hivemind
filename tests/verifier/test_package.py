"""Bootstrap smokes: the moved-in package imports and keeps its public surface.

The standalone wheel-build smoke retired with the move — hive.verifier ships
inside the hive distribution now (its packaging is the hive wheel's problem).
"""

from __future__ import annotations

import subprocess
import sys

import hive.verifier


def test_package_imports() -> None:
    assert hive.verifier.__name__ == "hive.verifier"
    assert isinstance(hive.verifier.__version__, str)


# The complete public surface a consumer (census) programs against. Exact
# equality: a name added or dropped here is a conscious API decision, and
# every exported name must actually resolve from the top level.
PUBLIC_SURFACE = frozenset(
    {
        "REGISTRY",
        "REGISTRY_VERSION",
        "ClassResult",
        "EvidenceTag",
        "RunState",
        "TouchedFile",
        "TouchedSet",
        "VerifierToolVersion",
        "VerifyOptions",
        "VerifyResult",
        "tag_tests",
        "tag_typecheck",
        "verify",
    }
)


def test_public_export_surface() -> None:
    assert set(hive.verifier.__all__) == PUBLIC_SURFACE
    for name in sorted(PUBLIC_SURFACE):
        assert getattr(hive.verifier, name) is not None


def test_importing_verifier_never_imports_matrix() -> None:
    """matrix reads MATRIX_OUT at import time, so a module-level matrix import
    would pin the environment of every consumer that merely imports this
    package; the engine handle is injected, so matrix may load only inside a
    verify() call."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import hive.verifier, hive.verifier.verify; "
            "sys.exit(1 if 'matrix' in sys.modules else 0)",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, "importing hive.verifier pulled in matrix"
