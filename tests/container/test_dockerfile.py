"""P1.13 / M12 — the Dockerfile contract (parsed; contracts that cannot lie).

Multi-stage, non-root FINAL user, weights baked offline, the build tree excluded from the
runtime image, the healthcheck/entrypoint wired to the right modules. These are structural
facts of the Dockerfile text — verifiable without a daemon (the live build is in
test_container_live.py, skip-guarded).
"""
from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "Dockerfile"


def _text() -> str:
    return _DOCKERFILE.read_text()


def _stages() -> dict[str, str]:
    """Split the Dockerfile into {stage_name: body} by `FROM ... AS <name>`."""
    text = _text()
    parts = re.split(r"(?im)^FROM\s+\S+\s+AS\s+(\w+)\s*$", text)
    # parts = [preamble, name1, body1, name2, body2, ...]
    out: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        out[parts[i].lower()] = parts[i + 1]
    return out


def test_dockerfile_exists():
    assert _DOCKERFILE.is_file()


def test_multistage_builder_and_runtime():
    stages = _stages()
    assert "builder" in stages and "runtime" in stages


def test_final_user_is_non_root():
    # the LAST USER directive governs the running container; it must be the non-root hive.
    users = re.findall(r"(?im)^USER\s+(\S+)\s*$", _text())
    assert users, "no USER directive — image would run as root"
    assert users[-1] == "hive"
    assert users[-1] not in ("root", "0")


def test_runtime_entrypoint_is_entrypoint_module():
    assert "hive.tools.entrypoint" in _stages()["runtime"]
    assert re.search(r"(?im)^ENTRYPOINT\b.*hive\.tools\.entrypoint", _text())


def test_runtime_healthcheck_runs_healthcheck_module():
    assert re.search(r"(?is)HEALTHCHECK\b.*hive\.tools\.healthcheck", _text())


def test_weights_baked_in_builder():
    builder = _stages()["builder"]
    assert "hive.tools.bake_model" in builder
    assert "Qwen/Qwen3-Embedding-0.6B" in builder


def test_model_baked_before_volatile_source_copy():
    # BUILD-CACHE invariant (BUG-014): the ~1.2 GB bake must precede the wholesale `COPY hive/ ./hive/`,
    # so editing any hive/ source never invalidates — and re-downloads — the weights layer. The bake
    # depends only on the three self-contained files (the two empty __init__.py + bake_model.py),
    # which are copied BEFORE it; the full source is copied AFTER.
    builder = _stages()["builder"]
    bake_at = builder.index("hive.tools.bake_model")          # the RUN (dotted module path)
    wholesale = re.search(r"(?im)^COPY\s+hive/\s+\./hive/\s*$", builder)
    assert wholesale, "no wholesale `COPY hive/ ./hive/` in builder"
    assert bake_at < wholesale.start(), "the model bake must precede `COPY hive/` or the cache is defeated"
    for f in ("hive/__init__.py", "hive/tools/__init__.py", "hive/tools/bake_model.py"):
        assert builder.index(f"COPY {f}") < bake_at, f"{f} must be copied before the bake"


def test_bake_dest_matches_runtime_hub_resolution():
    # bake --dest and the offline runtime's resolution must agree on ONE directory: with
    # HF_HOME=X and cache_folder unset, ST/huggingface_hub resolve X/hub — so the bake MUST
    # write to $HF_HOME/hub, else the model is unfindable under --network none.
    text = _text()
    hf_home = re.search(r"HF_HOME=(\S+)", _stages()["runtime"]).group(1)
    dest = re.search(r"bake_model\s+--model\s+\S+\s+--dest\s+(\S+)", text).group(1)
    assert dest == hf_home.rstrip("/") + "/hub", f"bake dest {dest!r} != {hf_home}/hub"


def test_runtime_is_hard_offline():
    runtime = _stages()["runtime"]
    assert re.search(r"HF_HUB_OFFLINE=1", runtime)
    assert re.search(r"TRANSFORMERS_OFFLINE=1", runtime)


