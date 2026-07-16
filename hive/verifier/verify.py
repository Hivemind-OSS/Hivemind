"""The verify() orchestrator — composes model, registry, rootfind, spawn, ingest.

One public function: consume a composer-supplied touched-set (this package
never parses a diff), ask the matrix engine for the affected-test lower bound
(with the registry's glob union passed explicitly — the engine default is
narrower than the rows need), then per language and per config dir: probe the
tool, spawn the recipe hermetically, ingest its report, classify.

Safe direction: every per-group and per-class step fails CLOSED — a timeout,
missing report, parse failure, or internal fault lands that scope in
``errored`` with a reason while every other scope still completes; abstentions
(``not_run``/``errored``) never read as a pass anywhere in assembly.
``verify()`` raises only on invalid arguments.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from hive.verifier.ingest import INGESTERS, ReportParseError
from hive.verifier.registry import (
    REGISTRY,
    REGISTRY_VERSION,
    LangRecipe,
    ToolRecipe,
    all_test_globs,
)
from hive.verifier.result import (
    Affected,
    ClassResult,
    Diagnostic,
    Ingested,
    LangOutcome,
    RunState,
    VerifierToolVersion,
    VerifyResult,
    classify,
)
from hive.verifier.rootfind import group_by_config_dir
from hive.verifier.spawn import SpawnResult, ToolPresence, probe_tool, run_hermetic
from hive.verifier.touched import TouchedSet

if TYPE_CHECKING:
    import networkx as nx
    from matrix import Graph

# Worst state first: a failure outranks an internal fault, which outranks a
# pass, which outranks an abstention — an errored member can never be
# shadowed by a passed one.
_STATE_PRECEDENCE: tuple[RunState, ...] = ("failed", "errored", "passed", "not_run")

# Formats whose report can arrive on stderr (go vet reports there; the
# native-exit floor wants the stderr tail): those groups read both streams.
# JSON/structured formats read stdout alone so noise cannot corrupt the parse.
_STDERR_BEARING_FORMATS = frozenset({"gotest", "native-exit"})

_ZERO_TESTS_REASON = "no affected tests in blast radius (lower bound)"


@dataclass(frozen=True, slots=True)
class VerifyOptions:
    """Operator-facing knobs; fail-closed behavior itself is unconditional."""

    run_typecheck: bool = True
    run_tests: bool = True
    run_adequacy: bool = False  # v1: structurally inert (no row has a recipe)
    authoritative: bool = False  # True: full suite per affected config dir
    depth: int = 2
    # Path-dialect bridge (BUG-039): the engine's extraction root relative to
    # repo_root (e.g. "pkg" on a single-source-root checkout, "" when the
    # corpus is multi-rooted and the dialects already agree). The composer
    # derives it per side; the default "" is a structural no-op.
    root_offset: str = ""
    registry: Mapping[str, LangRecipe] | None = None  # None -> REGISTRY

    def __post_init__(self) -> None:
        if self.depth < 1:
            raise ValueError("VerifyOptions.depth must be >= 1")


@dataclass(frozen=True, slots=True)
class _GroupOutcome:
    """One config-dir spawn's verdict; ``ingested`` only when a report parsed."""

    state: RunState
    reason: str | None
    ingested: Ingested | None


def _merge_states(states: Iterable[RunState]) -> RunState:
    collected = set(states)
    for state in _STATE_PRECEDENCE:
        if state in collected:
            return state
    return "not_run"


def _row_for_path(registry: Mapping[str, LangRecipe], path: str) -> LangRecipe | None:
    name = path.rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:  # extensionless, or a dotfile
        return None
    extension = name[dot:]
    for row in registry.values():
        if extension in row.extensions:
            return row
    return None


def _row_for_test_path(
    registry: Mapping[str, LangRecipe], path: str
) -> LangRecipe | None:
    """Resolve an affected TEST file to the row whose runner owns it.

    Test files are discovered by the rows' test globs, so the same globs name
    the owner (a ``t/*.sql`` pgTAP test belongs to the sql row by glob). Glob
    match first (full path or basename, the engine's own matching semantics),
    extension as the fallback; lang-sorted iteration keeps any tie
    deterministic."""
    name = path.rsplit("/", 1)[-1]
    for lang in sorted(registry):
        row = registry[lang]
        if any(fnmatch(path, glob) or fnmatch(name, glob) for glob in row.test_globs):
            return row
    return _row_for_path(registry, path)


