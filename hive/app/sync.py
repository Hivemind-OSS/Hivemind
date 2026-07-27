"""The server-side sync daemon — a DRIVING adapter over the ledger, mint, and
drift doors, N repos wide.

One daemon thread (ALWAYS started by ``start_sync``; the entrypoint arms it after
serve-ready) re-reads the durable repo REGISTRY every tick — registering a repo
needs no restart, an empty registry is an inert tick — and, per registered repo
under its OWN fail-open guard, runs two legs against that repo's mirror at
``<mirror_dir>/<name>-<url-digest>/`` — the digest is identity, not decoration:
a mirror is a cache of ONE remote, so binding the URL into the path makes a
re-registered name reaching the previous repository's checkout unconstructable
(``mirror_dirname``). The repos of one tick run serially by default and
concurrently at ``sync.workers > 1`` (an isolated-by-construction fan-out; same
verdict, less wall-clock):

- LEDGER: tracked-branch movement lands as ONE unsigned receipt per contiguous
  ``watermark..tip`` range, built by ``python -m hive.census.cli build`` in a
  SUBPROCESS (engine machinery and repo code never enter this process), ingested
  in-proc under the ONE global write lock (phase post_merge; the verdict/tag are
  DERIVED by the domain service — decided execution lines cap at
  bounded-estimate, else the landed-line parity verdict rides), then the
  watermark advances in the same critical section and the post-ingest promotion
  sweep (``LifecycleService.promote_established``) runs.
- DRIFT MATERIALIZER: per (repo, tip, base_tip, anchor) wire verdicts into the
  ``anchor_drift`` cache, asked of GIT rather than of an engine. Each binding
  carries a BASELINE COMMIT (``anchor_baselines`` — the repo watermark at write
  time; the sweep here fills any that had no watermark yet, insert-if-absent so
  a recorded baseline is never moved). Per DISTINCT baseline the leg reads the
  paths that existed at it (``ls-tree``) and what the range ``base..tip`` did to
  each (``diff --name-status``); a path the range left alone is ``fresh`` with
  no further work, and only a SYMBOL anchor whose file actually moved costs one
  more call (``log -L ':sym:path'``). A symbol binding additionally costs ONE
  ``blame -L ':sym'`` the first time it is seen, to record whether that symbol
  existed at its own baseline at all — an immutable fact, stored beside the
  baseline and never re-asked, and the reason a symbol nobody ever saw cannot
  read ``fresh`` off a quiet file. The ladder itself is the pure
  ``hive.domain.staleness.decide`` — this module supplies facts and never
  decides. There is no per-anchor queue, no cap and no budget: the call count is
  bounded by real churn, which is the correct bound.
  Tips = the canonical tip (first — the served-most line never starves), plus
  every ref a LIVE episode of the repo DECLARES (``store.declared_refs`` —
  coverage a memory's own line earns whether or not anyone ever recalled it),
  plus branch tips recall DEMANDED via ``ref_requests`` within the 7-day
  demand window; old tips pruned, and a retired memory's anchor is excluded from
  the work list while its cached rows AND its baseline are dropped even at a tip
  that is still canonical (BUG-065). Every resolved declared or demanded ref is
  recorded into ``ref_tips`` before its tip is judged, so the recall-side reader
  can resolve a branch's tip to read the real verdict once it lands, rather than
  degrading forever (BUG-063), and the retirement gate can judge a memory at its
  own declared line (BUG-064).

Named safe directions (Law 6):
- AN EMPTY REGISTRY IS INERT: no git, no clone, no engine import — the tick
  returns after the registry read and the leftover-mirror prune alone (the
  prune is pure local filesystem, delete-only, and never CREATES the mirrors
  dir).
- A DEREGISTERED REPO LOSES ITS MIRROR (BUG-050): every tick reconciles the
  mirrors dir against the live registry — ``repo_remove`` stops the feed, the
  next tick deletes that repo's mirror. The same sweep reaps a SUPERSEDED
  incarnation: the prune runs before the repo legs, so a name re-registered at
  a new URL has its old directory reaped and the new one cloned within ONE
  tick. Only direct, slug-named, non-symlink
  children resolving strictly under the configured base are ever deleted, each
  under its own fail-open guard.
- THE SIDE-CHANNEL FAILS OPEN PER REPO, THE SERVE NEVER DOES: every repo runs
  under its own guard (fault → log + ``sync:<name>:last_error``; the OTHER
  repos are untouched), every leg under its own guard inside that, and the
  tick shell survives everything — nothing here can take the serve down.
- THE WATERMARK IS THE DURABLE TRUTH (``sync:<name>:last_tip`` store meta —
  the SAME key ``attach_drift`` resolves canonical tips from; ``ref_tips`` is
  its branch twin, resolved by the same reader for a queried non-canonical
  ref); the mirror and the drift cache are rebuildable caches — losing either
  can never open a coverage gap or serve a wrong verdict (an un-materialized
  anchor reads ``unverifiable``, never false-fresh, never false-stale).
- CREDENTIALS STAY IN ENV VIA INDIRECTION (D2): a registry row stores only the
  NAME of a token env var, resolved at tick time per repo; the rewritten remote
  URL lives ONLY in the mirror's git config; logs and error meta pass
  ``_redact`` first. A row-named var absent at tick fails that repo open with
  the var NAMED (the boot-time EX_CONFIG probe is the entrypoint's). The
  mirror's remote is RECONCILED against that freshly-resolved credential every
  tick, so rotating the secret takes effect without a re-clone (BUG-073) — the
  registry is truth and the mirror's own config is never authoritative.
- EVERY UNKNOWN UNDER-CLAIMS: a probe fault, an anchor with no baseline, a path
  absent from the baseline tree, a symbol that never resolved at its baseline
  and an unresolvable symbol all materialize ``unverifiable`` — the ladder can
  withhold a retirement, never grant one.
- A RECORDED BASELINE IS NEVER MOVED, AND NEITHER IS ITS SYMBOL RESOLUTION: the
  sweep inserts if absent only and the resolution is written only over the
  unprobed state, so re-baselining at a later tip (which would erase a break
  that already landed) and overwriting a real observation with a transient
  failure to resolve are both unconstructable rather than merely avoided.
- TRUST-TOUCHED ONLY MECHANICALLY: rows land through the same
  ChangeEvidenceService door as ``hive ingest``, and the only trust movement is
  the injected lifecycle's own ``promote_established`` sweep (the v3
  established rung) — no other trust handle exists here.
- CLEAN GIT ENV ON EVERY SPAWN (BUG-034): matrix.gitenv's denylist strips any
  hook-planted GIT_DIR family var from every child.

``tick()`` runs the whole cycle; the returned thread carries its control events
(``sync_stop`` / ``sync_nudge``) so the webhook nudge door can wake the loop —
ONE nudge wakes the loop for ALL registered repos.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from hive.app.config import Config, SyncConfig
from hive.app.drift import DRIFT_BLAST_RADIUS_CHANGED as BLAST_RADIUS_CHANGED
from hive.app.sync_keys import (
    backfilled_total_key,
    canonical_tip_key,
    fleet_last_error_key,
    fleet_last_sync_ts_key,
    last_error_key,
    last_sync_ts_key,
    tracked_ref_key,
)
from hive.domain.anchor_grammar import probe_target
from hive.domain.change_evidence import ChangeEvidenceService
from hive.domain.anchor_grammar import SYMBOL_SEP
from hive.domain.staleness import (
    ANCHOR_CHANGED,
    ANCHOR_MISSING,
    FRESH,
    AnchorFacts,
    decide,
    symbol_lookups,
)

_log = logging.getLogger("hive.sync")

# EVERY key this daemon writes comes from hive.app.sync_keys — the ONE grammar
# census_health reads back. Two namespaces: the per-repo builders (`sync:<repo>:<field>`)
# and the 2-part tick-SHELL fleet builders (`sync:<field>`), stamped with no repo in
# scope and served in the report's own `fleet` block rather than any repo's.
# the base dir; mirrors live at <base>/<name>-<url-digest> (see mirror_dirname)
_DEFAULT_MIRROR_DIR = "/data/sync/mirror"
_DEFAULT_TOKEN_ENV = "HIVE_SYNC__TOKEN"  # the fleet-default credential var (D2)
# The registry name grammar (store_sqlite.repo_add's gate, mirrored): only names
# the registry could ever have minted are prune candidates — anything else in
# the mirrors dir was never this daemon's mirror and is never deleted.
_REPO_SLUG_RE = re.compile(r"^[a-z0-9._-]+$")
_REF_REQUEST_WINDOW_S = (
    7 * 86_400
)  # recall demand keeps a branch on the work list this long

# ``git log -L :<funcname>:<path>`` needs a ``diff=<lang>`` attribute to know what a
# funcname line looks like — without one, git's default pattern misses every INDENTED
# declaration, so a method resolves as absent. The daemon supplies git's own SHIPPED
# drivers through ONE attributes file written beside the mirrors and passed as
# ``-c core.attributesFile=``. That source is git's LOWEST precedence, so a repo
# carrying its own ``.gitattributes`` still wins — correct by construction rather than
# by us promising not to interfere. Extensions git ships no driver for simply fall back
# to the file tier, which is the more sensitive verdict, never a blind one.
_FUNCNAME_ATTRIBUTES = (
    "\n".join(
        f"*{ext} diff={driver}"
        for ext, driver in (
            (".ads", "ada"),
            (".adb", "ada"),
            (".sh", "bash"),
            (".bash", "bash"),
            (".bib", "bibtex"),
            (".c", "cpp"),
            (".h", "cpp"),
            (".cc", "cpp"),
            (".cpp", "cpp"),
            (".cxx", "cpp"),
            (".hh", "cpp"),
            (".hpp", "cpp"),
            (".hxx", "cpp"),
            (".cs", "csharp"),
            (".css", "css"),
            (".scss", "css"),
            (".dts", "dts"),
            (".dtsi", "dts"),
            (".ex", "elixir"),
            (".exs", "elixir"),
            (".f", "fortran"),
            (".f90", "fortran"),
            (".f95", "fortran"),
            (".fountain", "fountain"),
            (".go", "golang"),
            (".html", "html"),
            (".htm", "html"),
            (".java", "java"),
            (".kt", "kotlin"),
            (".kts", "kotlin"),
            (".md", "markdown"),
            (".markdown", "markdown"),
            (".m", "objc"),
            (".mm", "objc"),
            (".pas", "pascal"),
            (".pl", "perl"),
            (".pm", "perl"),
            (".php", "php"),
            (".py", "python"),
            (".rb", "ruby"),
            (".rs", "rust"),
            (".scm", "scheme"),
            (".tex", "tex"),
        )
    )
    + "\n"
)
_ATTRIBUTES_FILE = "git-funcname-attributes"

# ``-L`` failure strings are distinguishable, and the distinction is load-bearing:
# "no match" means git could not resolve the SYMBOL (which may simply never have
# existed), while a no-path message means the FILE is gone (provable absence). The two
# spellings are `git log`'s and `git blame`'s for the same condition.
_L_NO_MATCH = "no match"
_L_NO_PATH = "There is no path"
_BLAME_NO_PATH = "no such path"

# ── the three symbol_ok states of a baseline row (store_sqlite.anchor_baselines) ──
# Ordered by how much they claim, so MIN() folds two rows onto the weaker claim.
_SYMBOL_UNPROBED = -1
_SYMBOL_ABSENT = 0
_SYMBOL_RESOLVED = 1

# ``-L :<funcname>:<file>`` takes a REGEX, and ':' is the argument's own field
# separator — so a symbol carrying one (``Ns::Store.m``) corrupts the parse and comes
# back as "There is no path", i.e. a FALSE anchor_missing. Such a spelling is skipped
# and only its dotted tail is asked. The rest is regex, not a literal: an unescaped
# ``.`` matches any character, and an unanchored short name substring-matches an
# unrelated declaration (``m`` matches ``def repo_remove``). Metacharacters are
# escaped and the name is word-bounded, with the BARE name kept as a last resort so a
# regex flavour without ``\b`` degrades to imprecision rather than to a false verdict.
_ERE_META = set("\\.^$*+?()[]{}|")


def _funcname_patterns(lookup: str) -> tuple[str, ...]:
    """The ``-L`` regexes to try for one candidate name, most precise first. Empty
    when the name cannot be expressed in ``-L``'s grammar at all."""
    if not lookup or ":" in lookup:
        return ()
    escaped = "".join(("\\" + c) if c in _ERE_META else c for c in lookup)
    return (f"\\b{escaped}\\b", lookup)


