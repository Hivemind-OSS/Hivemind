"""P1.13 / M12 — offline weight bake (build-time). Fail-fast is the whole contract:
a bake that cannot complete must return NON-ZERO so `docker build` fails, rather than
shipping a weightless image that only discovers the gap at first `--network none` recall.
The heavy loader is injected so this runs without the multi-hundred-MB download.
"""

from __future__ import annotations

import pytest

from hive.tools import bake_model as B


def test_bake_calls_loader_and_creates_dest(tmp_path):
    dest = tmp_path / "hf-cache"
    seen = {}

    def loader(model, d):
        seen["args"] = (model, d)
        (tmp_path / "hf-cache" / "model.bin").write_bytes(b"weights")

    rc = B.bake("BAAI/bge-small-en-v1.5", str(dest), loader=loader)
    assert rc == B.OK == 0
    assert seen["args"] == ("BAAI/bge-small-en-v1.5", str(dest))
    assert dest.is_dir() and any(dest.iterdir())


def test_bake_fails_when_loader_raises(tmp_path):
    def loader(model, d):
        raise RuntimeError("no network at build time")

    rc = B.bake("m", str(tmp_path / "dest"), loader=loader)
    assert rc == B.FAILED == 1


def test_bake_refuses_empty_dest(tmp_path):
    # a loader that runs but writes nothing ⇒ no weights ⇒ the offline guarantee is a lie;
    # the build MUST fail rather than succeed weightless.
    dest = tmp_path / "empty"

    def loader(model, d):
        return None  # no-op: wrote nothing

    rc = B.bake("m", str(dest), loader=loader)
    assert rc == B.FAILED


@pytest.mark.parametrize("model,dest", [("", "/tmp/x"), ("m", "")])
def test_bake_rejects_empty_args(model, dest):
    assert B.bake(model, dest, loader=lambda *_: None) == B.FAILED


def test_main_parses_args_and_invokes_bake(tmp_path):
    dest = tmp_path / "cache"
    seen = {}

    def loader(model, d):
        seen["args"] = (model, d)
        (dest / "f").parent.mkdir(parents=True, exist_ok=True)
        (dest / "f").write_text("w")

    rc = B.main(
        ["--model", "BAAI/bge-small-en-v1.5", "--dest", str(dest)], loader=loader
    )
    assert rc == B.OK
    assert seen["args"] == ("BAAI/bge-small-en-v1.5", str(dest))


def test_main_requires_model_and_dest():
    with pytest.raises(SystemExit):  # argparse: both flags required
        B.main(["--model", "m"], loader=lambda *_: None)
    with pytest.raises(SystemExit):
        B.main(["--dest", "/tmp/x"], loader=lambda *_: None)