def _substitute_argv(
    argv: tuple[str, ...],
    *,
    files: tuple[str, ...] | None,
    root: Path,
    report: Path | None,
) -> tuple[str, ...]:
    """Expand the row placeholders; ``files=None`` elides {files} entirely
    (authoritative mode). A row with no {files} runs as-is — the go rows."""
    out: list[str] = []
    for part in argv:
        if part == "{files}":
            if files is not None:
                out.extend(files)
            continue
        part = part.replace("{root}", str(root))
        if report is not None:
            part = part.replace("{report}", str(report))
        out.append(part)
    return tuple(out)


def _run_group(
    recipe: ToolRecipe,
    members: tuple[Path, ...],
    config_dir: Path,
    spawn: Callable[..., SpawnResult],
    *,
    authoritative: bool,
) -> _GroupOutcome:
    """Spawn one recipe from one config dir and read its report, fail-closed."""
    tool = recipe.argv[0]
    files_rel = tuple(p.relative_to(config_dir).as_posix() for p in members)
    with tempfile.TemporaryDirectory(prefix="hive-verifier-") as tmp:
        report = Path(tmp) / "report.xml" if recipe.report_source == "file" else None
        argv = _substitute_argv(
            recipe.argv,
            files=None if authoritative else files_rel,
            root=config_dir,
            report=report,
        )
        spawned = spawn(
            argv, cwd=config_dir, timeout_s=recipe.timeout_s, extra_env=recipe.extra_env
        )
        if spawned.timed_out:
            return _GroupOutcome(
                "errored", f"{tool} timed out after {recipe.timeout_s}s", None
            )
        if report is not None:
            if not report.exists():
                return _GroupOutcome(
                    "errored", f"{tool} produced no report file", None
                )
            raw = report.read_bytes()
        else:
            if spawned.truncated:
                return _GroupOutcome(
                    "errored",
                    f"{tool} output truncated at the cap; report unreliable",
                    None,
                )
            raw = (
                spawned.stdout + b"\n" + spawned.stderr
                if recipe.report_format in _STDERR_BEARING_FORMATS
                else spawned.stdout
            )
    try:
        ingested = INGESTERS[recipe.report_format](raw, exit_code=spawned.exit_code)
    except ReportParseError as exc:
        return _GroupOutcome("errored", f"{tool}: {exc}", None)
    state, reason = classify(ingested)
    return _GroupOutcome(state, reason, ingested)


def _abstain_class(
    state: Literal["not_run", "errored"],
    reason: str,
    *,
    mode: Literal["scoped", "authoritative"] | None = None,
) -> ClassResult:
    return ClassResult(
        state=state,
        ran=False,
        passed=0,
        failed=0,
        errored=0,
        diagnostics=(),
        runners=(),
        reason=reason,
        report_format=None,
        per_lang=(),
        mode=mode,
    )


def _lang_targets(
    registry: Mapping[str, LangRecipe],
    paths: Iterable[str],
    slot: str,
    resolve: Callable[[Mapping[str, LangRecipe], str], LangRecipe | None],
) -> dict[str, tuple[LangRecipe, ToolRecipe | None, tuple[str, ...]]]:
    """Partition paths by language row, lang-sorted; paths no resolver can
    place have no runnable recipe anywhere and drop out."""
    rows: dict[str, LangRecipe] = {}
    by_lang: dict[str, list[str]] = {}
    for path in sorted(set(paths)):
        row = resolve(registry, path)
        if row is None:
            continue
        rows[row.lang] = row
        by_lang.setdefault(row.lang, []).append(path)
    return {
        lang: (rows[lang], getattr(rows[lang], slot), tuple(files))
        for lang, files in sorted(by_lang.items())
    }


