# Contributing to Hivemind

Thanks for your interest in improving Hivemind — the single-tenant, stigmergic
episodic-memory server (an MCP server exposing the `hive_*` tools) for an agent fleet.
This guide covers the branch model, how to run the tests, and how to open a change. See
the [README](README.md) for architecture and a quickstart.

## Branch model

- **`development`** is the integration branch. **All work happens here** — either
  directly, or on a short-lived feature branch that you merge into `development`.
- **`master`** is the stable branch. It is protected: **direct pushes are rejected**,
  and it advances *only* through a pull request **from `development`**. A required CI
  check (`guard-master-source`) enforces that any pull request targeting `master`
  originates from this repository's `development` branch; anything else fails the check
  and cannot be merged.

The flow is always:

```
feature branch ──▶ development ──▶ (pull request) ──▶ master
```

Open your pull requests against **`development`** — never straight into `master`.

## Getting set up

Hivemind uses [uv](https://docs.astral.sh/uv/). Everything lives in this one repository —
including the census engines, which are first-party subpackages (`hive/matrix`,
`hive/combdrift`) of the `hive` distribution — so `uv sync` resolves every
dependency from the lockfile in one step:

```bash
uv sync --extra dev
```

## Running the tests — required before you submit

**Every pull request must have a green test suite before you open it.**

```bash
uv run pytest -m "not embed"      # fast suite — run this before every pull request
```

The `embed` marker gates tests that load the real Qwen3 embedding model (~1.2 GB). Run
the full suite when you touch recall or embedding behavior:

```bash
uv sync --extra dev --extra embed
uv run pytest
```

If your change adds behavior, add a test for it. If it fixes a bug, add a regression
test that fails before your change and passes after.

## Opening a change

1. Branch from `development`.
2. Make the change; keep it focused, and remove code your change makes dead.
3. Ensure `uv run pytest -m "not embed"` (and the full suite where relevant) is green.
4. Open a pull request **against `development`**, describing *what* changed and *why*.
5. A maintainer periodically promotes `development` to `master` through the guarded
   `development → master` pull request.

## Commit messages

Describe what the change accomplishes, in the imperative mood (e.g. "Guard master
against direct pushes"). Keep each commit focused and self-contained.

## License

By contributing, you agree that your contributions are licensed under the project's
[Apache-2.0](LICENSE) license.
