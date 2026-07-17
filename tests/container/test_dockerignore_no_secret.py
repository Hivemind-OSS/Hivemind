"""P1.13 / M12 — the build-layer secret floor (no-secret-in-any-layer infra), made structural.

Two halves: (1) `.dockerignore` excludes the sensitive trees so a stray credential, the
git history, a local venv, or the test/doc trees can never enter the build context; (2) a
scan of the files that WOULD become layers (pyproject.toml + hive/ + vendor/, the exact
COPY set) finds no AWS/OpenAI/GitHub credential shapes. The full layer-tar scan of a built image is
in test_container_live.py (skip-guarded); this is the daemon-free static guarantee.
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERIGNORE = _ROOT / ".dockerignore"

# credential SHAPES (length-bounded so pattern NAMES in source comments don't false-match).
# The sk- branch allows hyphens so it catches the post-2023 OpenAI/Anthropic formats
# (sk-proj-…, sk-svcacct-…, sk-ant-api03-…), not only the legacy hyphen-free token; plus
# GitHub fine-grained PATs (github_pat_…) and Google API keys (AIza…). See the positive
# test below — the matcher's own coverage is pinned so it cannot silently regress to a no-op.
_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9-]{20,}|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}"
    r"|github_pat_[A-Za-z0-9_]{50,}|xox[baprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{35})")

# modern credential shapes the scanner MUST flag (regression guard against a too-narrow regex)
_KNOWN_KEY_FAKES = [
    "sk-proj-Abcd1234Efgh5678Ijkl9012Mnop3456qrst",
    "sk-ant-api03-Abc12345Def67890Ghi12345Jkl67890",
    "github_pat_11ABCDEFG0abcdefghijKL_mnopqrstuvwxyz0123456789ABCDEFGHIJKLMNOP",
    "AKIAIOSFODNN7EXAMPLE", "ghp_" + "a" * 36, "AIza" + "A" * 35,
]

# exactly the trees the Dockerfile COPYs into a layer
_CONTEXT = [_ROOT / "pyproject.toml", _ROOT / "hive", _ROOT / "vendor"]
_REQUIRED_IGNORES = [".git", "__pycache__", ".env", "*.pem", "*.key",
                     "tests/", "docs/", "*.db"]


def test_dockerignore_exists():
    assert _DOCKERIGNORE.is_file()


def test_dockerignore_excludes_sensitive_trees():
    body = _DOCKERIGNORE.read_text()
    missing = [pat for pat in _REQUIRED_IGNORES if pat not in body]
    assert not missing, f".dockerignore missing required excludes: {missing}"


def test_dockerignore_secret_patterns_are_depth_agnostic():
    # a bare `*.key`/`.env` matches only the context ROOT; the COPY hive/ ./hive/ would still
    # bake a nested credential. The recursive **/ sibling must be present for each.
    body = _DOCKERIGNORE.read_text()
    required_recursive = ["**/.env", "**/*.pem", "**/*.key", "**/*.p12",
                          "**/id_rsa*", "**/*.secret", "**/*.db"]
    missing = [pat for pat in required_recursive if pat not in body]
    assert not missing, f".dockerignore missing recursive secret excludes: {missing}"


def _iter_context_files():
    for entry in _CONTEXT:
        if entry.is_file():
            yield entry
        elif entry.is_dir():
            for p in entry.rglob("*"):
                if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                    yield p


def test_no_secret_shape_in_build_context():
    offenders = []
    for p in _iter_context_files():
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if _SECRET_RE.search(text):
            offenders.append(str(p.relative_to(_ROOT)))
    assert not offenders, f"credential-shaped string in build context: {offenders}"


def test_secret_regex_matches_modern_key_shapes():
    # the matcher must catch post-2023 OpenAI/Anthropic/GitHub/Google formats, not just the
    # legacy hyphen-free sk- token — else "no secret in any layer" passes vacuously.
    misses = [k for k in _KNOWN_KEY_FAKES if not _SECRET_RE.search(k)]
    assert not misses, f"secret regex fails to flag modern credential shapes: {misses}"


def test_context_set_matches_dockerfile_copy():
    # guard the assumption above: the Dockerfile COPYs pyproject + hive/ + vendor/wheels/
    # and nothing else source-wise into the builder (so scanning that set == scanning the
    # layered source).
    df = (_ROOT / "Dockerfile").read_text()
    assert re.search(r"(?im)^COPY\s+pyproject\.toml", df)
    assert re.search(r"(?im)^COPY\s+hive/\s", df)
    assert re.search(r"(?im)^COPY\s+vendor/wheels/\s", df)
