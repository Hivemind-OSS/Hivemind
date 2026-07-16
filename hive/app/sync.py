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

Extension seams: ``tick()`` runs the LEDGER leg today; the candidate-PR
verification leg and the backfill sweep slot in as sibling ``_candidate_leg()`` /
``_backfill_leg()`` calls, each under its own fail-open guard. The PR refspec
(``refs/sync/pr/*``) is already fetched for the candidate leg's benefit, and the
returned thread carries its control events (``sync_stop`` / ``sync_nudge``) so the
webhook nudge door can wake the loop without touching this module's internals.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from hive.app.config import SyncConfig
from hive.domain.change_evidence import ChangeEvidenceService

_log = logging.getLogger("hive.sync")

META_LAST_TIP = "sync:last_tip"      # the durable watermark (store meta = truth, D4)
META_LAST_ERROR = "sync:last_error"  # the last surfaced fault (redacted, advisory)
_DEFAULT_MIRROR_DIR = "/data/sync/mirror"
_PR_REFSPEC = "+refs/pull/*/head:refs/sync/pr/*"

# The spawn seam (mirrors hive.tools.cli's Run/default_run shape): full child argv
# + an env mapping in, the completed process out — injectable so contract tests can
# observe or replace spawns without faking git itself.
Run = Callable[..., "subprocess.CompletedProcess"]


def default_run(argv: Sequence[str], env: Optional[Mapping[str, str]] = None,
                ) -> subprocess.CompletedProcess:
    return subprocess.run(list(argv), env=None if env is None else dict(env),
                          capture_output=True, text=True)


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
        nothing raises past this method toward the caller."""
        try:
            self.ensure_mirror()
            branch = self._tracked_branch()
            prev_local = self._rev(f"refs/remotes/origin/{branch}")
            self._fetch(branch)
        except Exception as exc:  # noqa: BLE001 — marker: re-raising breaks
            # test_unreachable_fail_open (an unreachable remote is a logged skip;
            # the next tick retries, the serve path never feels it).
            self._note_error("mirror", exc)
            return
        try:
            self._ledger_leg(branch, prev_local)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_error("ledger", exc)
        # Sibling legs slot in here, one fail-open guard each: the candidate-PR
        # verification leg (refs/sync/pr/* is already fetched for it) and the
        # backfill sweep.

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
