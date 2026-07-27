"""Chunk 7: CLI surface — exit codes, JSON I/O, fail-fast env, public API."""

from __future__ import annotations

import json
from pathlib import Path


from hive.combdrift.cli import main
from hive.combdrift.resolution import fingerprint_anchor
from hive.combdrift.types import Anchor


def _write_records(tmp_path: Path, records: list[dict]) -> Path:
    p = tmp_path / "records.json"
    p.write_text(json.dumps(records), encoding="utf-8")
    return p


def _minted_anchor(repo: Path, path: str, symbol: str) -> dict:
    """An anchor dict carrying the symbol's real, freshly minted call shape.

    `current` is reserved for a claim that was actually compared, so a record
    that must classify as current records a live baseline here; an anchor
    without one has no shape claim to measure and is unverifiable.
    """
    return {
        "path": path,
        "symbol": symbol,
        "fingerprint": fingerprint_anchor(str(repo), Anchor(path, symbol)),
    }


def test_public_api_importable():
    # The two surfaces plus the deep resolver, the capture seam, and the data
    # types are re-exported.
    from hive.combdrift import (  # noqa: F401
        Anchor,
        AnchorResult,
        Record,
        RecordVerdict,
        RunSummary,
        fingerprint_anchor,
        resolve_anchor,
        verify_record,
        verify_records,
    )


def test_cli_all_current_exit_zero(tmp_repo: Path, tmp_path: Path, capsys):
    records = _write_records(
        tmp_path,
        [
            {
                "id": "a",
                "claim_text": "x",
                # Minted, so the record is current because its shape was compared.
                "anchors": [_minted_anchor(tmp_repo, "pkg/auth.py", "refresh")],
            },
        ],
    )
    code = main(["--records", str(records), "--repo", str(tmp_repo)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["summary"] == {"current": 1, "stale": 0, "unverifiable": 0}


def test_cli_stale_exit_code_and_output(tmp_repo: Path, tmp_path: Path, capsys):
    records = _write_records(
        tmp_path,
        [
            {
                "id": "ok",
                "claim_text": "x",
                # Minted, so "ok" is current on a compared shape and the stale
                # exit code below is driven only by the renamed-away anchor.
                "anchors": [_minted_anchor(tmp_repo, "pkg/auth.py", "refresh")],
            },
            {
                "id": "bad",
                "claim_text": "y",
                "anchors": [{"path": "pkg/auth.py", "symbol": "renamed_away"}],
            },
        ],
    )
    code = main(["--records", str(records), "--repo", str(tmp_repo)])
    assert code == 1  # >=1 stale -> non-zero
    out = json.loads(capsys.readouterr().out)
    assert set(out.keys()) == {"verdicts", "summary"}
    verdicts = {v["id"]: v["verdict"] for v in out["verdicts"]}
    assert verdicts == {"ok": "current", "bad": "stale"}
    assert out["summary"]["stale"] == 1


def test_cli_unverifiable_only_is_not_stale_exit_zero(tmp_repo: Path, tmp_path: Path):
    # Unverifiable is not stale; exit code stays 0 (only stale forces non-zero).
    records = _write_records(
        tmp_path,
        [
            {"id": "u", "claim_text": "x", "anchors": []},
        ],
    )
    code = main(["--records", str(records), "--repo", str(tmp_repo)])
    assert code == 0


def test_cli_reads_stdin(tmp_repo: Path, monkeypatch, capsys):
    import io

    payload = json.dumps(
        [
            {
                "id": "a",
                "claim_text": "x",
                "anchors": [{"path": "pkg/auth.py", "symbol": "refresh"}],
            },
        ]
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    code = main(["--records", "-", "--repo", str(tmp_repo)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["verdicts"][0]["id"] == "a"


def test_cli_writes_out_file(tmp_repo: Path, tmp_path: Path):
    records = _write_records(
        tmp_path,
        [
            {
                "id": "a",
                "claim_text": "x",
                "anchors": [{"path": "pkg/auth.py", "symbol": "refresh"}],
            },
        ],
    )
    out_path = tmp_path / "verdicts.json"
    code = main(
        [
            "--records",
            str(records),
            "--repo",
            str(tmp_repo),
            "--out",
            str(out_path),
        ]
    )
    assert code == 0
    out = json.loads(out_path.read_text(encoding="utf-8"))
    assert out["verdicts"][0]["id"] == "a"


def test_cli_bad_json_exit_2(tmp_repo: Path, tmp_path: Path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json", encoding="utf-8")
    code = main(["--records", str(bad), "--repo", str(tmp_repo)])
    assert code == 2  # usage/input error, not a crash or a stale exit
    # Nothing written to stdout as a verdict payload.
    captured = capsys.readouterr()
    assert "verdicts" not in captured.out


def test_cli_missing_records_file_exit_2(tmp_repo: Path, tmp_path: Path):
    code = main(
        [
            "--records",
            str(tmp_path / "nope.json"),
            "--repo",
            str(tmp_repo),
        ]
    )
    assert code == 2


def test_cli_missing_repo_exit_2(tmp_path: Path):
    records = _write_records(
        tmp_path,
        [
            {"id": "a", "claim_text": "x", "anchors": []},
        ],
    )
    code = main(
        [
            "--records",
            str(records),
            "--repo",
            str(tmp_path / "no_such_repo"),
        ]
    )
    assert code == 2


def test_cli_malformed_record_contract_exit_2(tmp_repo: Path, tmp_path: Path):
    # A record missing 'id' is an input-contract error -> exit 2.
    records = _write_records(tmp_path, [{"claim_text": "x", "anchors": []}])
    code = main(["--records", str(records), "--repo", str(tmp_repo)])
    assert code == 2


def test_cli_require_env_fail_fast(tmp_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.delenv("MSV_MISSING", raising=False)
    out_path = tmp_path / "verdicts.json"
    records = _write_records(
        tmp_path,
        [
            {
                "id": "a",
                "claim_text": "x",
                "anchors": [{"path": "pkg/auth.py", "symbol": "refresh"}],
            },
        ],
    )
    code = main(
        [
            "--records",
            str(records),
            "--repo",
            str(tmp_repo),
            "--out",
            str(out_path),
            "--require-env",
            "MSV_MISSING",
        ]
    )
    assert code == 2
    # Fail-fast: env is checked before any verification, so no output is written.
    assert not out_path.exists()


def test_cli_require_env_present_proceeds(tmp_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MSV_PRESENT", "value")
    records = _write_records(
        tmp_path,
        [
            {
                "id": "a",
                "claim_text": "x",
                "anchors": [{"path": "pkg/auth.py", "symbol": "refresh"}],
            },
        ],
    )
    code = main(
        [
            "--records",
            str(records),
            "--repo",
            str(tmp_repo),
            "--require-env",
            "MSV_PRESENT",
        ]
    )
    assert code == 0
