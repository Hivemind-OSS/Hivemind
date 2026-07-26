"""hive-edge CLI — the in-image engine verb tree.

The first-party CLI front over the two engine subpackages (`hive.combdrift` + `hive.matrix`);
no engine logic lives here. Two exit-code regimes, kept in SEPARATE verbs so the contract stays
describable:

  - AGENT verbs (mint / verify) ALWAYS exit 0 (fail-open — a nonzero could block a capture or
    recall step). On any fault they emit the empty/again output and log ONE line to stderr +
    ~/.hive-edge/last-error.log, so a genuinely broken install is operator-visible and
    distinguishable from a legitimately unverifiable anchor.
  - OPERATOR verbs (graph) keep exit-coded semantics (0 ok / 2 usage-or-fault).

`_split_anchor` is the ONE Hivemind-anchor tokenizer used by BOTH mint and verify, so the capture
and verify sides can never disagree on how a `path:symbol` string is parsed. The mint / verify /
subgraph-fp computations live in shared cores (`_mint_core`, `_verify_core`, `_subgraph_fp_core`)
that the CLI verbs consume — the verb prints the JSON.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import hive.combdrift as combdrift

if TYPE_CHECKING:
    from hive.matrix import Graph

GIT_TIMEOUT_S = 5.0


def _clean_git_env() -> dict[str, str]:
    """Call-time delegate to `matrix.gitenv.clean_git_env` (the ONE denylist
    owner): this module keeps every engine-package import call-time, so
    importing the CLI stays env-inert."""
    from hive.matrix import gitenv

    return gitenv.clean_git_env()


# The device state home (error/state files, the matrix graph cache) —
# the ONE owner. HIVE_EDGE_HOME overrides, read at import time (the CLI is a short-lived
# process). Redirected under test.
CONFIG_DIR = Path(os.environ.get("HIVE_EDGE_HOME") or Path.home() / ".hive-edge")

_LINE_NUMBER = re.compile(r"\d+")

# A clean, letter-initial filename extension (`.py`, `.rs`, `.js`) — the shape a real
# code path's basename ends in. Rejects a dotted prose token's trailing segment
# (`.0`, `.0 rollout`: digit-led / space-bearing), which is not an extension.
_CODE_EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]*")

# The in-flight memory-management options a stale verdict carries, on every harness. Ordered by what
# the agent may do alone vs what needs a human. hive_flag is deliberately absent — it is a
# memory-vs-memory conflict tool (two episode ids) and does not apply to a single stale anchor.
REMEDIATION_TEXT = (
    "This memory's anchor no longer matches the code (stale) — treat it as REFERENCE ONLY, do not "
    "follow it. Options, by what you may do alone vs what needs a human: (1) act now, no approval — "
    "record hive_outcome(hurt=[<episode_id>]) for this stale anchor. (2) Escalate a human-gated "
    "retirement, diagnosing per BAD_VS_STALE: if the memory has a SUCCESSOR (STALE), "
    "hive_supersede(loser, winner) or hive_write(replaces=...) carrying the corrected fact; if it "
    "was wrong even when written (BAD), hive_prune. ALERT your human — never retire on your own "
    "judgment."
)

# The advisory a changed dependency NEIGHBORHOOD carries — deliberately NOT a remediation, and
# never part of the verify output itself: a changed neighborhood does not mean the memory is
# wrong, so the radius tier never touches the anchor verdict and names no retirement option.
# Single owner; the recall relay is the consumer.
RADIUS_NOTICE = (
    "code in this memory's dependency neighborhood has changed since it was stored — re-verify "
    "against the current code before relying on it; the memory itself may still be correct"
)


# ── shared helpers ──────────────────────────────────────────────────────────────


def _split_anchor(text: str) -> tuple[str, str | None]:
    """A free-text Hivemind anchor -> (path, symbol|None). `'::'` (Class::method) is split first,
    then the first `':'`. A symbol that is empty or a bare line number is existence-only (None).

    The single-colon fallback is the READER side of a mint-current/read-historical split, NOT a
    fork to be tidied away (the meta-envelope posture, THEORY §5). `hive.domain.anchor_grammar`
    is the MINT side and refuses that spelling at the write boundary from now on; this branch
    keeps the anchors already stored in it resolvable, so they keep a genuinely computed drift
    verdict and stay inside retirement coverage. Making it strict would replace a real verdict
    with `unverifiable` for that whole population — trading information for nothing — and would
    move the frozen engine-parity goldens. Same for `_LINE_NUMBER`: the boundary states "a bare
    number is not a symbol" as a refusal for NEW anchors, this states it as leniency for OLD
    ones; the pair is pinned by the both-sides agreement test."""
    text = (text or "").strip()
    if "::" in text:
        path, _, symbol = text.partition("::")
    elif ":" in text:
        path, _, symbol = text.partition(":")
    else:
        return text, None
    symbol = symbol.strip()
    if not symbol or _LINE_NUMBER.fullmatch(symbol):
        return path.strip(), None
    return path.strip(), symbol


def _is_code_shaped_path(path: str) -> bool:
    """True when `path` looks like a code file: it contains NO whitespace AND its basename
    carries a clean, letter-initial filename extension (`pkg/f.py`, `src/main.rs`, `a.py`).
    A prose anchor is not code-shaped — it either holds whitespace ('the auth refresh flow',
    'update the readme.md') or its trailing dot-segment is not a real extension ('the v2.0
    rollout' -> '.0 rollout', 'the rollout v2.0' -> '.0': space-bearing / digit-led). A bare
    `os.path.splitext` extension check is too loose: it reads a dotted prose token as code and
    routes a not-a-code anchor to a false `stale`. Used so a MISSING code file stays `stale`
    (the code moved past the memory) while a missing non-code path is only `unverifiable` — a
    prose string was never code, so the code cannot have moved past it."""
    # // marker: loosening this to "has ANY extension" (dropping the whitespace/letter-initial
    # gate) misreads dotted prose as code and resurrects the false stale — a mutation.
    if any(ch.isspace() for ch in path):
        return False
    return bool(_CODE_EXTENSION.fullmatch(os.path.splitext(path)[1]))


def _resolve_repo(repo_arg: str | None) -> str | None:
    """The repo root every verb resolves against: the passed `--repo`, else `git rev-parse
    --show-toplevel` (NEVER os.getcwd(), which would mis-root an anchor from a subdirectory).
    Returns None when omitted and not inside a git repo — the caller then fails open."""
    if repo_arg:
        return repo_arg
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_clean_git_env(),
        )
    except Exception:
        return None
    top = (proc.stdout or "").strip()
    return top if (proc.returncode == 0 and top) else None


def _log_error(verb: str, exc: BaseException) -> None:
    """One operator-visible line on any agent-verb fault, so a BROKEN install is distinguishable
    from a legitimately unverifiable anchor. Best-effort — never raises, never changes stdout/exit."""
    line = f"hive-edge {verb}: internal error ({type(exc).__name__}: {exc})"
    try:
        sys.stderr.write(line + "\n")
    except Exception:
        pass
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_DIR / "last-error.log", "a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass


# ── agent verbs (ALWAYS exit 0) ─────────────────────────────────────────────────


def _mint_core(repo: str | None, anchor: str) -> dict[str, str]:
    """Mint an anchor's interface fingerprint — the shared capture core (the CLI verb AND the
    pre-capture hook both call it). `{"combdrift/fp": <token>}` for a resolvable callable, else
    `{}`. Fail-open: NEVER raises — a broken engine logs one line and yields `{}`."""
    try:
        path, symbol = _split_anchor(anchor)
        if repo is None or symbol is None:
            return {}
        token = combdrift.fingerprint_anchor(repo, combdrift.Anchor(path, symbol))
        # // marker: dropping the "combdrift/fp" key (yielding the bare token) is a mutation.
        return {"combdrift/fp": token} if isinstance(token, str) else {}
    except Exception as exc:  # fail-open: a broken engine must never block a capture
        _log_error("mint", exc)
        return {}


def _cmd_mint(args: argparse.Namespace) -> int:
    """Capture seam: print the UNION of both anchor fingerprints — `combdrift/fp` (interface
    shape) and `matrix/subgraph_fp` (dependency neighborhood) — ONE JSON map to pass verbatim
    as capture meta. Each key appears only when its engine resolves the anchor; `{}` when
    neither does. `--branch-scope` (opt-in, NEVER automatic) additionally merges the
    set-valued `git/branches` relevance tag: bare = the current branch, explicit names = a
    set. Fail-open: no resolvable branch name (detached HEAD / outside a repo) ⇒ the key is
    simply absent and mint still exits 0."""
    repo = _resolve_repo(args.repo)
    minted = {**_mint_core(repo, args.anchor), **_subgraph_fp_core(repo, args.anchor)}
    if args.branch_scope is not None:
        names = args.branch_scope or ([b] if (b := _current_branch(repo)) else [])
        token = _branches_token(names)
        if token:
            minted["git/branches"] = token
    print(json.dumps(minted))
    return 0


def _verify_core(
    repo: str | None,
    anchor: str,
    fp: str | None,
    subgraph_fp: str | None = None,
    *,
    graph: Graph | None = None,
    branches: str | None = None,
    consumer_branch: str | None = None,
) -> dict[str, Any]:
    """Verify one anchored hit — the shared recall core (the CLI verb AND the post-recall hook
    both call it). Returns `{"verdict", "reason"[, "remediation", "delta"][, "radius"]}`; a
    `stale` verdict CARRIES the remediation options (+ a fingerprint `delta` when one exists).
    A stored `subgraph_fp` adds the SEPARATE advisory tier: `"radius": "changed" | "current"`
    against a recompute of the anchor's dependency-neighborhood fp (`graph=None` loads the
    persistent graph; a caller already holding it passes it). Recompute impossible / `{}` ⇒ the
    key is OMITTED — byte-inert for old memories and unverifiable anchors. The radius NEVER
    moves the anchor verdict in either direction: a changed neighborhood does not mean the
    memory is wrong. Fail-open: NEVER raises — repo None or an internal fault yields an
    `unverifiable` verdict (bias toward keep). A stored token whose version differs from
    `SUBGRAPH_FP_VERSION` is incomparable ⇒ the key is omitted (never `changed`).

    A stored `branches` token (the hit's `git/branches` relevance scope) routes by SET
    MEMBERSHIP: consumer branch ∉ the tagged set AND the anchor would read `stale` ⇒ the
    advisory `branch_scoped` verdict — scoped to another line, NOT stale here, so it carries
    NO remediation, NO delta, and the radius tier is suppressed. Every other combination
    (member, no tag, unreadable/other-version token, detached/unknown consumer) falls through
    byte-identical to the untagged verify — fail-open, never a false off-branch."""
    try:
        if repo is None:
            return {"verdict": "unverifiable", "reason": "unverifiable"}
        path, symbol = _split_anchor(anchor)
        record = combdrift.Record(
            id="verify",
            claim_text="",
            anchors=(combdrift.Anchor(path, symbol, fingerprint=(fp or None)),),
        )
        result = combdrift.verify_record(repo, record)
        a = result.anchors[0]
        verdict = result.verdict
        reason = a.reason.split(":", 1)[0]
        # A missing NON-code path (a prose anchor with no file extension) was never code, so the
        # code cannot have moved past it: it is unverifiable, not stale. Without this, the
        # post-recall hook relays a false retire-this-memory alarm on every prose-anchored hit.
        if (
            verdict == "stale"
            and reason == "file_missing"
            and not _is_code_shaped_path(path)
        ):
            # // marker: dropping this reclassification (prose stays stale) is a mutation.
            verdict, reason = "unverifiable", "not_a_code_anchor"
        # Branch-scope membership routing — AFTER the prose reclassification (a prose
        # anchor is unverifiable, never off-branch), BEFORE the remediation composes.
        scope = _branch_scope(branches) if branches else None
        off_set = (
            scope is not None
            and consumer_branch is not None
            and consumer_branch not in scope
        )
        if scope is not None and off_set and verdict == "stale":
            # marker: emitting stale (or remediation) for an off-set consumer is a mutation —
            # scoped-to-another-line is advisory relevance, never a retirement trigger.
            return {
                "verdict": "branch_scoped",
                "reason": "off_branch",
                "branch_scope": {
                    "scoped_to": sorted(scope),
                    "consumer": consumer_branch,
                },
                "notice": BRANCH_SCOPE_NOTICE.format(
                    scoped=", ".join(sorted(scope)), consumer=consumer_branch
                ),
            }
        out: dict[str, Any] = {"verdict": verdict, "reason": reason}
        if verdict == "stale":
            # // marker: dropping the remediation block from a stale verdict is a mutation.
            out["remediation"] = REMEDIATION_TEXT
            if a.delta is not None:
                out["delta"] = {
                    "old_token": a.delta.old_token,
                    "new_token": a.delta.new_token,
                    "breaking": a.delta.breaking,
                }
        # marker: recomputing/emitting radius for an off-set consumer is a mutation —
        # an off-branch neighborhood signal is noise for this consumer.
        if subgraph_fp and not off_set:
            # // marker: comparing tokens across versions (dropping this gate) is a mutation —
            # a version bump would emit a false `changed` on every old memory (BUG-037).
            if _subgraph_fp_version(subgraph_fp) == SUBGRAPH_FP_VERSION:
                recomputed = _subgraph_fp_core(repo, anchor, graph=graph)
                if recomputed:
                    # // marker: writing the anchor verdict from the radius tier (either
                    # direction) is a mutation — radius is a separate ADVISORY channel.
                    out["radius"] = (
                        "changed"
                        if recomputed["matrix/subgraph_fp"] != subgraph_fp
                        else "current"
                    )
            # else: incomparable (older/future/malformed) ⇒ OMIT radius — silence, and no
            # graph recompute is even attempted (the load is the expensive half).
        return out
    except (
        Exception
    ) as exc:  # fail-open: bias toward keep — an internal fault is unverifiable
        _log_error("verify", exc)
        return {"verdict": "unverifiable", "reason": "error"}


def _cmd_verify(args: argparse.Namespace) -> int:
    """Recall seam: verify one anchored hit. Prints verdict + machine reason; a `stale` verdict
    additionally CARRIES the remediation options (+ a fingerprint `delta` when one exists); a
    stored `--subgraph-fp` token adds the advisory `radius` field; a stored `--branches`
    token routes an off-set consumer's would-be-stale to the advisory `branch_scoped`
    verdict (membership keyed on the checkout's current branch)."""
    repo = _resolve_repo(args.repo)
    print(
        json.dumps(
            _verify_core(
                repo,
                args.anchor,
                args.fp,
                args.subgraph_fp,
                branches=args.branches,
                consumer_branch=_current_branch(repo),
            )
        )
    )
    return 0


# ── graph verb group (operator queries over the persistent matrix graph) ────────


# `_state_dir` — the device state home's `state/` subdir, the parent of the per-checkout
# matrix graph cache below; HIVE_EDGE_HOME-rooted via CONFIG_DIR.
def _state_dir() -> Path:
    return CONFIG_DIR / "state"


def _matrix_out_dir(repo: str) -> Path:
    """The per-checkout matrix artifact cache: `$HIVE_EDGE_HOME/state/matrix/<digest>` where
    digest = sha256(realpath(repo))[:32], so two checkouts of one project never share (or
    clobber) a cache. NEVER inside the repo: an untracked artifact dir would flip
    `git status --porcelain` for any git operation over the checkout, so the cache lives under
    the device state home."""
    digest = hashlib.sha256(os.path.realpath(repo).encode("utf-8")).hexdigest()[:32]
    return _state_dir() / "matrix" / digest


def _load_graph(repo: str) -> Graph | None:
    """matrix.Graph for `repo`, refreshed incrementally against the WORKING TREE on every call
    (`matrix.update`; the first call on a checkout pays one full build), artifacts cached under
    `_matrix_out_dir(repo)`. Lazy-imports matrix — the engine loads only when a graph consumer
    runs. Fail-open: None on ANY fault, one `_log_error` line (broken install stays
    operator-visible, never a crash)."""
    try:
        import hive.matrix as matrix

        return matrix.update(Path(repo), out_dir=_matrix_out_dir(repo))
    except Exception as exc:
        _log_error("graph", exc)
        return None


# ── subgraph fingerprint (the dependency-neighborhood identity) ─────────────────

# Fingerprint semantics version: UNION cone (seed ∪ forward ∪ reverse over the dependency
# relations), shape-only member hash. Bump on ANY semantic change — a token minted under an
# older scheme must never spuriously compare equal to a current one.
SUBGRAPH_FP_VERSION = "1"
_SUBGRAPH_FP_PREFIX = "matrix-subgraph-fp/"
# De-facto unbounded walk depth: the neighborhood claim is reachability, and depth=2 provably
# misses 3-hop members.
_UNBOUNDED_DEPTH = 10_000


def _subgraph_fp_version(token: str) -> str | None:
    """The version segment of a stored `matrix-subgraph-fp/<N>:<body>` token, or None for a
    foreign/malformed token (no recognized prefix, or no ':' separator). The ONE reader-side
    parse of this token's envelope — comparison is legal only between same-version tokens;
    an unreadable/other-version token is incomparable and must route to SILENCE (the meta
    envelope law), never to a false `changed`."""
    if not token.startswith(_SUBGRAPH_FP_PREFIX):
        return None
    version, sep, _body = token[len(_SUBGRAPH_FP_PREFIX) :].partition(":")
    return version if (sep and version) else None


# ── branch-scope tag (the opt-in git/branches relevance scope) ──────────────────

# Token semantics version: a SET of git branch names, space-joined and sorted (space is
# illegal in a git ref name, so the body is unambiguous). Bump on ANY semantic change —
# the reader is current-version-only (the BUG-037 idiom): an other-version token is
# incomparable and routes to silence.
BRANCHES_VERSION = "1"
_BRANCHES_PREFIX = "git-branches/"

# The advisory an off-set branch-scoped memory carries — deliberately NOT a remediation
# (scoped-to-another-line is not stale) and it names no retirement option. Single owner;
# the verify core composes it and the recall relay is the consumer.
BRANCH_SCOPE_NOTICE = (
    "this memory is scoped to branch(es) {scoped} — you are on {consumer}: likely not "
    "relevant to your branch; disregard unless working that line (scope rides the "
    "memory's git/branches tag)"
)


def _current_branch(repo: str | None) -> str | None:
    """The checkout's current branch name via `git rev-parse --abbrev-ref HEAD`.
    Fail-open: None for `repo is None`, a git fault (rc != 0 / timeout), empty output,
    or the literal `"HEAD"` (detached) — a consumer with no resolvable branch name
    keeps today's semantics everywhere downstream."""
    if repo is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_S,
            env=_clean_git_env(),
        )
    except Exception:
        return None
    name = (proc.stdout or "").strip()
    if proc.returncode != 0 or not name or name == "HEAD":
        return None
    return name


def _branches_token(names: list[str]) -> str | None:
    """Compose the set-valued `git/branches` token from branch names: strip, dedupe,
    SORT into one space-joined body. Fail-open: None on an empty (post-strip) set —
    the caller then omits the key and the capture proceeds untagged."""
    cleaned = sorted({n.strip() for n in names if n and n.strip()})
    if not cleaned:
        return None
    return f"{_BRANCHES_PREFIX}{BRANCHES_VERSION}:{' '.join(cleaned)}"


def _branch_scope(token: str) -> frozenset[str] | None:
    """The ONE reader-side envelope parse of a stored `git/branches` token (the BUG-037
    idiom): the branch-name set for a CURRENT-version token, else None. Fail direction
    is SILENCE — None (tag ignored, today's semantics) for a foreign/malformed token or
    any other version, never a false off-branch verdict."""
    if not token.startswith(_BRANCHES_PREFIX):
        return None
    version, sep, body = token[len(_BRANCHES_PREFIX) :].partition(":")
    # marker: comparing across versions (dropping this version gate) is a mutation —
    # an other-version token is incomparable and must route to silence (None).
    if not sep or version != BRANCHES_VERSION:
        return None
    names = body.split()
    return frozenset(names) if names else None


def _resolve_anchor_seed(graph: Graph, path: str, symbol: str) -> str | None:
    """Resolve a `path:symbol` anchor to its graph node WITHIN the anchor's own file. (The
    engine's global `resolve_seed` ignores the path, so an unrelated same-named symbol
    elsewhere makes resolution ambiguous, and a dotted `Cls.m` resolves to nothing.)
    `path` arrives in the GRAPH's dialect — the caller bridges a repo-root-relative anchor
    through `_graph_root_offset` first, so `source_file` comparison is exact on any layout.
    Candidates are nodes with `source_file == path` whose label matches a variant: the symbol
    itself (a class), `f"{symbol}()"` (a callable), or — for a dotted `Cls.m` — a `.m()`
    method node whose incoming `method` containment edge comes from a node labeled `Cls`.
    Exactly one candidate wins; zero or many -> None, NEVER a cross-file guess."""
    # // marker: dropping the source_file filter (matching corpus-wide) is a mutation.
    in_file = [node for node in graph.nodes() if node.source_file == path]
    matches = [node.id for node in in_file if node.label in (symbol, f"{symbol}()")]
    if "." in symbol:
        cls_label, _, method = symbol.rpartition(".")
        for node in in_file:
            if node.label != f".{method}()":
                continue
            for edge in graph.in_edges(node.id):
                if edge.relation != "method":
                    continue
                owner = graph.node(edge.source)
                if owner is not None and owner.label == cls_label:
                    matches.append(node.id)
                    break
    unique = list(dict.fromkeys(matches))
    return unique[0] if len(unique) == 1 else None


def _combdrift_symbol(graph: Graph, node_id: str) -> str | None:
    """The ONE owner of the matrix-label -> combdrift-symbol bridge (the dialects differ, so
    a label fed to combdrift verbatim NEVER resolves and every member would silently degrade
    to the identity fallback). `"f()"` -> `"f"`; a method `".m()"` -> `"Cls.m"` via its
    incoming `method` containment edge; a class label is already the symbol; a file node
    (label == its own filename) -> None."""
    node = graph.node(node_id)
    if node is None:
        return None
    label = node.label
    if label == Path(node.source_file).name:  # a file node has no symbol
        return None
    # // marker: returning the label verbatim (no dialect bridge) is a mutation.
    if label.endswith("()"):
        if label.startswith("."):
            method = label[1:-2]
            for edge in graph.in_edges(node_id):
                if edge.relation == "method":
                    owner = graph.node(edge.source)
                    if owner is not None:
                        return f"{owner.label}.{method}"
            return None  # a method node with no class edge cannot be qualified
        return label[:-2]
    return label  # a class (or other bare-symbol) label — combdrift's dialect already


def _forward_ids(g: Graph, seed: str, depth: int) -> set[str]:
    """Forward reachability — the out-edges BFS twin of the engine's reverse `affected_nodes`
    walk, filtered to the same dependency relations, over the PUBLIC Graph API only. The
    union cone needs it because reverse-only reachability misses the callee case: a CALLEE's
    signature change must move its callers' fingerprints."""
    import hive.matrix as matrix

    seen = {seed}
    queue = deque([(seed, 0)])
    while queue:
        current, dist = queue.popleft()
        if dist >= depth:
            continue
        # // marker: walking in_edges here (reverse-only, again) is a mutation.
        for edge in g.out_edges(current):
            if edge.relation not in matrix.DEPENDENCY_RELATIONS:
                continue
            if edge.target in seen:
                continue
            seen.add(edge.target)
            queue.append((edge.target, dist + 1))
    return seen - {seed}


def _graph_root_offset(repo: str) -> str:
    """The path-dialect offset between the repo root and the graph's extraction root.
    matrix roots every node's `source_file` at the corpus' common ancestor, so when ALL
    parsed source lives under one top-level dir the graph dialect drops that prefix
    (`src/two.py` -> `two.py`) while anchors stay repo-root-relative. Derived from the
    persisted manifest's absolute keys — the exact corpus the engine inferred its root
    from — via `matrix.root_offset`, the engine-owned inverse of that inference (the
    census consumes the same owner at its seam, BUG-038; this call site is BUG-035's):
    "" whenever the dialects already agree (any multi-rooted corpus), else the
    repo-root-relative prefix, e.g. `"src"`. Fail-open: "" on any fault (missing or
    foreign manifest) — exactly the pre-bridge repo-dialect assumption."""
    # // marker: forcing the offset to "" unconditionally (no dialect bridge) is a mutation.
    try:
        import hive.matrix as matrix

        manifest = json.loads(
            (_matrix_out_dir(repo) / "manifest.json").read_text(encoding="utf-8")
        )
        return matrix.root_offset(repo, [Path(key) for key in manifest])
    except Exception:
        return ""  # unreadable/foreign manifest: keep the repo-dialect assumption


def _member_token(
    repo: str, graph: Graph, node_id: str, *, offset: str = ""
) -> str | None:
    """One member's content token: `{path}:{qualified_symbol_or_label}:{shape}`, where path
    is the member's REPO-ROOT-relative source file (`offset` re-roots the graph's dialect;
    "" when the dialects already agree) and shape is combdrift's call-shape token when the
    label bridges to a symbol combdrift resolves, else the literal `"identity"` (uncovered
    language / non-callable) — so every member still detects rename/move through its name
    half. Portable by construction: built from label + the repo-relative path, NEVER
    node_id (ids can embed absolute checkout paths). None only for a node that vanished
    from the graph."""
    node = graph.node(node_id)
    if node is None:
        return None
    rel_path = f"{offset}/{node.source_file}" if offset else node.source_file
    symbol = _combdrift_symbol(graph, node_id)
    shape = "identity"
    if symbol is not None:
        token = combdrift.fingerprint_anchor(repo, combdrift.Anchor(rel_path, symbol))
        if isinstance(token, str):
            shape = token
    return f"{rel_path}:{symbol if symbol is not None else node.label}:{shape}"


def _combine_tokens(tokens: list[str]) -> str:
    """Fold member tokens into the ONE opaque fingerprint:
    `matrix-subgraph-fp/<version>:<sha256>`. Hashes the SORTED tokens — order-independence
    is load-bearing, because two builds of the same tree may enumerate members differently
    and the fp must never move with iteration order."""
    # // marker: dropping the sorted() (hashing arrival order) is a mutation.
    digest = hashlib.sha256("\n".join(sorted(tokens)).encode("utf-8")).hexdigest()
    return f"{_SUBGRAPH_FP_PREFIX}{SUBGRAPH_FP_VERSION}:{digest}"


def _subgraph_member_tokens(
    repo: str, graph: Graph, anchor: str, *, depth: int = _UNBOUNDED_DEPTH
) -> list[str] | None:
    """The sorted member-token list behind `anchor`'s subgraph fp — members are the union
    cone {seed} ∪ forward reach ∪ reverse reach over the dependency relations. None when the
    anchor does not resolve to a node in its own file. ONE owner of the member pipeline:
    `_subgraph_fp_core` hashes this list and `graph fp --explain` prints it, so the
    explanation can never drift from the fingerprint. The anchor arrives repo-root-relative
    (the ONLY anchor dialect agents see); `_graph_root_offset` bridges it into the graph's
    dialect for seed resolution, and every member token is bridged BACK to repo-root-relative
    form — so the fp is layout-independent AND portable."""
    path, symbol = _split_anchor(anchor)
    if symbol is None:
        return None
    offset = _graph_root_offset(repo)
    if offset:
        if not path.startswith(offset + "/"):
            return None  # the anchor lies outside the graph's extraction root
        path = path[len(offset) + 1 :]
    seed = _resolve_anchor_seed(graph, path, symbol)
    if seed is None:
        return None
    import hive.matrix as matrix

    radius = matrix.blast_radius(graph, seed, depth=depth)
    members = {seed} | _forward_ids(graph, seed, depth)
    members.update(hit.node_id for hit in radius.callers)
    members.update(hit.node_id for hit in radius.dependents)
    tokens = [
        token
        for nid in sorted(members)
        if (token := _member_token(repo, graph, nid, offset=offset)) is not None
    ]
    return sorted(tokens)


def _subgraph_fp_core(
    repo: str | None,
    anchor: str,
    *,
    graph: Graph | None = None,
    depth: int = _UNBOUNDED_DEPTH,
) -> dict[str, str]:
    """`{"matrix/subgraph_fp": <token>}` for a resolvable code anchor, else `{}` — the shared
    dependency-neighborhood capture core (the mint verb, `graph fp`, and the capture hook all
    call it). The token moves iff something inside the anchor's dependency neighborhood
    changed; an unrelated edit moves the whole-graph stamp but never this token. Fail-open:
    NEVER raises — any fault logs one line and yields `{}`.

    Sequence: `_split_anchor` -> the prose gate (`_is_code_shaped_path`; a prose anchor
    returns `{}` BEFORE any graph work) -> `_resolve_anchor_seed` -> union-cone member
    tokens -> `_combine_tokens`. `graph=None` loads (and incrementally refreshes) the
    persistent per-checkout graph; a caller that already holds the graph passes it and pays
    no re-load."""
    try:
        if repo is None:
            return {}
        path, symbol = _split_anchor(anchor)
        if symbol is None or not _is_code_shaped_path(path):
            return {}
        g = graph if graph is not None else _load_graph(repo)
        if g is None:  # _load_graph already logged the fault
            return {}
        tokens = _subgraph_member_tokens(repo, g, anchor, depth=depth)
        if tokens is None:
            return {}
        return {"matrix/subgraph_fp": _combine_tokens(tokens)}
    except Exception as exc:  # fail-open: a broken engine must never block a capture
        _log_error("subgraph-fp", exc)
        return {}


# One owner of the graph verb-group usage text — the `-h/--help` body AND the usage error.
_GRAPH_USAGE = """\
usage: hive-edge graph <update | radius | fp> ...

  update  refresh the persistent per-checkout code graph from the working tree:
          update [--repo <path>]   (the first run on a checkout pays one full build)
  radius  reverse blast-radius query — who could a change to this anchor affect:
          radius --anchor <path:symbol> [--repo <path>] [--depth N]
  fp      dependency-neighborhood fingerprint for an anchor:
          fp --anchor <path:symbol> [--repo <path>] [--explain]
"""


def _cmd_graph(rest: list[str]) -> int:
    """Operator queries over the persistent per-checkout code graph — thin wrappers over the
    PUBLIC matrix API only (no general graph-query surface). Exit-coded: 0 ok / 2 usage-or-fault."""
    if not rest:
        print(_GRAPH_USAGE, file=sys.stderr, end="")
        return 2
    sub, tail = rest[0], rest[1:]
    if sub in ("-h", "--help"):
        print(_GRAPH_USAGE, end="")
        return 0
    if sub == "update":
        parser = argparse.ArgumentParser(prog="hive-edge graph update")
        parser.add_argument(
            "--repo", default=None, help="repo root (default: git toplevel)"
        )
        ns = parser.parse_args(tail)
        repo = _resolve_repo(ns.repo)
        if repo is None:
            print(
                "hive-edge graph update: no repo (pass --repo or run inside a git repo)",
                file=sys.stderr,
            )
            return 2
        graph = _load_graph(repo)
        if graph is None:  # _load_graph already logged the fault
            return 2
        import hive.matrix as matrix

        stamp = matrix.version_stamp(graph, Path(repo))
        print(
            json.dumps({"nodes": stamp.node_count, "graph_sha256": stamp.graph_sha256})
        )
        return 0
    if sub == "radius":
        parser = argparse.ArgumentParser(prog="hive-edge graph radius")
        parser.add_argument(
            "--repo", default=None, help="repo root (default: git toplevel)"
        )
        parser.add_argument(
            "--anchor",
            required=True,
            help='"path:symbol", a bare symbol, or a file path',
        )
        parser.add_argument(
            "--depth", type=int, default=2, help="reverse-walk depth (default 2)"
        )
        ns = parser.parse_args(tail)
        repo = _resolve_repo(ns.repo)
        if repo is None:
            print(
                "hive-edge graph radius: no repo (pass --repo or run inside a git repo)",
                file=sys.stderr,
            )
            return 2
        graph = _load_graph(repo)
        if graph is None:  # _load_graph already logged the fault
            return 2
        import hive.matrix as matrix

        path, symbol = _split_anchor(ns.anchor)
        radius = matrix.blast_radius(graph, symbol or path, depth=ns.depth)
        print(json.dumps(dataclasses.asdict(radius)))
        return 0
    if sub == "fp":
        parser = argparse.ArgumentParser(prog="hive-edge graph fp")
        parser.add_argument(
            "--repo", default=None, help="repo root (default: git toplevel)"
        )
        parser.add_argument(
            "--anchor", required=True, help='"path:symbol" or "path::Class.method"'
        )
        parser.add_argument(
            "--explain",
            action="store_true",
            help="list the member tokens behind the fingerprint",
        )
        ns = parser.parse_args(tail)
        repo = _resolve_repo(ns.repo)
        if repo is None:
            print(
                "hive-edge graph fp: no repo (pass --repo or run inside a git repo)",
                file=sys.stderr,
            )
            return 2
        graph = _load_graph(repo)
        if (
            graph is None
        ):  # a real fault stays exit-coded (operator regime), unlike {} below
            return 2
        result: dict[str, Any] = _subgraph_fp_core(repo, ns.anchor, graph=graph)
        if ns.explain and result:
            members = _subgraph_member_tokens(repo, graph, ns.anchor) or []
            result = {**result, "members": members}
        print(
            json.dumps(result)
        )  # an unresolvable anchor is a clean {}, never a bogus token
        return 0
    print(f"hive-edge graph: unknown subcommand {sub!r}", file=sys.stderr)
    print(_GRAPH_USAGE, file=sys.stderr, end="")
    return 2


# ── parser + dispatch ───────────────────────────────────────────────────────────


def _print_version() -> None:
    """The 3-line engine-provenance banner, printed from SOURCE LITERALS — never
    importlib.metadata, which degrades to "0+unknown" for a first-party subpackage. There is no
    separate hive-edge distribution post-absorption, so the front reports the combdrift engine's
    frozen lockstep provenance literal as its own version. Each engine line is best-effort so a
    base install without the `sync` extra still prints what it can."""
    try:
        from hive.combdrift import version as combdrift_version

        print(f"hive-edge {combdrift_version.COMBDRIFT_VERSION}")
        print(
            f"combdrift {combdrift_version.COMBDRIFT_VERSION} "
            f"(fingerprint/{combdrift_version.FINGERPRINT_VERSION})"
        )
    except Exception:
        pass
    try:
        import hive.matrix.version as matrix_version

        print(f"matrix {matrix_version.ENGINE_VERSION}")
    except Exception:
        pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hive-edge",
        description="The Hivemind edge CLI: mint/verify code-anchor currency and run the operator "
        "graph verbs — one console script over two engines.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    mint = sub.add_parser(
        "mint", help="mint an anchor's interface fingerprint (capture seam)"
    )
    mint.add_argument("--repo", default=None, help="repo root (default: git toplevel)")
    mint.add_argument(
        "--anchor", required=True, help='"path:symbol" or "path::Class.method"'
    )
    mint.add_argument(
        "--branch-scope",
        nargs="*",
        default=None,
        metavar="BRANCH",
        help="scope the memory to branches: no value = current branch; "
        "explicit names for a set",
    )
    mint.set_defaults(func=_cmd_mint)

    verify = sub.add_parser(
        "verify", help="verify an anchored recall hit (recall seam)"
    )
    verify.add_argument(
        "--repo", default=None, help="repo root (default: git toplevel)"
    )
    verify.add_argument(
        "--anchor", required=True, help='"path:symbol" or "path::Class.method"'
    )
    verify.add_argument(
        "--fp", default=None, help="the hit's stored combdrift/fp token, if any"
    )
    verify.add_argument(
        "--subgraph-fp",
        default=None,
        help="the hit's stored matrix/subgraph_fp token, if any",
    )
    verify.add_argument(
        "--branches", default=None, help="the hit's stored git/branches token, if any"
    )
    verify.set_defaults(func=_cmd_verify)

    # Help-only stub: `graph` is dispatched in main() BEFORE parsing so its argv forwards
    # verbatim. This entry exists so `hive-edge --help` documents it — it never parses argv.
    sub.add_parser(
        "graph",
        help="persistent code graph: update | radius | fp (see `hive-edge graph --help`)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("--version", "-V"):
        _print_version()
        return 0
    if argv and argv[0] == "graph":
        return _cmd_graph(argv[1:])
    parser = _build_parser()
    args = parser.parse_args(argv)
    return cast(int, args.func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
