"""The server-side census sync daemon — a DRIVING adapter over the ledger door.

One daemon thread (armed by ``HIVE_SYNC__REPO_URL``, started by the entrypoint only
AFTER serve-ready) keeps a local git mirror of the configured repo and feeds
tracked-branch movement into the change-outcome evidence ledger: fetch → ONE
unsigned receipt per contiguous ``watermark..tip`` range, built by the
battle-tested ``python -m hive.census.cli build`` in a SUBPROCESS (engine
machinery and repo code never enter this process) → in-proc ingest under the ONE
global write lock (phase post_merge, verdict pass, signal none — exactly the
post-merge hook's parity) → watermark advance in the same critical section.

Named safe directions (Law 6):
- UNARMED IS INERT: ``start_sync`` returns None when ``repo_url`` is unset — no
  thread, no clone, no census/matrix import; the serve envelope is byte-identical
  to a build without this module.
- THE SIDE-CHANNEL FAILS OPEN, THE SERVE NEVER DOES: every tick leg catches its own
  faults (log + ``sync:last_error`` meta); an unreachable remote or a broken build
  skips a tick and the next tick retries — nothing here can take the serve down.
- THE WATERMARK IS THE DURABLE TRUTH (``sync:last_tip`` store meta); the mirror is
  a rebuildable cache — losing it can never open a coverage gap, and a crash
  between ingest and watermark advance is absorbed by the range ledger's
  exact-key dedupe.
- CREDENTIALS STAY IN THE REMOTE CONFIG: the token rides ONLY the rewritten remote
  URL inside the mirror's git config; every log line and every ``sync:last_error``
  value passes ``_redact`` first.
- TRUST-UNTOUCHED (O7): rows land through the same ChangeEvidenceService door as
  ``hive ingest`` — no trust handle exists here.
- REPO CODE RUNS ONLY IN CHILDREN: the candidate leg executes the PR tree's own
  tests, so every candidate spawn gets a STRIPPED env (clean git env, every
  ``HIVE_*`` var dropped, the token never rides env at all) and is killed at a
  constant timeout — the serve process never imports or executes candidate code.
- CANDIDATE ROWS NEVER IMPERSONATE THE TRACKED LINE: every pre-merge row is
  stamped with ``--ref refs/pull/N/head``, so a canonical-scoped
  ``last_verified`` read structurally cannot serve a PR-head verification.
- THE BACKFILL NEVER OVERWRITES: minted fp keys land through the store's
  absent-only merge (a present key structurally cannot be displaced), so a
  tagged episode is untouched byte-for-byte and text/hash/vector never move.

The candidate leg evaluates CHANGED PR heads only (``refs/sync/pr/*`` against the
``sync:pr_heads`` baseline): first connect baselines every existing head WITHOUT
evaluating, at most five changed heads run per tick (the rest carry over), an
exact ``(repo, merge-base, head, pre_merge)`` range already in the ledger is
skipped before any build, and a nothing-decided receipt is REFUSED by the
pre-merge derivation — logged and counted, never a fault.

The backfill leg sweeps approved code-anchored episodes whose meta lacks
``combdrift/fp`` and mints the absent fingerprint keys server-side through the
REAL ``hive-edge`` CLI subprocess against the mirror synced to the tracked tip —
one mint owner fleet-wide, so server-backfilled keys stay byte-equal to
edge-minted ones. An unresolvable anchor mints ``{}`` and is skipped silently;
at most ``_BACKFILL_PER_TICK`` mints run per tick, the rest carry over.

``tick()`` runs the LEDGER, CANDIDATE, and BACKFILL legs, each under its own
fail-open guard. The returned thread carries its control events (``sync_stop`` /
``sync_nudge``) so the webhook nudge door can wake the loop without touching this
module's internals.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from hive.app.config import SyncConfig
from hive.domain.change_evidence import ChangeEvidenceService, ReceiptRefused

_log = logging.getLogger("hive.sync")

META_LAST_TIP = "sync:last_tip"      # the durable watermark (store meta = truth, D4)
META_LAST_ERROR = "sync:last_error"  # the last surfaced fault (redacted, advisory)
META_TRACKED_REF = "sync:tracked_ref"    # the resolved tracked line (operator surface)
META_LAST_SYNC_TS = "sync:last_sync_ts"  # ts of the last fault-free tick (via the now seam)
META_PR_HEADS = "sync:pr_heads"      # {pr-number: head-sha} — the candidate baseline
META_CANDIDATES_EVALUATED = "sync:candidates_evaluated"  # completed evaluations
META_CANDIDATES_REFUSED = "sync:candidates_refused"      # nothing-decided receipts
META_BACKFILLED_TOTAL = "sync:backfilled_total"          # episodes fp-backfilled, ever
_DEFAULT_MIRROR_DIR = "/data/sync/mirror"
_PR_REFSPEC = "+refs/pull/*/head:refs/sync/pr/*"
_CANDIDATES_PER_TICK = 5             # changed-head cap per tick; the rest carry over
_CANDIDATE_TIMEOUT_S = 600           # the ONE bound on uv-sync / candidate / mint spawns
_BACKFILL_PER_TICK = 50              # fp-mint cap per tick; the rest carry over
_FP_KEY = "combdrift/fp"             # the sweep key: absent ⇒ the episode needs a mint

# The spawn seam (mirrors hive.tools.cli's Run/default_run shape): full child argv
# + an env mapping in, the completed process out — injectable so contract tests can
# observe or replace spawns without faking git itself.
Run = Callable[..., "subprocess.CompletedProcess"]


def default_run(argv: Sequence[str], env: Optional[Mapping[str, str]] = None,
                timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), env=None if env is None else dict(env),
                          capture_output=True, text=True, timeout=timeout)


class _SyncFault(RuntimeError):
    """An internal git/build fault whose message is ALREADY redacted."""


def _clean_env() -> dict[str, str]:
    """The hook-safe child env: matrix.gitenv (the ONE repo-discovery denylist owner)
    strips GIT_DIR-family vars so a ``git -C <mirror>`` child targets the mirror and
    never an inherited hook repo. Call-time import — the unarmed path never loads
    matrix (byte-inert)."""
    from matrix import gitenv  # noqa: PLC0415 — call-time by design
    return gitenv.clean_git_env()


def authenticated_url(url: str, token: str) -> str:
    """The token-rewritten https remote (``https://x-access-token:<token>@host/…``).
    Non-https URLs and an empty token pass through unchanged. The rewritten form is
    handed ONLY to ``git clone`` (git persists it in the mirror's remote config) and
    must never be logged — ``_redact`` guards every escape path."""
    if not token or not url.startswith("https://"):
        return url
    return "https://x-access-token:" + token + "@" + url[len("https://"):]


def normalized_repo_id(url: str) -> str:
    """The receipt-facing repo identity: the configured URL minus any userinfo
    credential, trailing slashes, and a trailing ``.git`` — a stable, credential-free
    key (it rides every payload row and the range ledger, so it must never carry the
    token and must not fork on cosmetic URL variants)."""
    base = url.strip()
    if "://" in base:
        scheme, _, rest = base.partition("://")
        authority, sep, path_part = rest.partition("/")
        authority = authority.rpartition("@")[2]        # strip user[:credential]@
        base = scheme + "://" + authority + (sep + path_part if sep else "")
    base = base.rstrip("/")
    if base.endswith(".git"):
        base = base[: -len(".git")]
    return base


class SyncService:
    """Mirror management + the poll tick + the ledger feed, one organ.

    ``store`` doubles as the meta-watermark seam and (through ``evidence``) the
    ledger door; ``lock`` is THE global write lock (shared with both HTTP doors) and
    is held ONLY for store access — never across a fetch or a subprocess build."""

    def __init__(self, cfg: SyncConfig, store, evidence: ChangeEvidenceService,
                 lock: threading.Lock, run: Run = default_run,
                 now: Optional[Callable[[], int]] = None, *,
                 canonical_ref: str = "") -> None:
        self._cfg = cfg
        self._store = store
        self._evidence = evidence
        self._lock = lock
        self._run = run
        self._now = now or (lambda: int(time.time()))
        self._canonical_ref = canonical_ref      # census.canonical_ref: the tracked line
        self.mirror_dir = cfg.mirror_dir or _DEFAULT_MIRROR_DIR
        # per-lock-hash uv venvs, beside the mirror (volume-local, rebuildable cache)
        self._env_cache_dir = str(Path(self.mirror_dir).parent / "candidate-envs")
        # the mint CLI's own state home, beside the mirror too (volume-local cache;
        # pinned explicitly so the daemon never writes an operator's home dir)
        self._edge_home = str(Path(self.mirror_dir).parent / "edge-home")
        # hive-edge ships in the image beside the server interpreter; PATH is the
        # fallback for layouts that install it elsewhere
        _edge = Path(sys.executable).parent / "hive-edge"
        self._edge_cli = str(_edge) if _edge.exists() else "hive-edge"
        # verify_candidates=False ⇒ the leg holds NO handle (the verified_reader
        # idiom: OFF is unreachable, not merely idle).
        self._candidate_leg: Optional[Callable[[str], None]] = (
            self._evaluate_candidates if cfg.verify_candidates else None)

    # ── the poll loop ──────────────────────────────────────────────────────────
    def run_forever(self, stop: threading.Event, nudge: threading.Event) -> None:
        """Tick, then wait ``interval_s`` (a nudge wakes the wait early); ``stop``
        ends the loop at the next wake. Every tick is already fail-open per leg; the
        belt here guarantees even a tick-shell fault never kills the thread."""
        while not stop.is_set():
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 — the loop survives everything
                self._note_error("tick", exc)
            nudge.wait(self._cfg.interval_s)
            nudge.clear()

    def tick(self) -> None:
        """ONE poll cycle: mirror + fetch first (shared by every leg), then each leg
        under its OWN fail-open guard — one leg's fault never kills the others, and
        nothing raises past this method toward the caller. Two operator-surface meta
        keys ride the tick (the census-health sync block serves store meta ONLY):
        ``sync:tracked_ref`` the moment the tracked line is resolved, and
        ``sync:last_sync_ts`` — stamped through the injected ``now`` seam — only when
        EVERY leg ran fault-free, so it reads as "the last fully-clean sync" against
        the sticky ``sync:last_error``."""
        try:
            self.ensure_mirror()
            branch = self._tracked_branch()
            with self._lock:
                self._store.meta_set(META_TRACKED_REF, branch)
            prev_local = self._rev(f"refs/remotes/origin/{branch}")
            self._fetch(branch)
        except Exception as exc:  # noqa: BLE001 — marker: re-raising breaks
            # test_unreachable_fail_open (an unreachable remote is a logged skip;
            # the next tick retries, the serve path never feels it).
            self._note_error("mirror", exc)
            return
        clean = True
        try:
            self._ledger_leg(branch, prev_local)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_error("ledger", exc)
            clean = False
        if self._candidate_leg is not None:
            try:
                self._candidate_leg(branch)
            except Exception as exc:  # noqa: BLE001 — the leg fails open too
                self._note_error("candidate", exc)
                clean = False
        try:
            tip = self._rev(f"refs/remotes/origin/{branch}")
            if tip is not None:
                self._backfill(tip, branch)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_error("backfill", exc)
            clean = False
        if clean:
            with self._lock:
                # marker: stamping this on a faulted tick reds
                # test_ledger_fault_does_not_advance_last_sync_ts — a fault anywhere
                # means the sync did NOT complete, and the timestamp must not lie.
                self._store.meta_set(META_LAST_SYNC_TS, str(self._now()))

    # ── the mirror (a rebuildable cache — never the durable truth) ─────────────
    def ensure_mirror(self) -> None:
        """Clone the tracked repo at ``mirror_dir`` when absent or broken (a broken
        checkout is wiped and recloned — the mirror is a cache); a healthy mirror is
        a no-op. The checkout is the tracked branch when census names one, else the
        origin default; the (possibly token-rewritten) URL lands ONLY in the clone's
        remote config."""
        probe = self._run(["git", "-C", self.mirror_dir, "rev-parse", "--git-dir"],
                          env=_clean_env())
        if probe.returncode == 0:
            return
        mirror = Path(self.mirror_dir)
        if mirror.exists():
            shutil.rmtree(mirror, ignore_errors=True)   # broken cache: rebuild whole
        mirror.parent.mkdir(parents=True, exist_ok=True)
        argv = ["git", "clone", "--quiet"]
        if self._canonical_ref:
            argv += ["--branch", self._canonical_ref]
        argv += [authenticated_url(self._cfg.repo_url, self._cfg.token),
                 self.mirror_dir]
        proc = self._run(argv, env=_clean_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise _SyncFault(self._redact(f"git clone failed: {detail}"))
        _log.info("sync.mirror_cloned dir=%s", self.mirror_dir)

    def _tracked_branch(self) -> str:
        """census.canonical_ref when set, else the origin default branch (the
        clone-recorded ``origin/HEAD``)."""
        if self._canonical_ref:
            return self._canonical_ref
        proc = self._run(["git", "-C", self.mirror_dir, "symbolic-ref", "--short",
                          "refs/remotes/origin/HEAD"], env=_clean_env())
        name = (proc.stdout or "").strip()
        if proc.returncode == 0 and name:
            return name[len("origin/"):] if name.startswith("origin/") else name
        raise _SyncFault("tracked branch unresolved: origin/HEAD is unset and "
                         "census.canonical_ref is empty")

    def _fetch(self, branch: str) -> None:
        """The one fetch per tick: the tracked branch (force-updating — a rewritten
        remote line must still land locally) plus every PR head into refs/sync/pr/*."""
        self._git("fetch", "--quiet", "--prune", "origin",
                  f"+refs/heads/{branch}:refs/remotes/origin/{branch}", _PR_REFSPEC)

    # ── the ledger leg (intent 3): watermark..tip → ONE post_merge receipt ─────
    def _ledger_leg(self, branch: str, prev_local: Optional[str]) -> None:
        tip = self._rev(f"refs/remotes/origin/{branch}")
        if tip is None:
            raise _SyncFault(f"tracked branch {branch!r} has no remote tip after fetch")
        with self._lock:
            watermark = self._meta_get(META_LAST_TIP)
        # Base resolution (D4): the durable watermark; else the mirror's own pre-fetch
        # view; else FIRST CONNECT — baseline at the remote tip, NO historical receipt
        # (marker: baselining at anything older mints history and reds the
        # no-historical-receipt assertion in every CT-3 test's first tick).
        base = watermark or prev_local or tip
        if base != tip and not self._is_ancestor(base, tip):
            # A rewritten line (force-push): log the discontinuity, reset to the last
            # shared point, and record a DEFENSIVE receipt over merge-base..tip —
            # never a receipt over commits that no longer exist on the line.
            merge_base = self._merge_base(base, tip)
            _log.warning("sync.discontinuity branch=%s (non-fast-forward remote; "
                         "defensive receipt over the last shared point)", branch)
            base = merge_base or tip
        if base == tip:
            if watermark != tip:
                with self._lock:
                    self._store.meta_set(META_LAST_TIP, tip)
            return
        envelope = self._build_receipt(base, tip)
        with self._lock:
            report = self._evidence.ingest(envelope, phase="post_merge",
                                           verdict="pass", signal="none")
            # same critical section as the ingest: the watermark can never run ahead
            # of rows the serve path could observe
            self._store.meta_set(META_LAST_TIP, tip)
        _log.info("sync.ledger_ingested base=%.12s head=%.12s matched=%d inserted=%d "
                  "already=%d range_skipped=%s", base, tip, report.matched,
                  len(report.inserted), report.already_recorded, report.range_skipped)

    def _build_receipt(self, base: str, head: str) -> dict:
        """ONE census receipt over base..head via the real CLI in a SUBPROCESS
        (process isolation absorbs the engines' process-global scratch pinning; repo
        code never runs in the server process), parsed back for the in-proc ingest."""
        with tempfile.TemporaryDirectory(prefix="hive-sync-receipt-") as tmp:
            out = Path(tmp) / "receipt.json"
            argv = [sys.executable, "-m", "hive.census.cli", "build",
                    "--repo", self.mirror_dir, "--base", base, "--head", head,
                    "--repo-id", normalized_repo_id(self._cfg.repo_url),
                    "--propagate", "--out", str(out)]
            proc = self._run(argv, env=_clean_env())
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                raise _SyncFault(self._redact(f"census build failed: {detail}"))
            return json.loads(out.read_text(encoding="utf-8"))

    # ── the candidate leg (intent 4): changed PR heads → pre_merge receipts ───
    def _evaluate_candidates(self, branch: str) -> None:
        """Diff the fetched ``refs/sync/pr/*`` heads against the ``sync:pr_heads``
        baseline; evaluate the changed ones (capped, carried over), drop the
        closed ones silently. Each candidate runs under its OWN guard — one bad
        PR never starves the rest of the tick's slots."""
        heads = self._pr_heads()
        with self._lock:
            baseline = self._parse_pr_baseline(self._meta_get(META_PR_HEADS))
        if baseline is None:
            # FIRST CONNECT: record every existing head WITHOUT evaluating —
            # marker: evaluating here is the first-connect PR storm
            # (test_first_connect_baselines_without_evaluating reds).
            with self._lock:
                self._store.meta_set(META_PR_HEADS,
                                     json.dumps(heads, sort_keys=True))
            _log.info("sync.candidates_baselined heads=%d", len(heads))
            return
        closed = [n for n in baseline if n not in heads]
        if closed:
            # a pruned ref IS the close signal: silence, minus the baseline entry
            with self._lock:
                self._advance_pr_heads_locked(remove=closed)
        changed = [n for n, sha in heads.items() if baseline.get(n) != sha]
        # marker: lifting the cap (or advancing unprocessed heads) reds
        # test_cap_carries_over — slots are bounded, the rest stay CHANGED.
        for number in self._pr_order(changed)[:_CANDIDATES_PER_TICK]:
            try:
                self._evaluate_one(number, heads[number], branch)
            except Exception as exc:  # noqa: BLE001 — one candidate never kills the leg
                self._note_error(f"candidate:{number}", exc)

    def _evaluate_one(self, number: str, head_sha: str, branch: str) -> None:
        """One changed head: merge-base against the tracked tip, the durable range
        dedupe, D8 env provisioning, the bounded build, then the in-proc pre_merge
        ingest (verdict DERIVED; a nothing-decided receipt is refused = logged +
        counted). The baseline advances only when the evaluation CONCLUDED
        (ingested, refused, deduped, or unmeasurable) — a killed or failed build
        leaves the head changed, so the next tick retries it."""
        tip = self._rev(f"refs/remotes/origin/{branch}")
        merge_base = self._merge_base(tip, head_sha) if tip else None
        if merge_base is None or merge_base == head_sha:
            # unrelated history, or a head already ON the tracked line: nothing
            # measurable pre-merge — concluded, never re-probed
            with self._lock:
                self._advance_pr_heads_locked(set_entry=(number, head_sha))
            return
        repo_id = normalized_repo_id(self._cfg.repo_url)
        with self._lock:
            # marker: dropping this durable dedupe rebuilds every restart-replayed
            # head (test_already_ingested_range_skips_build reds).
            already = self._store.already_ingested_range(
                repo_id, merge_base, head_sha, "pre_merge")
            if already:
                self._advance_pr_heads_locked(set_entry=(number, head_sha))
        if already:
            _log.info("sync.candidate_deduped pr=%s head=%.12s", number, head_sha)
            return
        extra_bin = self._provision_candidate_env(head_sha)
        envelope = self._build_candidate_receipt(merge_base, head_sha, number,
                                                 extra_bin)
        refusal: Optional[str] = None
        with self._lock:
            try:
                report = self._evidence.ingest(envelope, phase="pre_merge")
            except ReceiptRefused as refused:
                refusal = str(refused)
                self._bump_counter_locked(META_CANDIDATES_REFUSED)
            self._bump_counter_locked(META_CANDIDATES_EVALUATED)
            # same critical section as the ingest: an advanced head can never
            # run ahead of rows the serve path could observe
            self._advance_pr_heads_locked(set_entry=(number, head_sha))
        if refusal is not None:
            _log.info("sync.candidate_refused pr=%s head=%.12s reason=%s",
                      number, head_sha, refusal)
            return
        _log.info("sync.candidate_ingested pr=%s base=%.12s head=%.12s matched=%d "
                  "inserted=%d helped=%d hurt=%d range_skipped=%s", number,
                  merge_base, head_sha, report.matched, len(report.inserted),
                  report.verified_helped, report.verified_hurt,
                  report.range_skipped)

    def _pr_heads(self) -> dict[str, str]:
        """The fetched candidate refs: ``{pr-number: head-sha}`` from
        ``refs/sync/pr/*`` (the fetch's mapping of ``refs/pull/*/head``)."""
        out = self._git("for-each-ref", "--format=%(refname) %(objectname)",
                        "refs/sync/pr/")
        heads: dict[str, str] = {}
        for line in out.splitlines():
            ref, _, sha = line.strip().partition(" ")
            if ref.startswith("refs/sync/pr/") and sha:
                heads[ref[len("refs/sync/pr/"):]] = sha
        return heads

    @staticmethod
    def _parse_pr_baseline(raw: Optional[str]) -> Optional[dict[str, str]]:
        """The stored baseline, or None meaning FIRST CONNECT. Unparseable meta
        also reads as first connect — corruption re-baselines (a quiet skip),
        never a storm of evaluations."""
        if raw is None:
            return None
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(parsed, dict):
            return None
        return {str(k): str(v) for k, v in parsed.items()}

    @staticmethod
    def _pr_order(numbers: Sequence[str]) -> list[str]:
        """Ascending PR number (numeric first, lexicographic fallback) — the cap's
        slots are deterministic, so carry-over drains oldest-first."""
        return sorted(numbers,
                      key=lambda n: (0, int(n)) if n.isdigit() else (1, n))

    def _advance_pr_heads_locked(self, *,
                                 set_entry: Optional[tuple[str, str]] = None,
                                 remove: Sequence[str] = ()) -> None:
        """Read-modify-write of the ``sync:pr_heads`` baseline. The caller HOLDS
        the one global lock (this helper never takes it)."""
        current = self._parse_pr_baseline(self._meta_get(META_PR_HEADS)) or {}
        if set_entry is not None:
            current[set_entry[0]] = set_entry[1]
        for number in remove:
            current.pop(number, None)
        self._store.meta_set(META_PR_HEADS, json.dumps(current, sort_keys=True))

    def _bump_counter_locked(self, key: str) -> None:
        """Increment an integer ``sync:*`` meta counter. Caller holds the lock."""
        raw = self._meta_get(key)
        count = int(raw) if raw and raw.isdigit() else 0
        self._store.meta_set(key, str(count + 1))

    def _provision_candidate_env(self, head_sha: str) -> Optional[str]:
        """D8 provisioning: IFF ``uv.lock`` exists at the candidate root, ``uv
        sync --frozen`` into a venv keyed by the lock's blob OID (a content hash —
        candidates sharing the lock share the venv, one sync per lock); anything
        less returns None and the verifier abstains honestly. Fail-open: a
        missing or failing uv is a logged skip, never a leg fault. Returns the
        venv bin dir to prepend to the build child's PATH."""
        probe = self._run(["git", "-C", self.mirror_dir, "cat-file", "-e",
                           f"{head_sha}:uv.lock"], env=_clean_env())
        if probe.returncode != 0:
            return None
        lock_oid = self._rev(f"{head_sha}:uv.lock")
        if lock_oid is None:
            return None
        venv = Path(self._env_cache_dir) / lock_oid
        marker = venv / ".hive-sync-ready"
        if marker.exists():
            # marker: re-syncing a ready venv reds test_uv_lock_provisions_env
            # (cached per lock-hash — one sync per lock, ever).
            return str(venv / "bin")
        worktree = Path(tempfile.mkdtemp(prefix="hive-sync-candidate-wt-"))
        try:
            added = self._run(["git", "-C", self.mirror_dir, "worktree", "add",
                               "--detach", str(worktree), head_sha],
                              env=_clean_env())
            if added.returncode != 0:
                return None
            env = self._candidate_env(None)
            env["UV_PROJECT_ENVIRONMENT"] = str(venv)
            proc = self._run(["uv", "sync", "--frozen", "--project",
                              str(worktree)], env=env,
                             timeout=_CANDIDATE_TIMEOUT_S)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-300:]
                _log.warning("sync.candidate_env_failed head=%.12s detail=%s",
                             head_sha, self._redact(detail))
                return None
            venv.mkdir(parents=True, exist_ok=True)
            marker.write_text("")
            return str(venv / "bin")
        finally:
            self._run(["git", "-C", self.mirror_dir, "worktree", "remove",
                       "--force", str(worktree)], env=_clean_env())
            shutil.rmtree(worktree, ignore_errors=True)

    def _candidate_env(self, extra_bin: Optional[str]) -> dict[str, str]:
        """The candidate child env: clean git env with EVERY ``HIVE_*`` var
        dropped — repo code runs in these children, so operator configuration and
        credentials must be unreachable (the token never rides env anywhere).
        ``extra_bin`` (the provisioned venv) prepends PATH so the verifier's tool
        probes resolve inside it."""
        env = {k: v for k, v in _clean_env().items()
               if not k.startswith("HIVE_")}
        if extra_bin:
            env["PATH"] = extra_bin + os.pathsep + env.get("PATH", "")
        return env

    def _build_candidate_receipt(self, base: str, head: str, number: str,
                                 extra_bin: Optional[str]) -> dict:
        """The pre-merge receipt over merge-base..pr-head — the same battle-tested
        CLI as the ledger leg, but measured AS the PR line (``--ref``: a detached
        candidate checkout is not the tracked line), spawned with the stripped
        env and killed whole at the constant timeout."""
        with tempfile.TemporaryDirectory(prefix="hive-sync-candidate-") as tmp:
            out = Path(tmp) / "receipt.json"
            argv = [sys.executable, "-m", "hive.census.cli", "build",
                    "--repo", self.mirror_dir, "--base", base, "--head", head,
                    "--repo-id", normalized_repo_id(self._cfg.repo_url),
                    "--propagate", "--ref", f"refs/pull/{number}/head",
                    "--out", str(out)]
            # marker: an unbounded spawn here turns a hanging verifier into a
            # hanging tick (test_hang_fail_open / test_subprocess_env_stripped red).
            proc = self._run(argv, env=self._candidate_env(extra_bin),
                             timeout=_CANDIDATE_TIMEOUT_S)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                raise _SyncFault(self._redact(f"candidate build failed: {detail}"))
            return json.loads(out.read_text(encoding="utf-8"))

    # ── the backfill leg (intent 7): absent fp keys minted server-side ────────
    def _backfill(self, tip_sha: str, ref: str) -> None:
        """Fill ABSENT fingerprint keys on approved code-anchored episodes: sweep
        for carriers lacking ``combdrift/fp``, sync the mirror worktree to
        ``tip_sha`` (the fetch alone never moves the checkout — minting the
        clone-time tree would stamp provenance for a tree it never measured), mint
        each anchor through the real ``hive-edge`` CLI, and merge under the store's
        absent-only guard with ``hive-sync/minted`` provenance. ``{}`` (an
        unresolvable anchor / an engine fault) skips silently and the loop stays
        alive — the next tick retries; at most ``_BACKFILL_PER_TICK`` mints per
        tick, the rest carry over. The lock is held ONLY for store access — never
        across a mint."""
        with self._lock:
            rows = self._store.anchored_episodes_with_meta()
        todo = [(eid, anchor) for eid, anchor, raw in rows if self._lacks_fp(raw)]
        if not todo:
            return
        self._git("reset", "--hard", "--quiet", tip_sha)
        provenance = f"hive-sync-minted/1:server@{tip_sha} {ref}"
        filled = 0
        # marker: lifting the cap (an un-sliced todo) reds test_cap_carries_over —
        # mint spawns per tick are bounded, the rest stay LACKING for the next sweep.
        for eid, anchor in todo[:_BACKFILL_PER_TICK]:
            minted = self._mint(anchor)
            if not minted:
                continue                 # unresolvable ⇒ silent skip, loop alive
            with self._lock:
                if self._store.fill_absent_meta(
                        eid, {**minted, "hive-sync/minted": provenance}):
                    filled += 1
                    self._bump_counter_locked(META_BACKFILLED_TOTAL)
        if filled:
            _log.info("sync.backfilled episodes=%d tip=%.12s ref=%s",
                      filled, tip_sha, ref)

    @staticmethod
    def _lacks_fp(raw: str) -> bool:
        """True iff the carrier can take a minted fp: absent ('') or a map without
        ``combdrift/fp``. An unparseable / non-map carrier reads False — it cannot
        be merged into, so the sweep never selects it (skipped, not a fault)."""
        if not raw:
            return True
        try:
            parsed = json.loads(raw)
        except ValueError:
            return False
        return _FP_KEY not in parsed if isinstance(parsed, dict) else False

    def _mint(self, anchor: str) -> dict[str, str]:
        """One ``hive-edge mint`` subprocess against the mirror — the edge CLI owns
        every fingerprint computation (a server-side reimplementation would fork the
        token format), so backfilled keys are byte-equal to edge-minted ones.
        ``HIVE_EDGE_HOME`` pins the CLI's cache/state beside the mirror. Any fault
        (nonzero exit, unparseable stdout) reads as ``{}`` — the caller skips."""
        env = dict(_clean_env())
        env["HIVE_EDGE_HOME"] = self._edge_home
        proc = self._run([self._edge_cli, "mint", "--repo", self.mirror_dir,
                          "--anchor", anchor], env=env,
                         timeout=_CANDIDATE_TIMEOUT_S)
        if proc.returncode != 0:
            return {}
        try:
            parsed = json.loads(proc.stdout or "")
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): v for k, v in parsed.items() if isinstance(v, str)}

    # ── narrow git/store helpers ───────────────────────────────────────────────
    def _git(self, *args: str) -> str:
        proc = self._run(["git", "-C", self.mirror_dir, *args], env=_clean_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise _SyncFault(self._redact(f"git {args[0]} failed: {detail}"))
        return proc.stdout

    def _rev(self, ref: str) -> Optional[str]:
        proc = self._run(["git", "-C", self.mirror_dir, "rev-parse", "--verify",
                          "--quiet", ref], env=_clean_env())
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        proc = self._run(["git", "-C", self.mirror_dir, "merge-base",
                          "--is-ancestor", ancestor, descendant], env=_clean_env())
        return proc.returncode == 0

    def _merge_base(self, a: str, b: str) -> Optional[str]:
        proc = self._run(["git", "-C", self.mirror_dir, "merge-base", a, b],
                         env=_clean_env())
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _meta_get(self, key: str) -> Optional[str]:
        # meta READS are raw SQL at the driving-adapter boundary (the healthcheck
        # idiom); callers hold the global lock — the conn is shared, not thread-safe.
        row = self._store.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _redact(self, text: str) -> str:
        token = self._cfg.token
        return text.replace(token, "***") if token else text

    def _note_error(self, leg: str, exc: BaseException) -> None:
        """Fail-open surfacing: the fault is logged (redacted) and recorded under
        ``sync:last_error`` — never raised past the tick."""
        message = self._redact(f"{leg}: {type(exc).__name__}: {exc}")[:500]
        _log.warning("sync.leg_failed leg=%s error=%s", leg, message)
        try:
            with self._lock:
                self._store.meta_set(META_LAST_ERROR, message)
        except Exception:  # noqa: BLE001 — surfacing must never become the fault
            _log.warning("sync.last_error_write_failed leg=%s", leg)


def start_sync(cfg, store, evidence: ChangeEvidenceService,
               lock: threading.Lock) -> Optional[threading.Thread]:
    """Arm the sync daemon from the ROOT config (reads ``cfg.sync`` +
    ``cfg.census.canonical_ref``). Returns None when ``sync.repo_url`` is unset —
    byte-inert: no thread, no clone, no census import. The returned daemon thread
    carries its control events as ``sync_stop`` / ``sync_nudge`` attributes (the
    webhook nudge door and tests reach the loop through them) plus the service
    itself as ``sync_service``."""
    sync_cfg: SyncConfig = cfg.sync
    if not sync_cfg.repo_url:
        # marker: starting a thread here breaks test_unset_byte_inert (the MASTER
        # byte-inert check — unarmed must mean NO thread, not an idle one).
        return None
    service = SyncService(sync_cfg, store, evidence, lock,
                          canonical_ref=getattr(cfg.census, "canonical_ref", ""))
    stop, nudge = threading.Event(), threading.Event()
    thread = threading.Thread(target=service.run_forever, args=(stop, nudge),
                              name="hive-sync", daemon=True)
    thread.sync_stop = stop          # type: ignore[attr-defined]
    thread.sync_nudge = nudge        # type: ignore[attr-defined]
    thread.sync_service = service    # type: ignore[attr-defined]
    thread.start()
    _log.info("sync.armed interval_s=%d mirror=%s", sync_cfg.interval_s,
              service.mirror_dir)
    return thread