# The spawn seam (mirrors hive.tools.cli's Run/default_run shape): full child argv
# + an env mapping in, the completed process out — injectable so contract tests can
# observe or replace spawns without faking git itself.
Run = Callable[..., "subprocess.CompletedProcess[str]"]


def default_run(
    argv: Sequence[str],
    env: Optional[Mapping[str, str]] = None,
    timeout: Optional[float] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        env=None if env is None else dict(env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


class _SyncFault(RuntimeError):
    """An internal git/build fault whose message is ALREADY redacted."""


def _clean_env() -> dict[str, str]:
    """The hook-safe child env: matrix.gitenv (the ONE repo-discovery denylist owner)
    strips GIT_DIR-family vars so a ``git -C <mirror>`` child targets the mirror and
    never an inherited hook repo (BUG-034). Call-time import — the empty-registry
    path never loads matrix (byte-inert)."""
    from hive.matrix import gitenv  # noqa: PLC0415 — call-time by design

    env: dict[str, str] = gitenv.clean_git_env()
    return env


def authenticated_url(url: str, token: str) -> str:
    """The token-rewritten https remote (``https://x-access-token:<token>@host/…``).
    Non-https URLs and an empty token pass through unchanged. The rewritten form is
    handed ONLY to ``git clone`` (git persists it in the mirror's remote config) and
    must never be logged — ``_redact`` guards every escape path."""
    if not token or not url.startswith("https://"):
        return url
    return "https://x-access-token:" + token + "@" + url[len("https://") :]


def mirror_dirname(name: str, url: str) -> str:
    """The directory a repo's mirror lives in, ``<name>-<sha256(url)[:16]>``.

    The mirror is a cache of ONE remote, so its identity is (registry name, URL) —
    not the name alone. Binding the URL into the PATH makes reuse across a
    re-registration at a different URL unconstructable rather than merely checked
    (Law 2): a changed URL is a different directory, so the old checkout is a
    leftover the reconciliation prune reaps and the new one is a fresh clone.
    The name stays the prefix so mirrors remain greppable by registry name, and
    the result stays inside the prune's candidate grammar (_REPO_SLUG_RE) — a
    result outside it would make mirrors unreapable and silently undo BUG-050.

    Two registry rows naming the SAME url keep separate mirrors (the name is in
    the path), so no checkout is ever shared and the prune needs no refcount.
    """
    return f"{name}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]}"


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _shas(stdout: str) -> tuple[str, ...]:
    """The commit ids out of a ``--format=%H`` log, newest first. Filtered to full
    hex ids so a driver's stray output can never be mistaken for evidence."""
    return tuple(
        line
        for line in (stdout or "").split()
        if len(line) == 40 and all(c in "0123456789abcdef" for c in line)
    )


class _RegistryRow(Protocol):
    """The consumed surface of one repo-registry row (the store's ``RepoRow``
    shape) — the daemon reads exactly these four fields. Read-only properties so
    any row shape (frozen dataclass, test stub) conforms."""

    @property
    def name(self) -> str: ...

    @property
    def url(self) -> str: ...

    @property
    def canonical_ref(self) -> str: ...

    @property
    def token_env(self) -> str: ...


class SyncService:
    """Registry-driven mirrors + the poll tick + the ledger/mint/drift legs, one organ.

    ``store`` doubles as the registry/meta/drift seam and (through ``evidence``)
    the ledger door; ``lifecycle`` (optional) is the post-ingest promotion sweep
    handle; ``lock`` is THE global write lock (shared with both HTTP doors) and is
    held ONLY for store access — never across a fetch or a subprocess."""

    def __init__(
        self,
        cfg: SyncConfig,
        store: Any,
        evidence: ChangeEvidenceService,
        lock: threading.Lock,
        run: Run = default_run,
        now: Optional[Callable[[], int]] = None,
        *,
        lifecycle: Any = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._evidence = evidence
        self._lock = lock
        self._run = run
        self._now = now or (lambda: int(time.time()))
        self._lifecycle = lifecycle  # duck-typed: .promote_established() -> list[int]
        self.mirror_base = Path(cfg.mirror_dir or _DEFAULT_MIRROR_DIR)
        self._attrs_file = self._write_funcname_attributes()

    def _write_funcname_attributes(self) -> str:
        """Write git's shipped funcname drivers beside the mirrors, ONCE at init, and
        return the path ('' when it could not be written).

        Direction (Law 6): FAIL-OPEN. An unwritable file is logged once and tier 1
        still runs — it then resolves only for repos that declare their own drivers,
        and every anchor it cannot resolve degrades to ``unverifiable``, never to a
        verdict. The file is rewritten each boot so a driver added here reaches every
        deployment without an operator step."""
        path = self.mirror_base.parent / _ATTRIBUTES_FILE
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(_FUNCNAME_ATTRIBUTES, encoding="utf-8")
        except OSError as exc:
            _log.warning(
                "sync.funcname_attributes_unwritable path=%s error=%s", path, exc
            )
            return ""
        return str(path)

    # ── the poll loop ──────────────────────────────────────────────────────────
    def run_forever(self, stop: threading.Event, nudge: threading.Event) -> None:
        """Tick, then wait ``interval_s`` (ONE nudge wakes the wait early and the
        next tick covers ALL registered repos); ``stop`` ends the loop at the next
        wake. Every tick is already fail-open per repo per leg; the belt here
        guarantees even a tick-shell fault never kills the thread."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the loop survives everything
                self._note_error("tick", exc)
            nudge.wait(self._cfg.interval_s)
            nudge.clear()

    def tick(self) -> None:
        """ONE poll cycle: re-read the registry (a just-registered repo is picked up
        with no restart — D2), prune the mirrors of DEREGISTERED repos (the
        ``repo_remove`` promise — BUG-050), then run every repo under its OWN
        fail-open guard — one repo's fault never blocks another, and nothing
        raises past this method. Repos run SERIALLY at the default
        ``workers=1`` and concurrently through ``_repo_fanout`` above it; the
        aggregate verdict is identical either way, only the wall-clock differs.
        ``sync:last_sync_ts`` — stamped through the
        injected ``now`` seam — advances only when the prune AND every repo ran
        fault-free, so it reads as "the last fully-clean sync" against the
        sticky per-repo ``sync:<name>:last_error``."""
        try:
            with self._lock:
                rows = self._store.repo_registry()
        except Exception as exc:  # noqa: BLE001 — the shell fails open too
            self._note_error("registry", exc)
            return
        # Reconciliation BEFORE the inert early-return: deregistering the LAST
        # repo must still prune its leftover mirror. (A failed registry read
        # returned above — the live set is unknown, so nothing may be deleted.)
        clean = self._prune_mirrors({mirror_dirname(r.name, r.url) for r in rows})
        if not rows:
            # marker: spawning ANYTHING here (a clone, a fetch, an engine spawn)
            # or creating the mirrors dir reds CT-9's test_empty_registry_is_inert
            # — an empty registry tick stays inert: no git, no engine import, no
            # mirror dir made. The leftover-mirror prune above (pure local
            # filesystem, delete-only) is the ONE act allowed before this return.
            return
        if self._cfg.workers > 1 and len(rows) > 1:
            clean = self._repo_fanout(rows) and clean
        else:
            for row in rows:
                clean = self._repo_tick(row) and clean
        if clean:
            with self._lock:
                # marker: stamping this on a faulted tick reds
                # test_repo_fault_does_not_advance_last_sync_ts — a fault anywhere
                # means the sync did NOT complete, and the timestamp must not lie.
                self._store.meta_set(fleet_last_sync_ts_key(), str(self._now()))

    def _repo_fanout(self, rows: Sequence[_RegistryRow]) -> bool:
        """Tick every repo CONCURRENTLY, ``cfg.workers`` at a time. Opt-in only:
        the default ``workers=1`` never reaches here, so the serial loop above
        stays the shipped path byte-for-byte (§9 #9).

        Parallelizing is safe because a repo tick is ALREADY isolated by
        construction — its own mirror directory, its own token resolution, its own
        ``sync:<name>:last_error`` surface, its own tempdir per verify worktree —
        and every store touch inside it takes THE one global write lock, which is
        held for store access alone and never across a fetch or an engine spawn.
        Concurrency therefore widens no critical section; it only overlaps the
        waiting.

        Direction (Law 6): FAILS OPEN exactly like its serial twin. ``_repo_tick``
        already absorbs every per-leg fault; a worker that somehow raises past it
        is logged, surfaced on that repo's own error key, and counted unclean —
        never re-raised into the tick shell, and never able to strand the other
        repos. // O(repos / workers)"""
        clean = True
        with ThreadPoolExecutor(
            max_workers=min(self._cfg.workers, len(rows)),
            thread_name_prefix="hive-sync-repo",
        ) as pool:
            pending = {pool.submit(self._repo_tick, row): row for row in rows}
            for fut in as_completed(pending):
                try:
                    clean = fut.result() and clean
                except Exception as exc:  # noqa: BLE001 — the fanout fails open too
                    # marker: re-raising here (or dropping this guard) reds
                    # test_worker_fault_is_isolated_and_never_raises — one repo's
                    # worker fault must not take the tick or its siblings down.
                    self._note_repo_error(pending[fut].name, "tick", exc, "")
                    clean = False
        return clean

    # ── one repo, one guard ────────────────────────────────────────────────────
    def _repo_tick(self, row: _RegistryRow) -> bool:
        """Mirror + fetch, then the three legs, for ONE registry row. Returns True
        iff every leg ran fault-free; every fault is logged + recorded under THIS
        repo's ``sync:<name>:last_error`` and never raises past here.

        Two observability stamps ride this method for the health block: the RESOLVED
        tracked branch as soon as it is known (stamped even when a later leg faults —
        "which line do you think you track" is exactly what a fault needs answered),
        and this repo's own clean-tick timestamp when every leg ran fault-free."""
        token = ""
        try:
            token = self._resolve_token(row)
            mirror = self.ensure_mirror(row, token=token)
            branch = self._tracked_branch(mirror, row.canonical_ref)
            with self._lock:
                self._store.meta_set(tracked_ref_key(row.name), branch)
            prev_local = self._rev(mirror, f"refs/remotes/origin/{branch}")
            self._fetch(mirror)
        except Exception as exc:  # noqa: BLE001 — marker: re-raising breaks
            # test_unreachable_fail_open and CT-9's two-repo isolation (an
            # unreachable remote is a logged per-repo skip; the next tick retries,
            # the other repos and the serve path never feel it).
            self._note_repo_error(row.name, "mirror", exc, token)
            return False
        ok = True
        try:
            self._ledger_leg(row, mirror, branch, prev_local)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_repo_error(row.name, "ledger", exc, token)
            ok = False
        try:
            tip = self._rev(mirror, f"refs/remotes/origin/{branch}")
            if tip is not None:
                self._materialize_drift(row, mirror, tip)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_repo_error(row.name, "drift", exc, token)
            ok = False
        if ok:
            # marker: stamping this unconditionally reds
            # test_repo_fault_does_not_advance_its_own_last_sync_ts — the per-repo
            # twin of the tick shell's rule, and for the same reason: a faulted leg
            # means THIS repo did not sync, and its timestamp must not lie.
            with self._lock:
                self._store.meta_set(last_sync_ts_key(row.name), str(self._now()))
        return ok

    @staticmethod
    def _resolve_token(row: _RegistryRow) -> str:
        """Per-repo credential via env-var indirection (D2): the registry row names
        the var, never a secret byte. A row-named var ABSENT at tick time raises
        (the KeyError NAMES the var) into the per-repo fail-open guard — surfaced
        under ``sync:<name>:last_error``, the other repos untouched; the boot-time
        EX_CONFIG probe is the entrypoint's job. An unset row falls to the fleet
        default var; that one absent ⇒ anonymous (public/local remotes)."""
        if row.token_env:
            return os.environ[row.token_env]
        return os.environ.get(_DEFAULT_TOKEN_ENV, "")

    # ── registry reconciliation: a deregistered repo loses its mirror ──────────
    def _prune_mirrors(self, live_dirs: set[str]) -> bool:
        """Delete every mirror directory NO LONGER named by the live registry —
        ``repo_remove`` stops the feed; THIS is where the mirror goes away
        (BUG-050; ``repo_remove``'s docstring names this tick). ``live_dirs``
        holds DIRECTORY names (``mirror_dirname(name, url)``), not registry
        names, so a name re-registered against a different URL presents a
        different directory and its predecessor is reaped here like any other
        leftover — reuse across incarnations is unconstructable, not detected.
        Guard rails: the mirrors dir is never created (absent ⇒ no-op, so the
        empty-registry tick stays inert); only DIRECT children named by the
        registry slug grammar are candidates (anything else was never this
        daemon's mirror, so it is not ours to delete); a symlink, a
        non-directory, or a child resolving outside the base is skipped — never
        followed, never deleted — so nothing outside the configured mirrors dir
        can ever be rmtree'd; and each deletion runs under its own fail-open
        guard. Returns True iff every prune ran fault-free — a prune fault
        withholds ``sync:last_sync_ts`` exactly like a repo-leg fault."""
        try:
            children = sorted(self.mirror_base.iterdir())
        except OSError:  # absent (or unreadable) base: nothing to prune
            return True
        base = self.mirror_base.resolve()
        clean = True
        for child in children:
            name = child.name
            # marker: the live set and these children must be the SAME grammar.
            # Computing it from row.name instead of mirror_dirname(row.name,
            # row.url) reds test_reregistration_with_the_same_url_reuses_the_mirror
            # — no live directory would ever match, so every mirror is reaped and
            # re-cloned each tick; keying the mirror PATH by row.name reds
            # test_reregistration_with_a_new_url_never_feeds_the_old_remote — the
            # mirror would again be keyed by a name that outlives the repository
            # it mirrors.
            if name in live_dirs or _REPO_SLUG_RE.match(name) is None:
                continue
            try:
                if (
                    child.is_symlink()
                    or not child.is_dir()
                    or child.resolve().parent != base
                ):
                    continue  # never through a link, never outside the base
                shutil.rmtree(child)
                _log.info("sync.mirror_pruned repo=%s dir=%s", name, child)
            except Exception as exc:  # noqa: BLE001 — marker: raising here breaks
                # test_prune_fault_fails_open_tick_continues — a stuck leftover
                # is a logged per-name skip; the repos, the tick, and the serve
                # path never feel it.
                # The fault rides the tick-SHELL key, not a per-repo one (BUG-061):
                # `name` here is DEREGISTERED, so it has no health block, and a
                # per-repo key written under it is readable by nobody — the stuck
                # mirror would leak disk in total silence.
                self._note_error(f"prune[{name}]", exc)
                clean = False
        return clean

    # ── the mirror (a rebuildable cache — never the durable truth) ─────────────
    def ensure_mirror(self, repo: _RegistryRow, token: str = "") -> Path:
        """Clone ``repo`` at ``<mirror_dir>/<name>-<url-digest>/`` when absent or
        broken (a broken checkout is wiped and recloned — the mirror is a cache);
        a healthy mirror whose remote already matches the registry is a no-op. The
        directory name comes from ``mirror_dirname``, so it is a slug that cannot
        escape the base AND it pins WHICH repository this is a mirror of.

        The health probe alone answers "is this a git checkout?", never "of what?",
        so it is followed by a reconcile against the registry: the path already
        pins the repository, so a differing ``remote.origin.url`` can only be the
        CREDENTIAL and is rewritten in place (BUG-073 — a rotated token would
        otherwise stay frozen at the value ``git clone`` persisted). A remote that
        cannot be read at all is a broken cache and is rebuilt. The (possibly
        token-rewritten) URL lands ONLY in the mirror's remote config."""
        mirror = self.mirror_base / mirror_dirname(repo.name, repo.url)
        probe = self._run(
            ["git", "-C", str(mirror), "rev-parse", "--git-dir"], env=_clean_env()
        )
        if probe.returncode == 0:
            want = authenticated_url(repo.url, token)
            cfg = self._run(
                ["git", "-C", str(mirror), "config", "--get", "remote.origin.url"],
                env=_clean_env(),
            )
            have = (cfg.stdout or "").strip() if cfg.returncode == 0 else ""
            if have == want:
                return mirror
            if have:
                # marker: returning the mirror without this reconcile reds
                # test_a_rotated_token_is_reconciled_without_a_reclone — a rotated
                # credential would stay frozen at the value `git clone` persisted,
                # forever. Neither `want` nor `have` may reach a log line: they
                # carry the token.
                self._git(mirror, "remote", "set-url", "origin", want)
                return mirror
            # Empty/unreadable config: the checkout cannot prove what it is a
            # mirror OF, so it is a BROKEN cache — fall through to the rmtree +
            # reclone below. Direction (Law 6): fail toward REBUILD, never reuse.
        if mirror.exists():
            shutil.rmtree(mirror, ignore_errors=True)  # broken cache: rebuild whole
        mirror.parent.mkdir(parents=True, exist_ok=True)
        argv = ["git", "clone", "--quiet"]
        if repo.canonical_ref:
            argv += ["--branch", repo.canonical_ref]
        argv += [authenticated_url(repo.url, token), str(mirror)]
        proc = self._run(argv, env=_clean_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise _SyncFault(_redact(f"git clone failed: {detail}", token))
        _log.info("sync.mirror_cloned repo=%s dir=%s", repo.name, mirror)
        return mirror

    def _tracked_branch(self, mirror: Path, canonical_ref: str) -> str:
        """The registry row's canonical_ref when set, else the origin default
        branch (the clone-recorded ``origin/HEAD``)."""
        if canonical_ref:
            return canonical_ref
        proc = self._run(
            [
                "git",
                "-C",
                str(mirror),
                "symbolic-ref",
                "--short",
                "refs/remotes/origin/HEAD",
            ],
            env=_clean_env(),
        )
        name = (proc.stdout or "").strip()
        if proc.returncode == 0 and name:
            return name[len("origin/") :] if name.startswith("origin/") else name
        raise _SyncFault(
            "tracked branch unresolved: origin/HEAD is unset and "
            "the registry row names no canonical_ref"
        )

    def _fetch(self, mirror: Path) -> None:
        """The one fetch per repo per tick: ALL branches (force-updating — a
        rewritten remote line must still land locally), pruned — the drift
        materializer needs requested branch tips, not just the canonical line."""
        self._git(
            mirror,
            "fetch",
            "--quiet",
            "--prune",
            "origin",
            "+refs/heads/*:refs/remotes/origin/*",
        )

    # ── the ledger leg: watermark..tip → ONE post_merge receipt ────────────────
    def _ledger_leg(
        self, row: _RegistryRow, mirror: Path, branch: str, prev_local: Optional[str]
    ) -> frozenset[str]:
        """Advance ``repo``'s watermark by ONE receipt over ``watermark..tip``.

        Returns the repo-relative paths that range changed. Nothing downstream in this
        daemon consumes them any more — the drift leg asks git directly — but the
        census ingest reports them and they stay part of the leg's honest return, so a
        caller (today: the tests, tomorrow: any second consumer) can see what a range
        contained without re-deriving it."""
        tip = self._rev(mirror, f"refs/remotes/origin/{branch}")
        if tip is None:
            raise _SyncFault(f"tracked branch {branch!r} has no remote tip after fetch")
        # the app-seams watermark key — attach_drift resolves canonical tips from
        # EXACTLY this key, so any other format would silently blind recall drift
        tip_key = canonical_tip_key(row.name)
        with self._lock:
            watermark = self._meta_get(tip_key)
        # Base resolution: the durable watermark; else the mirror's own pre-fetch
        # view; else FIRST CONNECT — baseline at the remote tip, NO historical
        # receipt (marker: baselining at anything older mints history and reds the
        # no-historical-receipt assertion in every first-tick test).
        base = watermark or prev_local or tip
        if base != tip and not self._is_ancestor(mirror, base, tip):
            # A rewritten line (force-push): log the discontinuity, reset to the
            # last shared point, and record a DEFENSIVE receipt over
            # merge-base..tip — never a receipt over commits that no longer exist.
            merge_base = self._merge_base(mirror, base, tip)
            _log.warning(
                "sync.discontinuity repo=%s branch=%s (non-fast-forward "
                "remote; defensive receipt over the last shared point)",
                row.name,
                branch,
            )
            base = merge_base or tip
        if base == tip:
            if watermark != tip:
                with self._lock:
                    self._store.meta_set(tip_key, tip)
            return frozenset()  # no new commits ⇒ nothing to defer
        envelope = self._build_receipt(row, mirror, branch, base, tip)
        with self._lock:
            report = self._evidence.ingest(
                envelope, phase="post_merge", verdict="pass", signal="none"
            )
            # same critical section as the ingest: the watermark can never run
            # ahead of rows the serve path could observe
            self._store.meta_set(tip_key, tip)
            # the post-ingest promotion sweep (CT-8): a canonical ingest is the
            # verified-win carrier, so the established rung runs right behind it
            promoted = (
                self._lifecycle.promote_established()
                if self._lifecycle is not None
                else []
            )
        _log.info(
            "sync.ledger_ingested repo=%s base=%.12s head=%.12s matched=%d "
            "inserted=%d already=%d range_skipped=%s promoted=%d",
            row.name,
            base,
            tip,
            report.matched,
            len(report.inserted),
            report.already_recorded,
            report.range_skipped,
            len(promoted),
        )
        return report.touched_paths

    def _build_receipt(
        self, row: _RegistryRow, mirror: Path, branch: str, base: str, head: str
    ) -> dict[str, Any]:
        """ONE census receipt over base..head via the real CLI in a SUBPROCESS
        (process isolation absorbs the engines' process-global scratch pinning;
        repo code never runs in the server process), parsed back for the in-proc
        ingest. ``--repo-id`` is the REGISTRY NAME — the §3.6 exact-match join key
        against ``episode_anchors.repo``; ``--ref`` names the measured line
        explicitly (the mirror checkout can lag the fetch)."""
        with tempfile.TemporaryDirectory(prefix="hive-sync-receipt-") as tmp:
            out = Path(tmp) / "receipt.json"
            argv = [
                sys.executable,
                "-m",
                "hive.census.cli",
                "build",
                "--repo",
                str(mirror),
                "--base",
                base,
                "--head",
                head,
                "--repo-id",
                row.name,
                "--ref",
                branch,
                "--propagate",
                "--out",
                str(out),
            ]
            proc = self._run(argv, env=_clean_env())
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                raise _SyncFault(f"census build failed: {detail}")
            receipt: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
            return receipt

    # ── the drift materializer: (repo, tip, base_tip, anchor) -> wire verdicts ─
    def _materialize_drift(
        self, row: _RegistryRow, mirror: Path, canonical_tip: str
    ) -> None:
        """Materialize the ``anchor_drift`` cache for THIS repo, asking GIT.

        The work list is every non-retired anchor binding with the BASELINE COMMIT it
        is measured from (``store.anchor_work_list``); any binding still lacking one is
        filled first, at the tip the server can actually observe. Work is grouped by
        DISTINCT baseline, so the two plumbing reads (``ls-tree`` at the baseline,
        ``diff`` over the range) are paid once per baseline rather than once per anchor,
        and only a symbol anchor whose file actually moved costs a third call.

        Tips = the canonical tip (first — the served-most line is judged first), every
        ref a LIVE episode DECLARES (``store.declared_refs``), and every branch tip
        recall DEMANDED (``ref_requests`` within the demand window), deduped by
        resolved SHA. Already-materialized ``(tip, base_tip, anchor)`` rows are skipped
        — that is the cheap steady state, and it is why a quiet repo spawns nothing at
        all. Every DECLARED or DEMANDED ref that resolves is recorded into ``ref_tips``
        BEFORE any judging, so a tip stays KNOWN even if the judging faults (the reader
        then sees "tip known, verdicts absent" as ``unverifiable``, never a ``fresh``
        inherited from an older tip; BUG-063) and the retirement gate can resolve a
        memory's own declared tip before its verdicts land (BUG-064).

        Direction (Law 6): a probe fault isolates to ITS baseline group — those anchors
        materialize ``unverifiable`` while every other group still lands — and the
        fault is then re-raised so the per-repo guard records it and this repo's
        ``last_sync_ts`` does not lie about having synced."""
        name = row.name
        self._fill_missing_baselines(name, canonical_tip)
        with self._lock:
            work = self._store.anchor_work_list(name)
            probes = self._store.anchor_symbol_probes(name)
            declared = self._store.declared_refs(name)
        self._resolve_symbols_at_baseline(name, mirror, work, probes)
        anchors = sorted({anchor for _e, anchor, _b in work})
        base_tips = sorted({base for _e, _a, base in work})
        suspects = self._suspect_anchors(name, anchors)
        by_base: dict[str, list[str]] = {}
        for _eid, anchor, base in work:
            group = by_base.setdefault(base, [])
            if anchor not in group:
                group.append(anchor)
        now = int(self._now())
        tips = [canonical_tip]
        resolved_refs: list[tuple[str, str, str, int]] = []
        seen_refs: set[str] = set()
        for ref in (*declared, *self._requested_refs(name)):
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            sha = self._rev(mirror, f"refs/remotes/origin/{ref}")
            if sha is not None:
                resolved_refs.append((name, ref, sha, now))
                if sha not in tips:
                    tips.append(sha)  # a deleted/unknown ref is silently skipped
        if resolved_refs:
            # marker: skipping this write (or writing only for tips that were later
            # judged) reds test_unmaterialized_branch_tip_is_unverifiable_not_fresh
            # — a resolved-but-not-yet-judged branch tip must still be a KNOWN tip on
            # the next read, never fall back to an unknown one.
            with self._lock:
                self._store.ref_tips_put(resolved_refs)
        fault: Optional[BaseException] = None
        out: list[tuple[str, str, str, str, str, str, int]] = []
        for tip in tips:
            for base_tip, group in sorted(by_base.items()):
                with self._lock:
                    have = self._store.drift_get(
                        name, tip, dict.fromkeys(group, base_tip), group
                    )
                missing = [a for a in group if a not in have]
                if not missing:
                    continue
                rows, exc = self._verdicts_at(
                    mirror, tip, base_tip, missing, now, probes, suspects=suspects
                )
                out.extend((name, *r) for r in rows)
                fault = fault or exc
        if out:
            with self._lock:
                self._store.drift_put(out)
        with self._lock:
            # marker: dropping `base_tips` from the drift_get/drift_put key (or from
            # this prune) reds test_a_new_baseline_at_a_standing_tip_is_not_read_from_
            # the_old_row — the verdict is a function of the baseline, so a key
            # without it answers one question with another question's row.
            self._store.drift_prune(
                name, tips, keep_anchors=anchors, keep_base_tips=base_tips
            )
            # the ref_tips twin of the same bound: a ref that stopped being
            # declared, demanded, or resolvable leaves the work list, so its
            # watermark goes with it. ``resolved_refs`` is the exact set the tip
            # list was built from above — no second computation. Dropping a
            # watermark fails SAFE: the next read is unverifiable and the ref
            # re-resolves on the next tick.
            self._store.ref_tips_prune(
                name, keep_refs=[ref for _n, ref, _sha, _ts in resolved_refs]
            )
            # and the baseline twin: a retired memory's baseline leaves with its
            # anchor rather than outliving the binding it describes.
            self._store.anchor_baselines_prune(
                name, keep=[(eid, anchor) for eid, anchor, _b in work]
            )
        if fault is not None:
            raise fault

    def _fill_missing_baselines(self, repo: str, tip: str) -> int:
        """Record a baseline for every live binding that has none, at ``tip``.

        The ordinary baseline is written at WRITE time from the repo's watermark; this
        sweep covers the bindings written before their repo had one at all. It is a
        single insert-if-absent pass over the work list — no subprocess, no cap, and
        structurally incapable of moving a baseline that already exists. Returns how
        many were filled. Caller must NOT hold the lock."""
        with self._lock:
            work = self._store.anchor_work_list(repo)
        rows = [
            (eid, repo, anchor, tip, int(self._now()))
            for eid, anchor, base in work
            if not base
        ]
        if not rows:
            return 0
        with self._lock:
            filled = int(self._store.anchor_baseline_put(rows))
            if filled:
                self._bump_counter_locked(backfilled_total_key(repo), filled)
        if filled:
            _log.info(
                "sync.baselines_filled repo=%s anchors=%d tip=%.12s", repo, filled, tip
            )
        return filled

    def _resolve_symbols_at_baseline(
        self,
        repo: str,
        mirror: Path,
        work: Sequence[tuple[int, str, str]],
        probes: dict[tuple[str, str], int],
    ) -> None:
        """Measure, ONCE per symbol binding, whether its symbol existed at its own
        baseline — and persist the answer, updating ``probes`` in place.

        This is what keeps ``fresh`` a claim about something the server actually saw:
        without it a symbol that never existed reads ``fresh`` on every quiet tick,
        because "the file is not in the diff" says nothing about a name inside it. The
        baseline is an immutable commit, so the answer can never change; a row is asked
        once, written once, and never re-asked — which is why a QUIET repo (no new
        bindings) spawns nothing here at all, and why the steady-state cost model is
        untouched.

        Direction (Law 6): a git answer we cannot read leaves the row UNPROBED, and an
        unprobed row materializes no verdict at all (``_verdicts_at``) rather than a
        guessed one. An UNASKABLE binding — one whose stored spelling cannot even be
        handed to git — is the same case as an unanswerable one, and is treated
        identically here: the row stays unprobed, materializes nothing, and the repo's
        other bindings are unaffected. Caller must NOT hold the lock.
        // O(new symbol bindings)."""
        pending: list[tuple[str, str, str, str]] = []  # (base, anchor, path, symbol)
        for _eid, anchor, base in work:
            key = (base, anchor)
            if not base or probes.get(key, _SYMBOL_UNPROBED) != _SYMBOL_UNPROBED:
                continue
            path, symbol = probe_target(anchor)
            if not symbol or any(p[:2] == key for p in pending):
                continue  # file-scoped anchors ask nobody; one probe per key
            pending.append((base, anchor, path, symbol))
        rows: list[tuple[str, str, int]] = []
        for base, anchor, path, symbol in pending:
            try:
                resolved = self._resolves_at(mirror, path, symbol, base)
            except Exception:  # noqa: BLE001 — marker: removing this guard reds
                # tests/contract/test_minimal_hardening_e2e.py::
                # test_one_unprobeable_binding_does_not_fault_the_repo_leg — one stored
                # row whose argv git cannot even be handed would abort the whole repo's
                # drift leg, freezing every OTHER anchor's cached verdict at its last
                # value (false-fresh).
                _log.warning("sync.binding_unprobeable repo=%s anchor=%r", repo, anchor)
                continue
            if resolved is None:
                continue  # git could not say — stay unprobed, claim nothing
            state = _SYMBOL_RESOLVED if resolved else _SYMBOL_ABSENT
            probes[(base, anchor)] = state
            rows.append((base, anchor, state))
        if not rows:
            return
        with self._lock:
            self._store.anchor_symbol_ok_put(repo, rows)
        _log.info("sync.symbols_resolved repo=%s bindings=%d", repo, len(rows))

    def _resolves_at(
        self, mirror: Path, path: str, symbol: str, rev: str
    ) -> Optional[bool]:
        """Did ``symbol`` resolve inside ``path`` at the single commit ``rev``?
        ``None`` = git said something we cannot read as either answer.

        ``git blame -L :<funcname>`` is the point form of the same funcname machinery the
        range tier uses, asked of ONE tree — and keeping it a DIFFERENT subcommand is the
        design, not an accident. "Does this symbol exist at this commit" and "what
        changed between these commits" are different questions; a range walk pinned to a
        single revision only imitates the first. The separation is also what keeps the
        tick's cost model readable: a ``log -L`` in the observed argv means tier 1 ran,
        and nothing else. Folding this back into ``log -L`` would merge two questions
        into one counter and silently destroy that reading.

        It runs through the same two-step ``symbol_lookups`` and the same attributes
        file, so a spelling that resolves for the range tier resolves here too, and both
        report the same distinguishable no-match / no-path failures. // O(lookups)."""
        unresolved = False
        for lookup in symbol_lookups(symbol):
            for pattern in _funcname_patterns(lookup):
                proc = self._blame_symbol(mirror, pattern, path, rev)
                if proc.returncode == 0:
                    return True
                detail = (proc.stderr or "").strip()
                if _BLAME_NO_PATH in detail:
                    return False  # the file was not there, so neither was the symbol
                unresolved = unresolved or _L_NO_MATCH in detail
        return False if unresolved else None

    # ── the probe: what git says about one baseline, one path, one symbol ──────
    def _verdicts_at(
        self,
        mirror: Path,
        tip: str,
        base_tip: str,
        anchors: Sequence[str],
        now: int,
        probes: Mapping[tuple[str, str], int],
        *,
        suspects: frozenset[str] = frozenset(),
    ) -> tuple[list[tuple[str, str, str, str, str, int]], Optional[BaseException]]:
        """``(tip, base_tip, anchor, verdict, detail, ts)`` rows for ONE baseline
        group, plus the probe fault that degraded them (None when clean).

        The cache stores MEASUREMENTS only. A group with no baseline, one whose probe
        faulted, and an anchor whose symbol has not been resolved at its baseline yet
        all write NO row: absence already reads ``unverifiable`` — the same served
        answer — and a cached row would be indistinguishable from a measured verdict,
        so the presence-is-the-skip-predicate steady state would freeze the degradation
        at that tip until the tip moved again."""
        if not base_tip:
            return [], None
        try:
            tree, status_by_path = self._probe_baseline(mirror, base_tip, tip)
        except Exception as exc:  # noqa: BLE001 — one group's fault, not the repo's
            return [], exc
        rows: list[tuple[str, str, str, str, str, int]] = []
        for anchor in anchors:
            path, symbol = probe_target(anchor)
            resolved = probes.get((base_tip, anchor), _SYMBOL_UNPROBED)
            if symbol and resolved == _SYMBOL_UNPROBED:
                continue  # unmeasured: absence is the honest answer, a row would stick
            facts = AnchorFacts(
                base_tip=base_tip,
                in_base_tree=path in tree,
                status=status_by_path.get(path, ""),
                symbol_resolved_at_base=resolved == _SYMBOL_RESOLVED,
            )
            if (
                facts.in_base_tree
                and facts.status
                and symbol
                and facts.symbol_resolved_at_base
            ):
                facts = self._probe_symbol(
                    mirror, path, symbol, base_tip, tip, status=facts.status
                )
            verdict, detail = decide(facts, symbol=symbol)
            if verdict == FRESH and anchor in suspects:
                # the anchor itself is intact, but the census watched a breaking
                # change propagate into its neighbourhood — the advisory tier, which
                # only ever makes a verdict MORE severe than the measurement
                verdict = BLAST_RADIUS_CHANGED
                detail = {"since": base_tip, "reason": "stale_suspect"}
            if verdict in (ANCHOR_CHANGED, ANCHOR_MISSING) and not detail["n_commits"]:
                # a stale verdict owes the agent the commits that carried it; the
                # symbol tier already has them, so this pays only for the file tier
                # and for a symbol whose own resolution is what went missing
                facts = dataclasses.replace(
                    facts,
                    path_commits=self._probe_commits(mirror, path, base_tip, tip),
                )
                verdict, detail = decide(facts, symbol=symbol)
            rows.append(
                (
                    tip,
                    base_tip,
                    anchor,
                    verdict,
                    json.dumps(detail, separators=(",", ":")),
                    now,
                )
            )
        return rows, None

    def _probe_baseline(
        self, mirror: Path, base_tip: str, tip: str
    ) -> tuple[frozenset[str], dict[str, str]]:
        """The two reads every anchor in one baseline group shares: the paths that
        existed AT the baseline, and what ``base..tip`` did to each.

        Both are read-only object-database reads — no worktree, no checkout, no
        ``reset --hard``. A rename retires the OLD path and adds the new one; a COPY
        adds its destination and leaves the source alone, which is why the two are not
        folded together. Raises ``_SyncFault`` on either call failing."""
        tree = frozenset(
            line
            for line in self._git(
                mirror, "ls-tree", "-r", "--name-only", base_tip
            ).splitlines()
            if line
        )
        status: dict[str, str] = {}
        for line in self._git(
            mirror, "diff", "--name-status", f"{base_tip}..{tip}"
        ).splitlines():
            fields = line.split("\t")
            if len(fields) < 2 or not fields[0]:
                continue
            code = fields[0]
            if code[:1] == "R" and len(fields) >= 3:
                status[fields[1]] = code  # the old path departed
                status[fields[2]] = "A"
            elif code[:1] == "C" and len(fields) >= 3:
                status[fields[2]] = "A"  # a copy leaves its source untouched
            else:
                status[fields[1]] = code
        return tree, status

    def _probe_symbol(
        self,
        mirror: Path,
        path: str,
        symbol: str,
        base_tip: str,
        tip: str,
        *,
        status: str,
    ) -> AnchorFacts:
        """Tier 1: did THIS symbol's own lines move between the baseline and the tip?

        The symbol is resolved through the two-step ``symbol_lookups`` — the spelling
        as written, then its last dotted segment — because git's funcname matches the
        DECLARATION line, so a bare method name resolves where a class-qualified one
        does not. Without the second step every corrected `path::Class.method` anchor
        in the live store would turn into ``anchor_missing``: a FALSE stale, the one
        direction this system must never fail.

        // marker: dropping the second lookup from ``symbol_lookups`` reds
        test_both_method_spellings_resolve.

        Reached only for a symbol the baseline sweep already resolved, so every arm
        here carries ``symbol_resolved_at_base=True`` EXCEPT the ``no match`` one: that
        arm re-asks the baseline question with the tip probe's own configuration, so a
        transient inability to resolve anything at all (a missing funcname driver) reads
        as the unknown rather than as a deletion. Only a symbol this tick can still
        resolve at the baseline, and can no longer resolve at the tip, is missing."""
        patterns = tuple(
            pattern
            for lookup in symbol_lookups(symbol)
            for pattern in _funcname_patterns(lookup)
        )
        unresolved = False
        for pattern in patterns:
            proc = self._log_symbol(mirror, pattern, path, f"{base_tip}..{tip}")
            if proc.returncode == 0:
                return AnchorFacts(
                    base_tip=base_tip,
                    in_base_tree=True,
                    status=status,
                    symbol_commits=_shas(proc.stdout),
                    symbol_resolved_at_base=True,
                )
            detail = (proc.stderr or "").strip()
            if _L_NO_PATH in detail:
                # the file itself is gone at the tip — provable absence, and the
                # ladder reaches anchor_missing through the ordinary departed-path arm
                return AnchorFacts(
                    base_tip=base_tip,
                    in_base_tree=True,
                    status="D",
                    symbol_resolved_at_base=True,
                )
            unresolved = unresolved or _L_NO_MATCH in detail
        if not unresolved:
            # nothing git said was "this symbol is not here" — an unexpressible
            # spelling, or a fault we cannot read. Neither is evidence.
            return AnchorFacts(
                base_tip=base_tip, in_base_tree=True, status=status, probe_failed=True
            )
        resolved_at_base = any(
            self._log_symbol(mirror, pattern, path, base_tip, limit=True).returncode
            == 0
            for pattern in patterns
        )
        return AnchorFacts(
            base_tip=base_tip,
            in_base_tree=True,
            status=status,
            symbol_resolved_at_tip=False,
            symbol_resolved_at_base=resolved_at_base,
        )

    def _suspect_anchors(self, repo: str, live: Sequence[str]) -> frozenset[str]:
        """The anchors of ``repo`` sitting in a breaking change's BLAST RADIUS.

        The census already writes advisory ``stale_suspect`` evidence when a
        propagation neighbour of a breaking seed matches a memory's binding; this
        reads the newest such row per episode and keeps the ones whose subject is a
        live anchor here. It is the producer of ``blast_radius_changed``: the anchor's
        OWN lines did not move (the ladder said ``fresh``), but something it depends
        on did.

        Direction (Law 6): fail-OPEN to the empty set. A neighbourhood signal that
        cannot be read degrades the verdict to the anchor's own tier — an
        under-claim — never to a stale verdict nobody measured. Caller must NOT hold
        the lock. // O(rows)."""
        wanted = set(live)
        if not wanted:
            return frozenset()
        try:
            with self._lock:
                rows = self._store.stale_suspect_rows()
        except Exception:  # noqa: BLE001 — an advisory signal never breaks the leg
            _log.debug("sync.stale_suspect_read_failed repo=%s", repo, exc_info=True)
            return frozenset()
        out: set[str] = set()
        for _eid, _ts, payload in rows:
            try:
                parsed = json.loads(payload)
            except (TypeError, ValueError):
                continue
            matched = parsed.get("matched") if isinstance(parsed, dict) else None
            if not isinstance(matched, dict):
                continue
            path, symbol = matched.get("path"), matched.get("symbol")
            if not isinstance(path, str) or not path:
                continue
            anchor = f"{path}{SYMBOL_SEP}{symbol}" if symbol else path
            if anchor in wanted:
                out.add(anchor)
        return frozenset(out)

    def _probe_commits(
        self, mirror: Path, path: str, base_tip: str, tip: str
    ) -> tuple[str, ...]:
        """The file tier's evidence: the commits in ``base..tip`` that touched
        ``path``, newest first. Fault ⇒ no evidence, never a changed verdict — the
        verdict is already decided by the time this is asked."""
        proc = self._run(
            [
                "git",
                "-C",
                str(mirror),
                "log",
                "--format=%H",
                f"{base_tip}..{tip}",
                "--",
                path,
            ],
            env=_clean_env(),
        )
        return _shas(proc.stdout) if proc.returncode == 0 else ()

    def _log_symbol(
        self, mirror: Path, pattern: str, path: str, rev: str, *, limit: bool = False
    ) -> "subprocess.CompletedProcess[str]":
        """One ``git log -L ':<funcname>:<path>'``, with the funcname drivers injected
        through ``core.attributesFile`` — git's LOWEST-precedence attribute source, so
        a repo's own ``.gitattributes`` still wins."""
        argv = ["git"]
        if self._attrs_file:
            argv += ["-c", f"core.attributesFile={self._attrs_file}"]
        argv += ["-C", str(mirror), "log", "-s", "--format=%H"]
        if limit:
            argv.append("-1")
        argv += ["-L", f":{pattern}:{path}", rev]
        return self._run(argv, env=_clean_env())

    def _blame_symbol(
        self, mirror: Path, pattern: str, path: str, rev: str
    ) -> "subprocess.CompletedProcess[str]":
        """One ``git blame -L ':<funcname>' <rev> -- <path>`` — the point form of the
        same funcname resolution, asked of ONE commit rather than a range, with the
        drivers injected the same way. ``rev`` is a commit, so the answer is immutable
        and this is asked once per binding, ever."""
        argv = ["git"]
        if self._attrs_file:
            argv += ["-c", f"core.attributesFile={self._attrs_file}"]
        argv += ["-C", str(mirror), "blame", "-L", f":{pattern}", rev, "--", path]
        return self._run(argv, env=_clean_env())

    def _requested_refs(self, name: str) -> list[str]:
        """The demand half of the work list: branch refs recall asked drift for
        within the 7-day window. The cutoff anchors on the DEMAND clock (the
        newest touch) capped by the service clock — touches are stamped by the
        serving container's clock, which tests pin far behind wall time; anchoring
        on the newer of the two would silently expire the whole list under a
        lagging demand clock (the fail-safe direction is to materialize, never to
        starve coverage). Refs whose last demand is > 7d older than the newest
        demand age out."""
        with self._lock:
            row = self._store.conn.execute(
                "SELECT MAX(last_requested_ts) AS newest FROM ref_requests "
                "WHERE repo=?",
                (name,),
            ).fetchone()
            newest = row["newest"] if row is not None else None
            if newest is None:
                return []
            cutoff = min(int(self._now()), int(newest)) - _REF_REQUEST_WINDOW_S
            refs: list[str] = self._store.requested_refs(name, cutoff)
            return refs

    # ── narrow git/store helpers ───────────────────────────────────────────────
    def _git(self, mirror: Path, *args: str) -> str:
        proc = self._run(["git", "-C", str(mirror), *args], env=_clean_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise _SyncFault(f"git {args[0]} failed: {detail}")
        return proc.stdout

    def _rev(self, mirror: Path, ref: str) -> Optional[str]:
        proc = self._run(
            ["git", "-C", str(mirror), "rev-parse", "--verify", "--quiet", ref],
            env=_clean_env(),
        )
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _is_ancestor(self, mirror: Path, ancestor: str, descendant: str) -> bool:
        proc = self._run(
            [
                "git",
                "-C",
                str(mirror),
                "merge-base",
                "--is-ancestor",
                ancestor,
                descendant,
            ],
            env=_clean_env(),
        )
        return proc.returncode == 0

    def _merge_base(self, mirror: Path, a: str, b: str) -> Optional[str]:
        proc = self._run(
            ["git", "-C", str(mirror), "merge-base", a, b], env=_clean_env()
        )
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _meta_get(self, key: str) -> Optional[str]:
        # meta READS are raw SQL at the driving-adapter boundary (the healthcheck
        # idiom); callers hold the global lock — the conn is shared, not thread-safe.
        row = self._store.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def _bump_counter_locked(self, key: str, by: int = 1) -> None:
        """Add ``by`` to an integer ``sync:*`` meta counter. Caller holds the lock."""
        raw = self._meta_get(key)
        count = int(raw) if raw and raw.isdigit() else 0
        self._store.meta_set(key, str(count + by))

    def _note_error(self, leg: str, exc: BaseException) -> None:
        """Fail-open surfacing for tick-SHELL faults (no repo in scope): logged and
        recorded under the fleet ``sync:last_error`` — never raised past the tick.
        That key is SERVED, in the health report's ``fleet`` block: a registry read
        that fails returns before any per-repo key is written or cleared, so this is
        the ONLY in-band statement that the daemon is down (BUG-062)."""
        message = f"{leg}: {type(exc).__name__}: {exc}"[:500]
        _log.warning("sync.tick_failed leg=%s error=%s", leg, message)
        try:
            with self._lock:
                self._store.meta_set(fleet_last_error_key(), message)
        except Exception:  # noqa: BLE001 — surfacing must never become the fault
            _log.warning("sync.last_error_write_failed leg=%s", leg)

    def _note_repo_error(
        self, name: str, leg: str, exc: BaseException, token: str
    ) -> None:
        """Per-repo fail-open surfacing: the fault is logged (redacted) and
        recorded under ``sync:<name>:last_error`` — the OTHER repos' keys and the
        tick itself are untouched."""
        message = _redact(f"{leg}: {type(exc).__name__}: {exc}", token)[:500]
        _log.warning("sync.repo_leg_failed repo=%s leg=%s error=%s", name, leg, message)
        try:
            with self._lock:
                self._store.meta_set(last_error_key(name), message)
        except Exception:  # noqa: BLE001 — surfacing must never become the fault
            _log.warning("sync.last_error_write_failed repo=%s leg=%s", name, leg)


def start_sync(
    cfg: Config,
    store: Any,
    evidence: ChangeEvidenceService,
    lock: threading.Lock,
    *,
    lifecycle: Any = None,
) -> threading.Thread:
    """Arm the sync daemon from the ROOT config (reads ``cfg.sync`` only — WHICH
    repos to feed is the store registry's, re-read every tick). ALWAYS starts the
    thread: an empty registry is an inert tick, not an unarmed daemon, so
    registering the first repo needs no restart. The returned daemon thread
    carries its control events as ``sync_stop`` / ``sync_nudge`` attributes (the
    webhook nudge door and tests reach the loop through them) plus the service
    itself as ``sync_service``. ``lifecycle`` (optional) wires the post-ingest
    promotion sweep."""
    sync_cfg: SyncConfig = cfg.sync
    service = SyncService(sync_cfg, store, evidence, lock, lifecycle=lifecycle)
    stop, nudge = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=service.run_forever, args=(stop, nudge), name="hive-sync", daemon=True
    )
    thread.sync_stop = stop  # type: ignore[attr-defined]
    thread.sync_nudge = nudge  # type: ignore[attr-defined]
    thread.sync_service = service  # type: ignore[attr-defined]
    thread.start()
    _log.info(
        "sync.armed interval_s=%d workers=%d mirrors=%s",
        sync_cfg.interval_s,
        sync_cfg.workers,
        service.mirror_base,
    )
    return thread
