# engine_parity fixtures (CT-16)

Committed input trees + `goldens.json` for the engine-parity contract
(`tests/contract/test_engine_parity.py`). They are the decisive anti-fork
evidence for the engine absorption: the fingerprint tokens and verify verdicts
here were minted from the pre-move **0.9.0 `hive-edge`** console script, and
CT-16 asserts the post-move in-repo engines (`hive.matrix` / `hive.combdrift` /
`hive.edge`, driven via `python -m hive.edge.cli`) reproduce them field-for-field
(tokens byte-exact).

## Trees

- `single_root/` — all source under one top-level dir (`src/`), so matrix infers
  its extraction root as `src` and its `source_file` dialect drops that prefix
  while a Hivemind anchor stays repo-root-relative. This is the BUG-035/038
  offset-bridge corner; `src/core.py:compute` mints a two-member subgraph fp
  (`compute` + its callee `helper`), proving the bridge re-roots every member.
- `multi_root/` — source across two top-level dirs (`alpha/`, `beta/`), so the
  corpus root IS the repo root (offset `""`, the "dialects agree" case);
  `beta.b` calls `alpha.a.run` across the boundary.
- `sig_change/before|after/` — `transform` gains a second required positional
  parameter (a breaking, interface-changing edit; a body-only change would be
  fingerprint-invisible, BUG-048). The BEFORE fp verified against the AFTER tree
  reads `stale/signature_changed` with a delta.

## goldens.json

`{_meta, scenarios}`. Each scenario carries the provenance `argv` (repo path
masked as `<REPO>`), the `exit` code, and the parsed `stdout` map. Scenario
inputs (a stored memory's `--fp` / `--subgraph-fp`) are sourced from the pinned
mint scenarios, so every verify scenario is independently checkable. The verify
matrix covers: `current` (fresh), `stale/signature_changed` (+delta),
`stale/symbol_missing`, `unverifiable/not_a_code_anchor` (BUG-022 prose),
`unverifiable/fingerprint_version_mismatch` (BUG-037 future-version fp), and
`branch_scoped/off_branch`.

Comparison is parsed-JSON field-for-field (whitespace-insensitive) with tokens
byte-exact via dict equality. The fixture trees are copied into a throwaway repo
per run and the tokens are path-independent by construction, so a fresh checkout
reproduces the pinned bytes.

## Notes for maintainers

- The intra-fixture imports (`from util import helper` in `single_root`,
  `from alpha.a import run` in `multi_root`) are **load-bearing**: matrix
  resolves them into the dependency edges that make the subgraph fingerprint span
  more than one member. They are source text the engine parses, never imports the
  test process executes. A static import checker (e.g. groundcheck) flags them as
  unresolved — that is a known false positive for these fixtures; do not "fix" the
  imports.
- To re-mint after an intentional engine change, run the pre-move CLI against
  these trees the same way (`hive-edge mint|verify`) and overwrite `goldens.json`.
  Re-minting is only correct when the token format genuinely changed — otherwise a
  red CT-16 is a real fork, not a stale golden.
