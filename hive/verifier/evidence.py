"""RunState -> EvidenceTag — the single owner of the evidence-strength mapping.

Pure module. Downstream composers tag classes 1-2 with these two functions
instead of re-deriving the mapping. The honesty rules are structural:

- typecheck (class 1) that actually ran is ``machine-checked`` either way — a
  checker's verdict is a machine proof of what it checked, pass or fail;
- tests (class 2) that actually ran are only ever a ``bounded-estimate`` — a
  green suite bounds the regression estimate by what the suite covers, it
  never proves absence of regression;
- an abstention (``not_run``/``errored``) is ``abstain`` for both classes:
  no evidence is claimed where nothing ran to completion.
"""

from __future__ import annotations

from typing import Literal

from hive.verifier.result import ClassResult

EvidenceTag = Literal["machine-checked", "bounded-estimate", "abstain"]


def tag_typecheck(c: ClassResult) -> EvidenceTag:
    """Class 1: a completed check is machine-checked; anything else abstains."""
    if c.state in ("passed", "failed"):
        return "machine-checked"
    return "abstain"


def tag_tests(c: ClassResult) -> EvidenceTag:
    """Class 2: a completed run is a bounded estimate — NEVER machine-checked,
    even green; anything else abstains."""
    if c.state in ("passed", "failed"):
        return "bounded-estimate"
    return "abstain"
