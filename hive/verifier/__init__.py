"""hive verifier — machine-run verification evidence (classes 1-2) for AI code changes.

The execution organ of the verification constellation: given a composer-supplied
touched-set, run the target repo's own typechecker and affected tests and report a
fail-closed, SHA-bound evidence bundle. The affected-test set comes from the matrix
engine; this package parses no diff and builds no graph — it runs and reports.

Fail-closed: ``not_run`` and ``errored`` never read as a pass.
"""

__version__ = "0.0.1"

from hive.verifier.evidence import EvidenceTag, tag_tests, tag_typecheck
from hive.verifier.registry import REGISTRY, REGISTRY_VERSION
from hive.verifier.result import (
    ClassResult,
    RunState,
    VerifierToolVersion,
    VerifyResult,
)
from hive.verifier.touched import TouchedFile, TouchedSet
from hive.verifier.verify import VerifyOptions, verify

__all__ = [
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
]