def _run_class(
    *,
    targets: Mapping[str, tuple[LangRecipe, ToolRecipe | None, tuple[str, ...]]],
    class_label: str,
    repo_root: Path,
    spawn: Callable[..., SpawnResult],
    probe_cache: dict[tuple[str, ...], ToolPresence],
    tools_used: dict[str, str],
    mode: Literal["scoped", "authoritative"] | None,
    empty_reason: str,
) -> ClassResult:
    """Run one evidence class across its languages and merge per D4:
    precedence ``failed > errored > passed > not_run`` at the top, with the
    full per-language truth riding the ``per_lang`` channel."""
    if not targets:
        return _abstain_class("not_run", empty_reason, mode=mode)

    lang_outcomes: list[LangOutcome] = []
    passed = failed = errored = 0
    diagnostics: list[Diagnostic] = []
    runners: set[str] = set()
    formats: set[str] = set()

    for lang, (row, recipe, files) in targets.items():
        if recipe is None:
            lang_outcomes.append(
                LangOutcome(lang, "not_run", f"no {class_label} recipe for {lang}")
            )
            continue
        tool = recipe.argv[0]
        presence = probe_tool(recipe.available_probe, cache=probe_cache, spawn=spawn)
        if not presence.available:
            lang_outcomes.append(
                LangOutcome(lang, "not_run", f"{tool} not installed ({presence.reason})")
            )
            continue
        tools_used[tool] = presence.version
        runners.add(tool)
        formats.add(recipe.report_format)

        groups = group_by_config_dir(
            [repo_root / path for path in files], repo_root, row.root_markers
        )
        group_states: list[RunState] = []
        group_reasons: list[str] = []
        for config_dir, members in groups.items():
            try:
                outcome = _run_group(
                    recipe, members, config_dir, spawn,
                    authoritative=(mode == "authoritative"),
                )
            except Exception as exc:  # one bad group never aborts the run
                outcome = _GroupOutcome(
                    "errored", f"{tool} group failed internally: {exc!r}", None
                )
            group_states.append(outcome.state)
            if outcome.reason:
                group_reasons.append(outcome.reason)
            if outcome.ingested is not None:
                passed += outcome.ingested.passed
                failed += outcome.ingested.failed
                errored += outcome.ingested.errored
                diagnostics.extend(outcome.ingested.diagnostics)

        lang_state = _merge_states(group_states)
        lang_reason = (
            "; ".join(group_reasons) or f"{tool}: no reason recorded"
            if lang_state in ("not_run", "errored")
            else None
        )
        lang_outcomes.append(LangOutcome(lang, lang_state, lang_reason))

    top = _merge_states(outcome.state for outcome in lang_outcomes)
    reason: str | None = None
    if top in ("not_run", "errored"):
        parts = [
            f"{outcome.lang}: {outcome.reason}"
            for outcome in lang_outcomes
            if outcome.state == top
        ]
        reason = "; ".join(parts) or empty_reason
    return ClassResult(
        state=top,
        ran=top in ("passed", "failed"),
        passed=passed,
        failed=failed,
        errored=errored,
        diagnostics=tuple(diagnostics),
        runners=tuple(sorted(runners)),
        reason=reason,
        report_format=next(iter(formats)) if len(formats) == 1 else None,
        per_lang=tuple(lang_outcomes),
        mode=mode,
    )


def _blast_radius(engine: Graph | nx.DiGraph, seed: str, *, depth: int,
                  test_globs: tuple[str, ...]):
    """Seam over matrix.blast_radius, module-level so tests can patch it.
    Lazy import: matrix reads MATRIX_OUT at import time, so importing
    hive.verifier must stay env-inert; matrix loads only inside a verify()."""
    from matrix import blast_radius

    return blast_radius(engine, seed, depth=depth, test_globs=test_globs)


def _compute_affected(
    engine: Graph | nx.DiGraph,
    touched: TouchedSet,
    depth: int,
    test_globs: tuple[str, ...],
    root_offset: str,
) -> Affected:
    """Union the blast radius over every touched file. v1 seeds by FILE — a
    conservative superset of the line-touched symbols' radius (more tests,
    never fewer); touched lines ride the input for provenance only.

    ``root_offset`` bridges the path dialects (BUG-039): matrix roots its
    graph at the corpus' common ancestor, so on a single-source-root checkout
    node paths drop the leading ``<root_offset>/`` that the composer's
    repo-relative touched paths carry. Each seed is translated INTO the graph
    dialect (prefix stripped) and every returned ``hit.source_file`` — files
    and tests alike — is re-rooted BACK to the repo dialect (prefix restored),
    so the affected tests the spawn receives join the real worktree.
    """
    # marker: the "" offset must stay a structural no-op (identity both ways),
    # and the two directions must stay PAIRED — stripping seeds without
    # re-rooting hits (or prefixing unconditionally) is a mutation: it breaks
    # multi-root byte-identity or hands the spawn paths outside the worktree.
    prefix = root_offset + "/" if root_offset else ""

    def to_graph(path: str) -> str:
        return path[len(prefix):] if prefix and path.startswith(prefix) else path

    def to_repo(path: str) -> str:
        return prefix + path if prefix else path

    files: set[str] = set()
    symbols: set[str] = set()
    tests: set[str] = set()
    for touched_file in touched.files:
        radius = _blast_radius(
            engine, to_graph(touched_file.path), depth=depth, test_globs=test_globs
        )
        # Hit fields are read defensively: an empty source_file is tolerated.
        for hit in (*radius.callers, *radius.dependents):
            symbols.add(str(hit.node_id))
            if hit.source_file:
                files.add(to_repo(str(hit.source_file)))
        for hit in radius.tests:
            if hit.source_file:
                tests.add(to_repo(str(hit.source_file)))
    return Affected(
        files=tuple(sorted(files)),
        symbols=tuple(sorted(symbols)),
        tests=tuple(sorted(tests)),
        unsound=True,
    )


