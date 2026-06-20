"""P1.13 / M12 — the Dockerfile contract (parsed; contracts that cannot lie).

Multi-stage, non-root FINAL user, weights baked offline, the build tree excluded from the
runtime image, the healthcheck/entrypoint wired to the right modules. These are structural
facts of the Dockerfile text — verifiable without a daemon (the live build is in
test_container_live.py, skip-guarded).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    # build-essential / git / apt installs belong ONLY to the builder stage (no bloat,
    # smaller attack surface in the runtime image).
    builder, runtime = _stages()["builder"], _stages()["runtime"]
    assert "build-essential" in builder
    assert "build-essential" not in runtime
    assert "apt-get install" not in runtime


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
