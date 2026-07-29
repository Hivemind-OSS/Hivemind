# Canonical mechanical gate. `make check` is the single verb that must pass before any
# change is done: format check, lint, strict typecheck, test suite (minus the opt-in
# `embed` tier — see `test`) — in that order, non-zero exit on the first leg that fails. Legs run through `uv run --extra dev` so a
# fresh clone needs nothing beyond uv itself (the dev extra carries ruff/mypy/pytest, and the
# lockfile resolves the first-party engines' third-party deps — no wheelhouse, no package index).

UV ?= uv
RUN := $(UV) run --extra dev

# The harness under harnesses/ is TypeScript, so the typecheck and test legs each
# grow one Node leg. These deliberately do NOT take $(RUN): they are not Python.
# `tsc` is not on PATH anywhere — it resolves out of harnesses/node_modules, which
# is gitignored, so `harness-deps` bootstraps a fresh clone rather than failing
# with a bare "tsc: not found". Node >= 23.6 is required for both legs (the
# harness runs .ts directly, with no build step).
NPM ?= npm
HARNESS := --prefix harnesses

.PHONY: check format lint typecheck test check-embed harness-deps

# The four legs run in this fixed order even under `make -j` (cheap legs fail first).
check:
	$(MAKE) format
	$(MAKE) lint
	$(MAKE) typecheck
	$(MAKE) test

format:
	$(RUN) ruff format --check .

lint:
	$(RUN) ruff check .

# The dev gate's only package. Gitignored, so a fresh clone has to fetch it before
# the strict TypeScript leg can run; the shipped plugin itself needs none of this.
harness-deps:
	$(NPM) $(HARNESS) ci

typecheck: harness-deps
	$(RUN) mypy hive/ --strict
	$(NPM) $(HARNESS) exec -- tsc -p harnesses --noEmit

# The embed tier (loads the real ~1.2 GB model; needs the `embed` extra) is opt-in,
# per the marker's own doc in pyproject.toml: run it with `uv run --extra embed pytest -m embed`.
# The harness's live tier self-skips without HIVE_LOOP_LIVE, so it needs no marker.
# The glob is quoted for node, not the shell: this node's --test does not discover a
# bare directory.
test:
	$(RUN) pytest -m "not embed"
	node --test "harnesses/test/*.test.ts"

# The opt-in tier, as one un-mistypeable verb. Deliberately NOT part of `check`: a
# 1.2 GB model load in every gate run would fail on any machine without the extra.
# The assertions that need it are the ones whose subject IS what the real embedder
# does to a real corpus (recall quality, the served-set relevance distribution).
check-embed:
	$(UV) run --extra embed pytest -m embed