def _summarize(typecheck: ClassResult, tests: ClassResult) -> str:
    """Deterministic one-liner; always names the abstained languages."""

    def clause(name: str, result: ClassResult) -> str:
        if result.state == "passed":
            return f"{name}: passed ({result.passed} passed)"
        if result.state == "failed":
            return f"{name}: failed ({result.failed + result.errored} failing)"
        return f"{name}: {result.state} ({result.reason})"

    parts = [clause("typecheck", typecheck), clause("tests", tests)]
    abstained = sorted(
        f"{outcome.lang} {name}: {outcome.reason or outcome.state}"
        for name, result in (("typecheck", typecheck), ("tests", tests))
        for outcome in result.per_lang
        if outcome.state in ("not_run", "errored")
    )
    if abstained:
        parts.append("abstained: " + "; ".join(abstained))
    return " | ".join(parts)


def verify(
    repo_root: str | Path,
    touched: TouchedSet,
    *,
    base_sha: str,
    head_sha: str,
    engine: Graph | nx.DiGraph,
    opts: VerifyOptions = VerifyOptions(),
    spawn: Callable[..., SpawnResult] = run_hermetic,
) -> VerifyResult:
    """Produce the complete classes-1-2 evidence bundle for one change."""
    root = Path(repo_root)
    if not root.is_dir():
        raise ValueError(f"repo_root is not a directory: {repo_root}")
    if not base_sha or not head_sha:
        raise ValueError("base_sha and head_sha must be non-empty")

    registry = opts.registry if opts.registry is not None else REGISTRY
    # The registry, not the engine, owns test discovery: its glob union rides
    # the call explicitly because the engine default misses pgTAP layouts.
    test_globs = (
        all_test_globs()
        if opts.registry is None
        else tuple(sorted({glob for row in registry.values() for glob in row.test_globs}))
    )
    probe_cache: dict[tuple[str, ...], ToolPresence] = {}
    tools_used: dict[str, str] = {}

    engine_error: str | None = None
    try:
        affected = _compute_affected(
            engine, touched, opts.depth, test_globs, opts.root_offset
        )
    except Exception as exc:
        engine_error = f"engine blast_radius failed: {exc}"
        affected = Affected(files=(), symbols=(), tests=())

    if not opts.run_typecheck:
        typecheck = _abstain_class("not_run", "typecheck disabled by options")
    else:
        try:
            typecheck = _run_class(
                targets=_lang_targets(
                    registry,
                    (f.path for f in touched.files),
                    "typecheck",
                    _row_for_path,
                ),
                class_label="typecheck",
                repo_root=root,
                spawn=spawn,
                probe_cache=probe_cache,
                tools_used=tools_used,
                mode=None,
                empty_reason="no typecheckable touched files",
            )
        except Exception as exc:  # class-level fail-closed: never a false pass
            typecheck = _abstain_class(
                "errored", f"typecheck assembly failed internally: {exc!r}"
            )

    tests_mode: Literal["scoped", "authoritative"] = (
        "authoritative" if opts.authoritative else "scoped"
    )
    if not opts.run_tests:
        tests = _abstain_class("not_run", "tests disabled by options", mode=tests_mode)
    elif engine_error is not None:
        tests = _abstain_class("errored", engine_error, mode=tests_mode)
    elif not affected.tests:
        tests = _abstain_class("not_run", _ZERO_TESTS_REASON, mode=tests_mode)
    else:
        try:
            tests = _run_class(
                targets=_lang_targets(
                    registry, affected.tests, "test", _row_for_test_path
                ),
                class_label="test",
                repo_root=root,
                spawn=spawn,
                probe_cache=probe_cache,
                tools_used=tools_used,
                mode=tests_mode,
                empty_reason="affected tests match no registered language",
            )
        except Exception as exc:  # class-level fail-closed: never a false pass
            tests = _abstain_class(
                "errored", f"tests assembly failed internally: {exc!r}", mode=tests_mode
            )

    return VerifyResult(
        typecheck=typecheck,
        tests=tests,
        # v1 ships the slot, not a producer: no registry row carries an
        # adequacy recipe, so run_adequacy is structurally inert and the
        # honest class-2 caveat ("suite strength unmeasured") stays explicit.
        adequacy=None,
        affected=affected,
        tool_version=VerifierToolVersion(
            head_sha=head_sha,
            base_sha=base_sha,
            tool_versions=tuple(sorted(tools_used.items())),
            registry_version=REGISTRY_VERSION,
        ),
        summary=_summarize(typecheck, tests),
    )
