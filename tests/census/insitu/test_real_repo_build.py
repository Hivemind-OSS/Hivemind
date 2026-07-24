"""In-situ tier: the composed CLI against a REAL repository commit pair.

Env-gated (skipped unless CENSUS_INSITU_REPO/_BASE/_HEAD name a repo and a
commit pair) because it builds real code graphs of a real project twice. The
run is a fresh-interpreter subprocess emitting an unsigned envelope; assertions
are the pair-independent invariants — the embedded receipt validates, provenance
binds the resolved SHAs and the verifier stamp, execution classes 1-2 ride LIVE
(decided or honestly abstaining, never producer-absent), class 4 stays not-run —
and the drift/finding/execution shape of the pair is printed as the recorded
summary.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from hive.census.schema import validate_receipt

_REPO = os.environ.get("CENSUS_INSITU_REPO")
_BASE = os.environ.get("CENSUS_INSITU_BASE")
_HEAD = os.environ.get("CENSUS_INSITU_HEAD")

pytestmark = pytest.mark.skipif(
    not (_REPO and _BASE and _HEAD),
    reason="in-situ tier: set CENSUS_INSITU_REPO/_BASE/_HEAD to a real repo + commit pair",
)


def _rev_parse(repo: str, ref: str) -> str:
    proc = subprocess.run(
        ["git", "-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def test_real_repo_build(tmp_path: Path) -> None:
    out = tmp_path / "receipt.envelope.json"
    env = {key: value for key, value in os.environ.items() if key != "MATRIX_OUT"}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "hive.census.cli",
            "build",
            "--repo",
            _REPO,
            "--base",
            _BASE,
            "--head",
            _HEAD,
            "--out",
            str(out),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, proc.stderr
    assert not (tmp_path / "matrix-out").exists()

    envelope = json.loads(out.read_text())
    assert envelope["unsigned"] is True
    receipt = json.loads(base64.b64decode(envelope["payload"]))["predicate"]
    validate_receipt(receipt)

    base_sha = _rev_parse(_REPO, _BASE)
    head_sha = _rev_parse(_REPO, _HEAD)
    provenance = receipt["provenance"]
    assert provenance["base_sha"] == base_sha
    assert provenance["head_sha"] == head_sha
    assert provenance["combdrift"]["combdrift_version"]
    for side, sha in (("base", base_sha), ("head", head_sha)):
        assert provenance["matrix"][side]["commit_sha"] == sha
        assert provenance["matrix"][side]["graph_sha256"]

    lines = receipt["lines"]
    assert lines, "a real change pair must produce evidence lines"
    by_class: dict[str, list[dict]] = {}
    for line in lines:
        by_class.setdefault(line["class"], []).append(line)

    # Execution classes 1-2 ride LIVE: whatever each class decided (or the
    # reason it abstained), it was never producer absence, and a decided
    # state carries its coherent scoped tag + the runners that produced it.
    import hive.verifier

    for cls in ("typecheck", "tests"):
        (execution_line,) = by_class[cls]
        state = execution_line["detail"]["state"]
        assert state in {"passed", "failed", "not_run", "errored"}
        assert execution_line["reason"] != "producer-absent"
        if state in {"passed", "failed"}:
            assert execution_line["tag"] == "bounded-estimate"
        else:
            assert execution_line["tag"] == "unverified"
            assert execution_line["reason"]
    (tests_line,) = by_class["tests"]
    if tests_line["detail"]["state"] in {"passed", "failed"}:
        assert tests_line["detail"]["runners"]
    (differential,) = by_class["differential"]
    assert differential["tag"] == "not-run"

    verifier_stamp = provenance["verifier"]
    assert verifier_stamp["base_sha"] == base_sha
    assert verifier_stamp["head_sha"] == head_sha
    assert verifier_stamp["registry_version"] == hive.verifier.REGISTRY_VERSION

    drift_histogram = Counter(
        line["detail"]["drift"] for line in by_class.get("contract", ())
    )
    regressions = by_class.get("regression", [])
    non_empty = [
        line
        for line in regressions
        if line["detail"]["callers"]
        or line["detail"]["dependents"]
        or line["detail"]["tests"]
    ]
    execution_summary = ", ".join(
        f"{cls}={by_class[cls][0]['detail']['state']}"
        f"[{','.join(by_class[cls][0]['detail']['runners']) or 'no-runner'}]"
        for cls in ("typecheck", "tests")
    )
    print(
        "\nin-situ summary "
        f"repo={_REPO} base={base_sha[:9]} head={head_sha[:9]}\n"
        f"  contract drift histogram: {dict(drift_histogram)}\n"
        f"  regression findings: {len(regressions)} "
        f"(non-empty impact: {len(non_empty)}, "
        f"abstained: {sum(1 for line in regressions if line['tag'] == 'unverified')})\n"
        f"  execution: {execution_summary}\n"
        f"  precision: {receipt['precision']}"
    )
