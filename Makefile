# Canonical mechanical gate. `make check` is the single verb that must pass before any
# change is done: format check, lint, strict typecheck, test suite (minus the opt-in
# `embed` tier — see `test`) — in that order, non-zero exit on the first leg that fails. Legs run through `uv run --extra dev` so a
# fresh clone needs nothing beyond uv itself (the dev extra carries ruff/mypy/pytest, and the
# lockfile resolves the first-party engines' third-party deps — no wheelhouse, no package index).

UV ?= uv
RUN := $(UV) run --extra dev

.PHONY: check format lint typecheck test check-embed

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

typecheck:
	$(RUN) mypy hive/ --strict

# The embed tier (loads the real ~1.2 GB model; needs the `embed` extra) is opt-in,
# per the marker's own doc in pyproject.toml: run it with `uv run --extra embed pytest -m embed`.
test:
	$(RUN) pytest -m "not embed"

# The opt-in tier, as one un-mistypeable verb. Deliberately NOT part of `check`: a
# 1.2 GB model load in every gate run would fail on any machine without the extra.
# The assertions that need it are the ones whose subject IS what the real embedder
# does to a real corpus (recall quality, the served-set relevance distribution).
check-embed:
	$(UV) run --extra embed pytest -m embed
