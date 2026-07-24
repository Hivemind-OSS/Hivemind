"""hive.edge — the in-image engine CLI front (a first-party `hive` subpackage).

`hive.edge.cli` is the one console-script verb tree fronting the two engine subpackages —
`hive.combdrift` (node-level contract-drift / symbol-existence verifier) and `hive.matrix`
(AST-only code-structure engine + blast radius). It carries no engine logic and no version of
its own: post-absorption there is no separate hive-edge distribution, so the engine provenance
literals (`hive.combdrift.version.COMBDRIFT_VERSION`, `hive.matrix.__version__`) are the version
identity `--version` reports.

Fail direction: the agent verbs (mint / verify) fail OPEN — exit 0 with empty/again output on any
fault, so a broken install can never block a capture or recall; the operator graph verbs stay
exit-coded (0 ok / 2 usage-or-fault).
"""
