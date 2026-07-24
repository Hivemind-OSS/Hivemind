"""One-hop re-export following: resolve_anchor across a single named edge.

These exercise the §5 follow-outcome table — the 0-false-stale enumeration. A
clean landing on a found symbol upgrades indirect -> current; a clean landing
on provable absence upgrades indirect -> stale (symbol_missing); every
uncertainty (no target, second hop, parse error, submodule, escape) stays
indirect (unverifiable). Targets are read as data only, never imported.
"""

from __future__ import annotations

from pathlib import Path

from hive.combdrift.resolution import fingerprint_anchor, resolve_anchor
from hive.combdrift.types import (
    REASON_FINGERPRINT_VERSION_MISMATCH,
    REASON_OK,
    REASON_SIGNATURE_CHANGED,
    REASON_SYMBOL_INDIRECT,
    REASON_SYMBOL_MISSING,
    Anchor,
)

# --- Python: follow to a clean landing ---------------------------------------


def test_follow_reexport_to_found_is_current(make_repo):
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core.py": "def parse(token):\n    return token\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is True
    assert res.reason == REASON_OK
    # location points at the SOURCE declaration, not the importer.
    assert res.location == "pkg/core.py:1"


def test_follow_reexport_source_deleted_is_stale(make_repo):
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core.py": "def other():\n    return 1\n",  # parse is gone at the source
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is False
    assert res.location is None
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    # The followed target is named so the deletion is locatable.
    assert "(via pkg/core.py)" in res.reason


def test_follow_aliased_reexport_to_found_is_current(make_repo):
    # `as p` is followed by the original name (`parse`) in the target.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse as p\n",
            "pkg/core.py": "def parse(token):\n    return token\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "p"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "pkg/core.py:1"


def test_follow_to_package_init_is_current(make_repo):
    # The module candidate resolves to core/__init__.py when core.py is absent.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core/__init__.py": "def parse(x):\n    return x\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is True
    assert res.location == "pkg/core/__init__.py:1"


# --- Python: every uncertainty stays indirect (unverifiable) -----------------


def test_follow_submodule_name_is_indirect_not_stale(make_repo):
    # parse is a SUBMODULE of the core package, not a name in its __init__.
    # Absence in __init__ is not provable absence -> must not flag stale.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core/__init__.py": "",
            "pkg/core/parse.py": "def parse(x):\n    return x\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_follow_second_hop_is_unverifiable(make_repo):
    # The target re-exports parse again; one hop stops here.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core.py": "from .deep import parse\n",
            "pkg/deep.py": "def parse(x):\n    return x\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_follow_no_target_file_is_unverifiable(make_repo):
    # `from .gone import parse` with no gone.py: an absent module is never a
    # deleted-symbol signal.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .gone import parse\n",
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_follow_target_parse_error_is_unverifiable(make_repo):
    # The target will not parse; its error is unverifiable, not the importer's,
    # and never a deletion.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core.py": "def parse(:\n    pass\n",  # syntax error in the target
        }
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