def test_runtime_excludes_build_toolchain():
    # build-essential belongs ONLY to the builder stage (no bloat, smaller attack
    # surface). The runtime deliberately carries git — the sync mirror and the census
    # diff/worktree engine shell to it — and NOTHING else from apt: every runtime
    # apt-get install line must name exactly {git}.
    builder, runtime = _stages()["builder"], _stages()["runtime"]
    assert "build-essential" in builder
    assert "build-essential" not in runtime
    installs = re.findall(r"apt-get install\s+((?:-\S+\s+)*)([^&\n]+)", runtime)
    assert installs, "runtime must apt-get install git (the sync subsystem shells to it)"
    for _flags, packages in installs:
        names = [token for token in packages.split() if token != "\\"]
        assert names == ["git"], f"runtime apt installs must be exactly ['git'], got {names}"


def test_runtime_has_git_and_uv_for_sync():
    # The sync subsystem's runtime tools: git (mirror + census worktrees) and the uv
    # binary (candidate-eval `uv sync --frozen`), copied from the official uv image.
    runtime = _stages()["runtime"]
    assert re.search(r"apt-get install[^\n]*\bgit\b", runtime)
    assert re.search(
        r"(?im)^COPY\s+--from=ghcr\.io/astral-sh/uv:\S+\s+/uv\s+/usr/local/bin/uv\s*$",
        runtime,
    )


def test_builder_installs_sync_extra_and_pytest():
    # The server-side census engines ride `.[sync]` (hive-edge CLI + comb-drift +
    # matrix) and the candidate-eval tier needs pytest in the venv the runtime copies.
    builder = _stages()["builder"]
    assert re.search(r"pip install[^\n]*\.\[embed,sync\][^\n]*\bpytest\b", builder)


def test_pyright_bake_is_best_effort_only():
    # pyright's node fetch may flake: the bake must be fail-open (`|| true`) so a
    # flaky fetch NEVER fails the image build — the typecheck line abstains honestly
    # instead. The cache dir is created unconditionally so the runtime COPY of it
    # cannot break, and the runtime copies it into the hive user's home.
    builder, runtime = _stages()["builder"], _stages()["runtime"]
    bake = re.search(r"pyright --version\s*\|\|\s*true", builder)
    assert bake, "the pyright warm-up must be guarded fail-open (`|| true`)"
    assert re.search(r"pip install[^\n]*\bpyright\b", builder)
    assert re.search(r"mkdir -p /root/\.cache", builder)
    assert re.search(r"(?im)^COPY\s+--from=builder\s+/root/\.cache\s+/home/hive/\.cache\s*$", runtime)


def test_sync_state_dir_created_and_owned_by_hive():
    runtime = _stages()["runtime"]
    assert re.search(r"mkdir -p[^\n]*/data/sync\b", runtime)
    assert re.search(r"chown -R hive:hive[^\n]*/data\b", runtime)
    assert re.search(r"chown -R hive:hive[^\n]*/home/hive/\.cache", runtime)


def test_runtime_does_not_copy_source_tree_wholesale():
    # the runtime gets the INSTALLED package from the builder venv, not a raw `COPY . .`
    # of the repo (which would drag the test tree and .git into a layer).
    runtime = _stages()["runtime"]
    assert "COPY --from=builder" in runtime
    assert not re.search(r"(?im)^COPY\s+\.\s+\.", runtime)
    assert not re.search(r"(?im)^COPY\s+hive/\s", runtime)


def test_embed_extra_installed_for_bake_and_warm():
    # the bake step and the runtime embedder both need sentence-transformers; the builder
    # must install the `embed` extra (a bare `pip install .` cannot bake or warm the model).
    builder = _stages()["builder"]
    assert re.search(r"pip install[^\n]*\.\[embed\]", builder)


def test_volume_and_data_dir_owned_by_hive():
    text = _text()
    assert re.search(r'VOLUME\s+\["/data"\]', text)
    assert "chown -R hive:hive /data" in text
