"""Shared fixtures: a throwaway Python repo on disk and a terse Record builder."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from hive.combdrift.types import Anchor, Record


# A small but representative repo. Keys are repo-relative paths; values are file text.
# Covers: a module-level function, a module-level class with a method, an async
# function, a file with a top-level side effect, and a file with a syntax error.
_REPO_FILES: dict[str, str] = {
    "pkg/__init__.py": "",
    "pkg/auth.py": (
        "TOKEN_TTL = 3600\n"
        "\n"
        "\n"
        "def refresh(token):\n"
        "    return token\n"
        "\n"
        "\n"
        "async def revoke(token):\n"
        "    return None\n"
        "\n"
        "\n"
        "class Session:\n"
        "    def login(self, user):\n"
        "        return user\n"
        "\n"
        "    async def logout(self):\n"
        "        return None\n"
    ),
    "pkg/parser.py": ("def parse(a, b, c):\n    return (a, b, c)\n"),
    "pkg/broken.py": (
        "def oops(:\n"  # deliberate syntax error
        "    pass\n"
    ),
    "pkg/explode.py": (
        "import sys\n"
        "sys.exit(1)\n"  # top-level side effect; must never execute
        "\n"
        "\n"
        "def handler():\n"
        "    return 'ok'\n"
    ),
    "pkg/nameerror.py": (
        "undefined_name_at_import_time\n"  # would raise NameError if imported
        "\n"
        "\n"
        "def safe():\n"
        "    return 1\n"
    ),
}


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """Materialize the sample repo under tmp_path; return the repo root Path."""
    root = tmp_path / "repo"
    for rel, text in _REPO_FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return root


# A JS/TS repo exercising every recognized declaration form, an error-recovery
# case, a .d.ts ambient file, an unsupported file type, and one Python file (to
# prove a single record may anchor across languages). Line numbers are asserted
# by tests, so edits here must keep them in sync.
_TS_REPO_FILES: dict[str, str] = {
    "src/auth.ts": (
        "export function refresh(token: string): string {\n"  # 1
        "  return token;\n"
        "}\n"
        "\n"
        "export const revoke = async (token: string): Promise<void> => {\n"  # 5
        "  return;\n"
        "};\n"
        "\n"
        "export class Session {\n"  # 9
        "  login(user: string): string {\n"  # 10
        "    return user;\n"
        "  }\n"
        "\n"
        "  async logout(): Promise<void> {}\n"  # 14
        "}\n"
        "\n"
        "export const MAX_AGE = 3600;\n"  # 17  (data const -> indirect)
        "export interface User {\n"  # 18  (interface -> indirect)
        "  id: string;\n"
        "}\n"
        "export type Id = string;\n"  # 21  (type alias -> indirect)
        "export enum Role {\n"  # 22  (enum -> indirect)
        "  Admin,\n"
        "  Guest,\n"
        "}\n"
    ),
    "src/component.tsx": (
        "export const Button = (props: { label: string }) => {\n"  # 1
        "  return <button>{props.label}</button>;\n"
        "};\n"
        "\n"
        "export default function App() {\n"  # 5
        '  return <Button label="x" />;\n'
        "}\n"
    ),
    "src/util.js": (
        "function helper() {\n"  # 1
        "  return 1;\n"
        "}\n"
        "const arrow = () => helper();\n"  # 4
        "class Widget {\n"  # 5
        "  render() {\n"  # 6
        "    return null;\n"
        "  }\n"
        "}\n"
    ),
    "src/broken.ts": (
        "export function good() {\n"  # 1  (clean, before the error)
        "  return 1;\n"
        "}\n"
        "function bad( {\n"  # 4  deliberate syntax error
        "export function alsoMaybe() {}\n"
    ),
    "types/api.d.ts": (
        "export declare function fetchUser(id: string): Promise<unknown>;\n"  # 1
        "declare class Client {\n"  # 2
        "  send(): void;\n"  # 3
        "}\n"
    ),
    "src/legacy.py": (
        "def old():\n"  # 1
        "    return 1\n"
    ),
    "src/data.json": '{"k": 1}\n',  # unsupported file type
}


@pytest.fixture
def tmp_ts_repo(tmp_path: Path) -> Path:
    """Materialize the JS/TS sample repo under tmp_path; return the repo root."""
    root = tmp_path / "ts_repo"
    for rel, text in _TS_REPO_FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return root


# A repo of declared-schema sources: a consolidated SQL DDL file exercising a
# table, a SELECT * view, and an ALTER-added column; a closed Mongo $jsonSchema
# document; and a malformed .sql. Line numbers are asserted, so keep them in sync.
_SCHEMA_REPO_FILES: dict[str, str] = {
    "db/schema.sql": (
        "CREATE TABLE users (\n"  # 1
        "  id integer PRIMARY KEY,\n"  # 2
        "  email text NOT NULL\n"  # 3
        ");\n"  # 4
        "\n"  # 5
        "CREATE VIEW active_users AS SELECT * FROM users;\n"  # 6
        "\n"  # 7
        "ALTER TABLE users ADD COLUMN created_at timestamptz;\n"  # 8
    ),
    "db/users.schema.json": (
        '{"$jsonSchema": {"bsonType": "object",\n'
        '  "properties": {"_id": {}, "email": {}},\n'
        '  "additionalProperties": false}}\n'
    ),
    "db/broken.sql": "CREATE TABLE (\n",  # malformed -> ParseError -> parse_error
}


@pytest.fixture
def tmp_schema_repo(tmp_path: Path) -> Path:
    """Materialize the declared-schema sample repo; return the repo root Path."""
    root = tmp_path / "schema_repo"
    for rel, text in _SCHEMA_REPO_FILES.items():
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def make_repo(tmp_path: Path):
    """Return a builder: make_repo({rel_path: text, ...}) -> repo root str.

    Materializes an arbitrary file layout under a fresh subdirectory, so each
    re-export-following test can specify exactly the source/target files it
    needs (a target with or without the symbol, a submodule, a second hop).
    """

    def _build(files: dict[str, str], name: str = "repo") -> str:
        root = tmp_path / name
        for rel, text in files.items():
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        root.mkdir(parents=True, exist_ok=True)  # an empty layout still has a root
        return str(root)

    return _build


def snapshot_tree(root: Path) -> dict[str, tuple[float, int]]:
    """Map every file under root to (mtime_ns, size) for tamper detection."""
    snap: dict[str, tuple[float, int]] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            st = p.stat()
            snap[str(p)] = (st.st_mtime_ns, st.st_size)
    return snap


@pytest.fixture
def make_record():
    """Return a builder: make_record(id, *anchor_pairs, claim_text=..., commit=...).

    Each anchor_pair is (path,), (path, symbol), or (path, symbol, fingerprint).
    """

    def _build(
        record_id: str,
        *anchor_pairs: tuple,
        claim_text: str = "a claim",
        commit: str | None = None,
    ) -> Record:
        anchors = tuple(
            Anchor(
                path=pair[0],
                symbol=(pair[1] if len(pair) > 1 else None),
                fingerprint=(pair[2] if len(pair) > 2 else None),
            )
            for pair in anchor_pairs
        )
        return Record(
            id=record_id,
            claim_text=claim_text,
            anchors=anchors,
            recorded_at_commit=commit,
        )

    return _build