def test_follow_target_outside_repo_is_unverifiable(make_repo, tmp_path: Path):
    # A specifier that escapes the repo is rejected by containment even though
    # the escaping target exists: re-exports cannot be used to leave the repo.
    repo = make_repo(
        {
            "api.py": "from ..secret import thing\n",
        }
    )
    (tmp_path / "secret.py").write_text(
        "def thing():\n    return 1\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("api.py", "thing"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


# --- JS/TS: barrel follow -----------------------------------------------------


def test_follow_ts_barrel_to_found_is_current(make_repo):
    repo = make_repo(
        {
            "src/index.ts": "export { Button } from './Button';\n",
            "src/Button.tsx": "export function Button() {\n  return null;\n}\n",
        }
    )
    res = resolve_anchor(repo, Anchor("src/index.ts", "Button"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "src/Button.tsx:1"


def test_follow_ts_barrel_source_deleted_is_stale(make_repo):
    repo = make_repo(
        {
            "src/index.ts": "export { Button } from './Button';\n",
            "src/Button.tsx": "export function Other() {\n  return null;\n}\n",
        }
    )
    res = resolve_anchor(repo, Anchor("src/index.ts", "Button"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "(via src/Button.tsx)" in res.reason


def test_follow_ts_named_import_to_found_is_current(make_repo):
    repo = make_repo(
        {
            "src/index.ts": "import { parse } from './core';\n",
            "src/core.ts": "export function parse(a: string) {\n  return a;\n}\n",
        }
    )
    res = resolve_anchor(repo, Anchor("src/index.ts", "parse"))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "src/core.ts:1"


def test_follow_ts_bare_specifier_stays_indirect(make_repo):
    # Even with a same-named local file, a bare specifier is never followed.
    repo = make_repo(
        {
            "src/index.ts": "import { useState } from 'react';\n",
            "react.ts": "export function useState() {}\n",
        }
    )
    res = resolve_anchor(repo, Anchor("src/index.ts", "useState"))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_INDIRECT)


# --- Layer B over the hop: capture/verify symmetry ----------------------------
# The capture seam follows the same edge as verify, so the baseline always
# describes the SOURCE declaration — capture and verify can never disagree on
# which shape is being compared.

_API = "from .core import parse\n"


def _pkg(core_body: str) -> dict[str, str]:
    return {"pkg/__init__.py": "", "pkg/api.py": _API, "pkg/core.py": core_body}


def test_fingerprint_anchor_follows_to_source(make_repo):
    # Capturing a re-export mints the source declaration's token — identical to
    # capturing that source directly, not None and not the importer's shape.
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    via_reexport = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    direct = fingerprint_anchor(repo, Anchor("pkg/core.py", "parse"))
    assert via_reexport is not None
    assert via_reexport.startswith("combdrift-fp/1:")
    assert via_reexport == direct


def test_follow_then_unchanged_is_current(make_repo):
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    fp = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse", fp))
    assert res.found is True
    assert res.reason == REASON_OK
    assert res.location == "pkg/core.py:1"


def test_follow_then_source_signature_drift_is_stale(make_repo, tmp_path: Path):
    # Baseline minted over the hop; the SOURCE then drops a required parameter.
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    fp = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    (Path(repo) / "pkg/core.py").write_text(
        "def parse(a):\n    return a\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse", fp))
    assert res.found is True  # symbol still resolves; only its shape drifted
    assert res.location == "pkg/core.py:1"
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)


def test_follow_then_additive_source_change_is_current(make_repo):
    # An added optional parameter at the source breaks no previously-valid call.
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    fp = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    (Path(repo) / "pkg/core.py").write_text(
        "def parse(a, b, c=1):\n    return (a, b, c)\n", encoding="utf-8"
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse", fp))
    assert res.found is True
    assert res.reason == REASON_OK


def test_reexport_removed_after_capture_is_stale(make_repo):
    # The importer drops the re-export line: the name is now missing at the
    # importer itself (no edge to follow), which is provable absence -> stale.
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    fp = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    (Path(repo) / "pkg/api.py").write_text("x = 1\n", encoding="utf-8")
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse", fp))
    assert res.found is False
    assert res.reason.startswith(REASON_SYMBOL_MISSING)
    assert "(via" not in res.reason  # missing at the importer, not at a source


def test_follow_then_source_overloaded_is_unverifiable(make_repo):
    # The source becomes overloaded after capture: an ambiguous shape has no
    # single interface to compare, so it is unverifiable, never stale.
    repo = make_repo(_pkg("def parse(a, b):\n    return (a, b)\n"))
    fp = fingerprint_anchor(repo, Anchor("pkg/api.py", "parse"))
    (Path(repo) / "pkg/core.py").write_text(
        "def parse(a):\n    return a\n\n\ndef parse(a, b):\n    return (a, b)\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("pkg/api.py", "parse", fp))
    assert res.found is True
    assert res.reason.startswith(REASON_FINGERPRINT_VERSION_MISMATCH)


def test_follow_ts_source_signature_drift_is_stale(make_repo):
    repo = make_repo(
        {
            "src/index.ts": "export { Button } from './Button';\n",
            "src/Button.tsx": "export function Button(label: string) {\n  return label;\n}\n",
        }
    )
    fp = fingerprint_anchor(repo, Anchor("src/index.ts", "Button"))
    assert fp is not None
    (Path(repo) / "src/Button.tsx").write_text(
        "export function Button(label: string, theme: string) {\n  return label;\n}\n",
        encoding="utf-8",
    )
    res = resolve_anchor(repo, Anchor("src/index.ts", "Button", fp))
    assert res.found is True
    assert res.reason.startswith(REASON_SIGNATURE_CHANGED)
