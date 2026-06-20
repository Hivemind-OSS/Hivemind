"""M12 — offline weight bake (BUILD-TIME only; never on the hot path).

`python -m hive.tools.bake_model --model Qwen/Qwen3-Embedding-0.6B --dest /opt/hf-cache/hub`
runs in the Dockerfile *builder* stage to materialize the sentence-transformer weights
into an image LAYER, so the runtime image (with `HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`)
can load the model with the network namespace removed (`--network none`) — the product's
"local & self-contained / no-network-on-hot-path" invariant made STRUCTURAL,
not documented.

`--dest` MUST be the HF hub cache dir the offline runtime resolves: with `HF_HOME=/opt/
hf-cache` and `cache_folder` unset, sentence-transformers/huggingface_hub look under
`$HF_HOME/hub` (= `/opt/hf-cache/hub`). Baking to the cache ROOT instead would write
`<dest>/models--…` one level above where the runtime looks, and the model would be
unfindable under `--network none`. Bake-dest and load-root must be the SAME directory.

Fail-fast is the whole contract: if the bake cannot complete (no network at build, bad
model id, partial download), it returns NON-ZERO so `docker build` FAILS — an image must
never ship without its weights and then discover the gap at first recall under
`--network none`. The heavy loader is injected (`loader=`) so the CLI wiring + the
fail-fast + the dest layout are unit-testable without the multi-hundred-MB download.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from typing import Callable, Optional

_log = logging.getLogger("hive.bake_model")

OK = 0
FAILED = 1


def _default_loader(model: str, dest: str) -> None:
    """Download the full sentence-transformers bundle into `dest`. Lazy import so this
    module loads without the (build-only) `embed` extra, and so `--help` is free."""
    from sentence_transformers import SentenceTransformer  # noqa: PLC0415 — build-only dep

    # cache_folder=dest puts the weights where HF_HOME points in the runtime stage.
    SentenceTransformer(model, cache_folder=dest)


def bake(model: str, dest: str, *, loader: Optional[Callable[[str, str], None]] = None) -> int:
    """Bake `model` into `dest`. Returns OK only if the loader completes AND `dest` is
    non-empty afterwards (a loader that silently no-ops must not pass as a successful
    bake). Any failure ⇒ FAILED + a logged error (the build then fails fast). // O(weights)."""
    loader = loader or _default_loader
    if not model:
        _log.error("bake.invalid_args reason=empty_model")
        return FAILED
    if not dest:
        _log.error("bake.invalid_args reason=empty_dest")
        return FAILED
    try:
        os.makedirs(dest, exist_ok=True)
        _log.info("bake.start model=%s dest=%s", model, dest)
        loader(model, dest)
    except Exception as exc:                            # noqa: BLE001 — surface as a non-zero exit
        _log.error("bake.failed model=%s dest=%s kind=%s", model, dest, type(exc).__name__)
        return FAILED
    if not _nonempty_dir(dest):
        # a loader that ran but wrote nothing ⇒ no weights baked ⇒ the offline guarantee
        # is a lie; refuse to let the build succeed (the no-network-on-hot-path invariant must be real).
        _log.error("bake.empty_dest model=%s dest=%s — no weights written", model, dest)
        return FAILED
    _log.info("bake.complete model=%s dest=%s", model, dest)
    return OK


def _nonempty_dir(path: str) -> bool:
    try:
        return os.path.isdir(path) and any(os.scandir(path))
    except OSError:
        return False


def main(argv: Optional[list[str]] = None, *,
         loader: Optional[Callable[[str, str], None]] = None) -> int:
    parser = argparse.ArgumentParser(prog="hive.tools.bake_model",
                                     description="Bake ST weights offline into an image layer.")
    parser.add_argument("--model", required=True, help="HF model id, e.g. Qwen/Qwen3-Embedding-0.6B")
    parser.add_argument("--dest", required=True, help="cache dir (must match runtime HF_HOME)")
    args = parser.parse_args(argv)
    return bake(args.model, args.dest, loader=loader)


if __name__ == "__main__":  # pragma: no cover — module entry (Dockerfile builder stage)
    raise SystemExit(main(sys.argv[1:]))
