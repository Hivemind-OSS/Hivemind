"""Regenerate the matrix per-language golden snapshots from hive.matrix output.

For each language: extract with hive.matrix, normalize, and write the result as
the committed golden under ``tests/matrix/golden/``. This re-blesses the CURRENT
engine output as the specification, so review the resulting diff before
committing — an unintended extraction change would otherwise silently become the
new golden.

The SQL golden needs the ``tree-sitter-sql`` grammar (a dev/test-only dependency
the runtime image deliberately excludes); regenerate under an env that provides
it, else ``sql.json`` is rewritten empty:

    uv run --with "tree-sitter-sql>=0.3" python scripts/regen_matrix_goldens.py

Run from the repo root.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import hive.matrix as matrix  # noqa: E402
from tests.matrix.fixtures_manifest import GOLDEN_ROOT, LANG_FILES, lang_paths  # noqa: E402
from tests.matrix.normalize import normalize_for_golden  # noqa: E402


def main() -> int:
    GOLDEN_ROOT.mkdir(parents=True, exist_ok=True)
    for lang in sorted(LANG_FILES):
        golden = normalize_for_golden(matrix.extract(lang_paths(lang)))
        out = GOLDEN_ROOT / f"{lang}.json"
        out.write_text(json.dumps(golden, indent=2, sort_keys=True) + "\n")
        print(
            f"[ok]   {lang}: golden written ({len(golden['nodes'])} nodes, {len(golden['edges'])} edges)"
        )
    print(
        "\nAll goldens regenerated from hive.matrix output — review the diff before committing."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
