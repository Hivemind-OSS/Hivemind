# matrix

> Reference for the first-party **`hive.matrix`** subpackage of the one `hive` distribution — the
> AST-only code-structure engine behind the census `matrix/subgraph_fp` token and the verifier's
> affected-test set. Absorbed from the former standalone engine in U5: it is not a separate
> distribution and has no install of its own — it ships inside the `hive` server, and its
> third-party dependencies (tree-sitter grammars, networkx) resolve through the repo lockfile.

A deterministic, **AST-only** code-structure engine. It turns a repository into a
directed graph of symbols and files and answers one question precisely: *if this
symbol or file changes, what is the lower-bound set of things that could be
affected?*

- **No LLM, no network, no API cost.** Pure static analysis over tree-sitter ASTs.
- **Deterministic.** The same tree builds the same graph and the same content hash.
- **Incremental.** A content-hash manifest re-derives only what changed per commit.
- **Versioned.** Every build is stamped so a stale graph is detectable.
- **A cache, never a source of truth.** Safe to delete and rebuild at any time.

## What it produces

Five components behind a narrow public surface:

| Component | Module | Responsibility |
|---|---|---|
| C1 | `hive/matrix/model.py`, `hive/matrix/ids.py` | Typed `Node`/`Edge`/`Graph` model, relation taxonomy, canonical node-ID minting |
| C2 | `hive/matrix/extract/` | Multi-language tree-sitter extractor (one module per language family) |
| C3 | `hive/matrix/affected.py`, `hive/matrix/blastradius.py` | Reverse-reachability blast-radius (a **lower bound**) |
| C4 | `hive/matrix/detect.py`, `hive/matrix/assemble.py`, `hive/matrix/serialize.py` | Incremental manifest + graph assembly/merge/prune + `graph.json` |
| C5 | `hive/matrix/version.py` | Deterministic version stamp (`graph_sha256`, commit, counts) |

## Languages

Exactly eight, by design: `python`, `javascript`, `typescript`, `go`, `rust`, `c`,
`cpp`, `sql`. Adding a ninth is one new `extract/<lang>.py` plus a dispatch row —
the rest of the engine is language-agnostic.

The SQL grammar is an optional extra; without it `extract_sql` returns an error
dict and the other seven languages still build.

## Use

```python
from pathlib import Path
import hive.matrix as matrix

graph = matrix.build_graph(Path("path/to/repo"))     # full derive → matrix-out/graph.json
graph = matrix.update(Path("path/to/repo"))          # incremental re-derive per commit

radius = matrix.blast_radius(graph, "my_function", depth=2)
print(radius.callers, radius.dependents, radius.tests, radius.unsound)  # unsound is always True

stamp = matrix.version_stamp(graph, Path("path/to/repo"))
print(stamp.graph_sha256, stamp.commit_sha, stamp.node_count, stamp.edge_count)

# The path-dialect bridge: node source_file paths are relative to the corpus'
# inferred common ancestor, which on a single-source-root checkout is a SUBDIR
# of the repo. root_offset names the dropped prefix ("" when they agree), so a
# consumer can re-root graph paths to repo-relative without re-implementing the
# inference.
offset = matrix.root_offset(Path("path/to/repo"), files)  # e.g. "" or "src"
```

### Blast radius is a lower bound

Static `calls` resolution leans on import evidence. Dynamic dispatch, reflection,
and framework callbacks are invisible to it — a well-documented 40–61% dynamic
miss. Every `BlastRadius` therefore carries `unsound=True`: treat it as "at least
these are affected", never "only these". Honesty handles the gap.

## Goldens

The per-language extraction goldens are the durable specification of correct
output. They live under `tests/matrix/golden/` and are regenerated with
`scripts/regen_matrix_goldens.py` after an intentional extraction change — the
`<0.26` tree-sitter pin is load-bearing, since the goldens were built on 0.25.x.
