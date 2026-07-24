"""Chunk 7: safety invariants — never import/execute the target; never write to it.

These exercise the whole pipeline (library + CLI) against repos engineered to
detonate on import, proving the verifier only ever AST-parses.
"""

from __future__ import annotations

import json
from pathlib import Path

from hive.combdrift.cli import main
from hive.combdrift.resolution import resolve_anchor
from hive.combdrift.types import Anchor
from hive.combdrift.verdict import verify_records

from tests.combdrift.conftest import snapshot_tree


def test_module_top_level_sys_exit_is_safe(tmp_repo: Path, make_record):
    # pkg/explode.py calls sys.exit(1) at module top level. If the resolver
    # imported it, this process would terminate. AST parse must be unaffected.
    rec = make_record("boom", ("pkg/explode.py", "handler"))
    verdicts, _summary = verify_records(str(tmp_repo), [rec])
    assert verdicts[0].verdict == "current"
    assert verdicts[0].anchors[0].found is True


def test_resolver_uses_ast_not_import(tmp_repo: Path):
    # pkg/nameerror.py raises NameError at import time. Resolving its symbol via
    # AST must still succeed, proving no import path is taken.
    res = resolve_anchor(str(tmp_repo), Anchor("pkg/nameerror.py", "safe"))
    assert res.found is True
    assert res.reason == "ok"


def test_repo_mtimes_unchanged_after_run(tmp_repo: Path, tmp_path: Path):
    before = snapshot_tree(tmp_repo)

    records = [
        {
            "id": "a",
            "claim_text": "x",
            "anchors": [{"path": "pkg/auth.py", "symbol": "refresh"}],
        },
        {
            "id": "b",
            "claim_text": "y",
            "anchors": [{"path": "pkg/broken.py", "symbol": "oops"}],
        },
        {
            "id": "c",
            "claim_text": "z",
            "anchors": [{"path": "pkg/explode.py", "symbol": "handler"}],
        },
        {
            "id": "d",
            "claim_text": "w",
            "anchors": [{"path": "pkg/missing.py", "symbol": "x"}],
        },
    ]
    records_file = tmp_path / "records.json"
    records_file.write_text(json.dumps(records), encoding="utf-8")
    out_file = tmp_path / "out.json"

    main(
        [
            "--records",
            str(records_file),
            "--repo",
            str(tmp_repo),
            "--out",
            str(out_file),
        ]
    )

    after = snapshot_tree(tmp_repo)
    assert after == before  # no repo file created, deleted, or modified


def test_no_new_files_created_in_repo(tmp_repo: Path):
    before = set(snapshot_tree(tmp_repo).keys())
    verify_records(str(tmp_repo), [])
    resolve_anchor(str(tmp_repo), Anchor("pkg/auth.py", "refresh"))
    resolve_anchor(str(tmp_repo), Anchor("pkg/broken.py", "x"))
    after = set(snapshot_tree(tmp_repo).keys())
    assert after == before  # e.g. no __pycache__ from an accidental import


def test_follow_reexport_is_read_only(make_repo):
    # Following a re-export reads the target as data only: no file in the repo
    # (importer or followed source) may be created, deleted, or modified.
    repo = make_repo(
        {
            "pkg/__init__.py": "",
            "pkg/api.py": "from .core import parse\n",
            "pkg/core.py": "def parse(x):\n    return x\n",
            "src/index.ts": "export { Button } from './Button';\n",
            "src/Button.tsx": "export function Button() {}\n",
        }
    )
    before = snapshot_tree(Path(repo))
    for anchor in [
        Anchor("pkg/api.py", "parse"),
        Anchor("src/index.ts", "Button"),
    ]:
        resolve_anchor(repo, anchor)
    assert snapshot_tree(Path(repo)) == before


def test_schema_sources_never_written_or_executed(make_repo, tmp_path: Path):
    # A .sql containing COPY ... FROM PROGRAM is AST-parsed, never run: if combdrift
    # executed it the sentinel would appear. Schema files are read as data only.
    sentinel = tmp_path / "combdrift_executed_sentinel"
    repo = make_repo(
        {
            "db/schema.sql": (
                f"CREATE TABLE t (id int);\nCOPY t FROM PROGRAM 'touch {sentinel}';\n"
            ),
            "db/doc.schema.json": '{"properties": {"email": {}}, "additionalProperties": false}\n',
        }
    )
    before = snapshot_tree(Path(repo))
    for anchor in [
        Anchor("db/schema.sql", "t"),
        Anchor("db/schema.sql", "t.id"),
        Anchor("db/schema.sql", "t.gone"),
        Anchor("db/doc.schema.json", "doc.email"),
    ]:
        resolve_anchor(repo, anchor)
    assert not sentinel.exists()  # COPY ... FROM PROGRAM parsed, never executed
    assert snapshot_tree(Path(repo)) == before  # no schema file created or modified


def test_ts_resolution_is_read_only(tmp_ts_repo: Path):
    # tree-sitter parses source as data; resolving JS/TS must not create,
    # delete, or modify any file in the target repo.
    before = snapshot_tree(tmp_ts_repo)
    for anchor in [
        Anchor("src/auth.ts", "refresh"),
        Anchor("src/component.tsx", "Button"),
        Anchor("src/util.js", "helper"),
        Anchor("src/broken.ts", "good"),
        Anchor("types/api.d.ts", "fetchUser"),
        Anchor("src/data.json", None),
    ]:
        resolve_anchor(str(tmp_ts_repo), anchor)
    assert snapshot_tree(tmp_ts_repo) == before
