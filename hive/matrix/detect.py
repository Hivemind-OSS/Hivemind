"""C4: discovery + the incremental content-hash manifest (AST-only).

The manifest records, per source file, its last-seen ``mtime`` and a single
content ``hash`` (md5). On the next run :func:`detect_incremental` re-derives the
change set without re-extracting anything unchanged:

* **changed** — the file has no stored hash, OR its mtime moved *and* its md5 no
  longer matches the stored hash. The mtime check is a fast path: an unchanged
  mtime means unchanged content, so the hash is never recomputed.
* **deleted** — a manifest key that discovery no longer finds on disk.
* **prune_sources** — the **deleted** set: the files whose prior graph
  contribution must be removed outright. Changed files are *not* pruned — the
  merge replaces them by ``source_file`` from the re-extraction, so pruning them
  would delete the fresh nodes that were just re-added.

matrix is AST-only, so a single content ``hash`` is the whole story — there is no
second semantic pass to reconcile. Discovery is delegated to
:func:`matrix.extract.collect_files`, the same walker the full build uses, so
incremental and full runs always agree on which files are sources.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .extract import collect_files
from .paths import default_manifest

__all__ = [
    "load_manifest",
    "save_manifest",
    "detect_incremental",
]


def _md5_file(path: Path) -> str:
    """MD5 of file contents streamed in 64KB chunks — for change detection only."""
    h = hashlib.md5(usedforsecurity=False)
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _stat_and_hash(path: Path) -> tuple[float, str] | None:
    """Stat + MD5 a single file; returns None on OSError (e.g. deleted mid-run)."""
    try:
        return path.stat().st_mtime, _md5_file(path)
    except OSError:
        return None


def load_manifest(manifest_path: str | Path = "") -> dict[str, dict[str, Any]]:
    """Load the manifest from a previous run. Returns ``{}`` on any error.

    Keys are absolute file paths; values are ``{"mtime": float, "hash": str}``.
    """
    manifest_path = Path(manifest_path or default_manifest())
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def save_manifest(files: list[Path], manifest_path: str | Path = "") -> None:
    """Write current mtimes + content hashes for the given files.

    Each file is keyed by its absolute path. Files that vanish between discovery
    and write (deleted mid-run) are skipped rather than recorded with an empty
    hash, so the next run sees them as genuinely gone.
    """
    manifest_path = Path(manifest_path or default_manifest())
    manifest: dict[str, dict[str, Any]] = {}
    for f in files:
        f = Path(f).resolve()
        sh = _stat_and_hash(f)
        if sh is None:
            continue
        mtime, h = sh
        manifest[str(f)] = {"mtime": mtime, "hash": h}
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def detect_incremental(root: Path, manifest_path: str | Path = "") -> dict[str, Any]:
    """Return the change set under ``root`` since the last :func:`save_manifest`.

    Returns ``{"changed": [...], "deleted": [...], "prune_sources": [...],
    "discovered": [...]}`` — all absolute ``Path`` lists. ``changed`` includes
    brand-new files (no stored hash); ``prune_sources`` is the **deleted** set,
    the contribution :func:`matrix.assemble.build_merge` must drop outright (a
    changed file is *replaced* from its re-extraction, never pruned).
    """
    manifest = load_manifest(manifest_path)
    discovered = [Path(p).resolve() for p in collect_files(Path(root))]

    changed: list[Path] = []
    for f in discovered:
        stored = manifest.get(str(f))
        stored_hash = stored.get("hash", "") if isinstance(stored, dict) else ""
        if not stored_hash:
            changed.append(f)
            continue
        try:
            current_mtime = f.stat().st_mtime
        except OSError:
            current_mtime = 0.0
        stored_mtime = stored.get("mtime") if isinstance(stored, dict) else None
        if not isinstance(stored_mtime, (int, float)):
            stored_mtime = None
        # mtime fast path: an unchanged mtime means unchanged content — skip the
        # hash. Only when mtime moved do we pay for md5 to confirm a real change.
        if stored_mtime is None or current_mtime != stored_mtime:
            if _md5_file(f) != stored_hash:
                changed.append(f)

    discovered_keys = {str(f) for f in discovered}
    deleted = [Path(k) for k in manifest if k not in discovered_keys]

    # Only deleted files are pruned. A changed file is replaced by source_file
    # from its re-extraction inside build_merge; pruning it as well would remove
    # the freshly re-extracted nodes that were just merged in.
    prune_sources = list(deleted)

    return {
        "changed": changed,
        "deleted": deleted,
        "prune_sources": prune_sources,
        "discovered": discovered,
    }
