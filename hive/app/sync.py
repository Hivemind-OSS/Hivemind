"""The server-side sync daemon — a DRIVING adapter over the ledger, mint, and
drift doors, N repos wide.

One daemon thread (ALWAYS started by ``start_sync``; the entrypoint arms it after
serve-ready) re-reads the durable repo REGISTRY every tick — registering a repo
needs no restart, an empty registry is an inert tick — and, per registered repo
under its OWN fail-open guard, runs three legs against that repo's mirror at
``<mirror_dir>/<name>/``:

- LEDGER: tracked-branch movement lands as ONE unsigned receipt per contiguous
  ``watermark..tip`` range, built by ``python -m hive.census.cli build`` in a
  SUBPROCESS (engine machinery and repo code never enter this process), ingested
  in-proc under the ONE global write lock (phase post_merge; the verdict/tag are
  DERIVED by the domain service — decided execution lines cap at
  bounded-estimate, else the landed-line parity verdict rides), then the
  watermark advances in the same critical section and the post-ingest promotion
  sweep (``LifecycleService.promote_established``) runs.
- MINT BACKFILL: ``anchors_lacking_fp`` rows (empty fingerprint carriers on
  approved anchor bindings) are minted through the in-image ``hive-edge mint``
  CLI against the mirror synced to the canonical tip — one mint owner
  fleet-wide, so server-backfilled keys stay byte-equal to edge-minted ones —
  and merged under the store's absent-only guard with ``hive-sync/minted``
  provenance. ``{}``/nonzero/unparseable ⇒ silent skip; capped, carried over.
- DRIFT MATERIALIZER: per (repo, tip, anchor) wire verdicts into the
  ``anchor_drift`` cache — a worktree at the tip, ``hive-edge verify`` per
  anchor with the stored fingerprints, the output mapped through
  ``hive.app.drift.wire_verdict`` (the cache stores wire vocabulary VERBATIM).
  Tips = the canonical tip plus branch tips recall demanded via ``ref_requests``
  within the 7-day demand window; capped, carried over; old tips pruned.

Named safe directions (Law 6):
- AN EMPTY REGISTRY IS INERT: no git, no clone, no engine import — the tick
  returns after the registry read alone.
- THE SIDE-CHANNEL FAILS OPEN PER REPO, THE SERVE NEVER DOES: every repo runs
  under its own guard (fault → log + ``sync:<name>:last_error``; the OTHER
  repos are untouched), every leg under its own guard inside that, and the
  tick shell survives everything — nothing here can take the serve down.
- THE WATERMARK IS THE DURABLE TRUTH (``sync:<name>:last_tip`` store meta —
  the SAME key ``attach_drift`` resolves canonical tips from); the mirror and
  the drift cache are rebuildable caches — losing either can never open a
  coverage gap or serve a wrong verdict (an un-materialized anchor reads
  ``unverifiable``, never false-fresh, never false-stale).
- CREDENTIALS STAY IN ENV VIA INDIRECTION (D2): a registry row stores only the
  NAME of a token env var, resolved at tick time per repo; the rewritten remote
  URL lives ONLY in the mirror's git config; logs and error meta pass
  ``_redact`` first. A row-named var absent at tick fails that repo open with
  the var NAMED (the boot-time EX_CONFIG probe is the entrypoint's).
- THE VERSION GATE NEVER SCREAMS STALE (BUG-037): an incomparable stored token
  version materializes silence/``unverifiable`` — ``hive-edge verify`` gates
  the resolved-symbol path itself, and the pre-spawn belt here covers the
  missing-symbol path the engine cannot gate.
- THE BACKFILL NEVER OVERWRITES: minted fp keys land through the store's
  absent-only merge (a present key structurally cannot be displaced).
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
from hive.app.drift import DRIFT_UNVERIFIABLE, canonical_tip_key, wire_verdict
from hive.domain.change_evidence import ChangeEvidenceService
from hive.domain.meta import token_version

_log = logging.getLogger("hive.sync")

META_LAST_ERROR = "sync:last_error"       # tick-SHELL faults only (per-repo faults ride their own key)
META_LAST_SYNC_TS = "sync:last_sync_ts"   # ts of the last fault-free tick (via the now seam)
META_BACKFILLED_TOTAL = "sync:backfilled_total"   # anchor carriers fp-backfilled, ever
_DEFAULT_MIRROR_DIR = "/data/sync/mirror"         # the base dir; mirrors live at <base>/<name>
_DEFAULT_TOKEN_ENV = "HIVE_SYNC__TOKEN"           # the fleet-default credential var (D2)
_BACKFILL_PER_TICK = 50              # fp-mint cap per repo per tick; the rest carry over
_DRIFT_PER_TICK = 50                 # verify cap per repo per tick; the rest carry over
_REF_REQUEST_WINDOW_S = 7 * 86_400   # recall demand keeps a branch on the work list this long
_ENGINE_TIMEOUT_S = 600              # the ONE bound on hive-edge mint/verify spawns
_FP_KEY = "combdrift/fp"             # the stored interface-fingerprint carrier key
_SUBGRAPH_KEY = "matrix/subgraph_fp"  # the stored dependency-neighborhood carrier key


def last_error_key(repo: str) -> str:
    """The per-repo fault surface (``sync:<name>:last_error``) — the twin of
    ``hive.app.drift.canonical_tip_key`` for the error side."""
    return f"sync:{repo}:last_error"


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
    never an inherited hook repo (BUG-034). Call-time import — the empty-registry
    path never loads matrix (byte-inert)."""
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


def _redact(text: str, token: str) -> str:
    return text.replace(token, "***") if token else text


def _incomparable_fp_version(fp_token: str) -> bool:
    """BUG-037 belt for the missing-symbol path: ``hive-edge verify``'s own version
    gate fires only when the symbol RESOLVES (the token compare is its layer B), so
    a stored token from another format era would otherwise read stale off a symbol
    the current engine cannot even compare against. The envelope version is read by
    the ONE sanctioned reader (``hive.domain.meta.token_version``); the CURRENT
    version is comb-drift's own constant (call-time import — the engine stays the
    single owner of the token format; no engine importable ⇒ the belt stands down
    and the CLI's own gate is the only one)."""
    version = token_version(fp_token)
    if version is None:
        return True                  # malformed envelope: silence, never stale
    try:
        from combdrift.fingerprint import FINGERPRINT_VERSION  # noqa: PLC0415 — call-time by design
    except Exception:  # pragma: no cover — image without the engine wheel
        return False
    return version != str(FINGERPRINT_VERSION)


class SyncService:
    """Registry-driven mirrors + the poll tick + the ledger/mint/drift legs, one organ.

    ``store`` doubles as the registry/meta/drift seam and (through ``evidence``)
    the ledger door; ``lifecycle`` (optional) is the post-ingest promotion sweep
    handle; ``lock`` is THE global write lock (shared with both HTTP doors) and is
    held ONLY for store access — never across a fetch or a subprocess."""

    def __init__(self, cfg: SyncConfig, store, evidence: ChangeEvidenceService,
                 lock: threading.Lock, run: Run = default_run,
                 now: Optional[Callable[[], int]] = None, *,
                 lifecycle=None) -> None:
        self._cfg = cfg
        self._store = store
        self._evidence = evidence
        self._lock = lock
        self._run = run
        self._now = now or (lambda: int(time.time()))
        self._lifecycle = lifecycle          # duck-typed: .promote_established() -> list[int]
        self.mirror_base = Path(cfg.mirror_dir or _DEFAULT_MIRROR_DIR)
        # the mint/verify CLI's own state home, beside the mirrors (volume-local
        # cache; pinned explicitly so the daemon never writes an operator's home)
        self._edge_home = str(self.mirror_base.parent / "edge-home")
        # hive-edge ships in the image beside the server interpreter; PATH is the
        # fallback for layouts that install it elsewhere
        _edge = Path(sys.executable).parent / "hive-edge"
        self._edge_cli = str(_edge) if _edge.exists() else "hive-edge"

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
        with no restart — D2), then run every repo under its OWN fail-open guard —
        one repo's fault never blocks another, and nothing raises past this method.
        ``sync:last_sync_ts`` — stamped through the injected ``now`` seam — advances
        only when EVERY repo ran fault-free, so it reads as "the last fully-clean
        sync" against the sticky per-repo ``sync:<name>:last_error``."""
        try:
            with self._lock:
                rows = self._store.repo_registry()
        except Exception as exc:  # noqa: BLE001 — the shell fails open too
            self._note_error("registry", exc)
            return
        if not rows:
            # marker: doing ANY work here (a clone, a fetch, an engine spawn) reds
            # CT-9's test_empty_registry_is_inert — an empty registry tick is
            # byte-inert: no git, no mirror dir, no engine import.
            return
        clean = True
        for row in rows:
            clean = self._repo_tick(row) and clean
        if clean:
            with self._lock:
                # marker: stamping this on a faulted tick reds
                # test_repo_fault_does_not_advance_last_sync_ts — a fault anywhere
                # means the sync did NOT complete, and the timestamp must not lie.
                self._store.meta_set(META_LAST_SYNC_TS, str(self._now()))

    # ── one repo, one guard ────────────────────────────────────────────────────
    def _repo_tick(self, row) -> bool:
        """Mirror + fetch, then the three legs, for ONE registry row. Returns True
        iff every leg ran fault-free; every fault is logged + recorded under THIS
        repo's ``sync:<name>:last_error`` and never raises past here."""
        token = ""
        try:
            token = self._resolve_token(row)
            mirror = self.ensure_mirror(row, token=token)
            branch = self._tracked_branch(mirror, row.canonical_ref)
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
        tip = None
        try:
            tip = self._rev(mirror, f"refs/remotes/origin/{branch}")
            if tip is not None:
                self._backfill(row, mirror, tip, branch)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_repo_error(row.name, "backfill", exc, token)
            ok = False
        try:
            if tip is not None:
                self._materialize_drift(row, mirror, tip)
        except Exception as exc:  # noqa: BLE001 — the leg fails open too
            self._note_repo_error(row.name, "drift", exc, token)
            ok = False
        return ok

    @staticmethod
    def _resolve_token(row) -> str:
        """Per-repo credential via env-var indirection (D2): the registry row names
        the var, never a secret byte. A row-named var ABSENT at tick time raises
        (the KeyError NAMES the var) into the per-repo fail-open guard — surfaced
        under ``sync:<name>:last_error``, the other repos untouched; the boot-time
        EX_CONFIG probe is the entrypoint's job. An unset row falls to the fleet
        default var; that one absent ⇒ anonymous (public/local remotes)."""
        if row.token_env:
            return os.environ[row.token_env]
        return os.environ.get(_DEFAULT_TOKEN_ENV, "")

    # ── the mirror (a rebuildable cache — never the durable truth) ─────────────
    def ensure_mirror(self, repo, token: str = "") -> Path:
        """Clone ``repo`` at ``<mirror_dir>/<name>/`` when absent or broken (a
        broken checkout is wiped and recloned — the mirror is a cache); a healthy
        mirror is a no-op. The name is a slug by registry construction, so the
        path cannot escape the base. The (possibly token-rewritten) URL lands
        ONLY in the clone's remote config."""
        mirror = self.mirror_base / repo.name
        probe = self._run(["git", "-C", str(mirror), "rev-parse", "--git-dir"],
                          env=_clean_env())
        if probe.returncode == 0:
            return mirror
        if mirror.exists():
            shutil.rmtree(mirror, ignore_errors=True)   # broken cache: rebuild whole
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
        proc = self._run(["git", "-C", str(mirror), "symbolic-ref", "--short",
                          "refs/remotes/origin/HEAD"], env=_clean_env())
        name = (proc.stdout or "").strip()
        if proc.returncode == 0 and name:
            return name[len("origin/"):] if name.startswith("origin/") else name
        raise _SyncFault("tracked branch unresolved: origin/HEAD is unset and "
                         "the registry row names no canonical_ref")

    def _fetch(self, mirror: Path) -> None:
        """The one fetch per repo per tick: ALL branches (force-updating — a
        rewritten remote line must still land locally), pruned — the drift
        materializer needs requested branch tips, not just the canonical line."""
        self._git(mirror, "fetch", "--quiet", "--prune", "origin",
                  "+refs/heads/*:refs/remotes/origin/*")

    # ── the ledger leg: watermark..tip → ONE post_merge receipt ────────────────
    def _ledger_leg(self, row, mirror: Path, branch: str,
                    prev_local: Optional[str]) -> None:
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
            _log.warning("sync.discontinuity repo=%s branch=%s (non-fast-forward "
                         "remote; defensive receipt over the last shared point)",
                         row.name, branch)
            base = merge_base or tip
        if base == tip:
            if watermark != tip:
                with self._lock:
                    self._store.meta_set(tip_key, tip)
            return
        envelope = self._build_receipt(row, mirror, branch, base, tip)
        with self._lock:
            report = self._evidence.ingest(envelope, phase="post_merge",
                                           verdict="pass", signal="none")
            # same critical section as the ingest: the watermark can never run
            # ahead of rows the serve path could observe
            self._store.meta_set(tip_key, tip)
            # the post-ingest promotion sweep (CT-8): a canonical ingest is the
            # verified-win carrier, so the established rung runs right behind it
            promoted = (self._lifecycle.promote_established()
                        if self._lifecycle is not None else [])
        _log.info("sync.ledger_ingested repo=%s base=%.12s head=%.12s matched=%d "
                  "inserted=%d already=%d range_skipped=%s promoted=%d",
                  row.name, base, tip, report.matched, len(report.inserted),
                  report.already_recorded, report.range_skipped, len(promoted))

    def _build_receipt(self, row, mirror: Path, branch: str,
                       base: str, head: str) -> dict:
        """ONE census receipt over base..head via the real CLI in a SUBPROCESS
        (process isolation absorbs the engines' process-global scratch pinning;
        repo code never runs in the server process), parsed back for the in-proc
        ingest. ``--repo-id`` is the REGISTRY NAME — the §3.6 exact-match join key
        against ``episode_anchors.repo``; ``--ref`` names the measured line
        explicitly (the mirror checkout can lag the fetch)."""
        with tempfile.TemporaryDirectory(prefix="hive-sync-receipt-") as tmp:
            out = Path(tmp) / "receipt.json"
            argv = [sys.executable, "-m", "hive.census.cli", "build",
                    "--repo", str(mirror), "--base", base, "--head", head,
                    "--repo-id", row.name, "--ref", branch,
                    "--propagate", "--out", str(out)]
            proc = self._run(argv, env=_clean_env())
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "").strip()[-500:]
                raise _SyncFault(f"census build failed: {detail}")
            return json.loads(out.read_text(encoding="utf-8"))

    # ── the mint-backfill leg: absent fp keys minted server-side ───────────────
    def _backfill(self, row, mirror: Path, tip_sha: str, ref: str) -> None:
        """Fill ABSENT fingerprint carriers on approved anchor bindings of THIS
        repo: sweep ``anchors_lacking_fp`` (empty-carrier rows only — a pinned
        carrier is structurally out of the sweep), sync the mirror worktree to
        ``tip_sha`` (the fetch alone never moves the checkout — minting the
        clone-time tree would stamp provenance for a tree it never measured), mint
        each anchor through the real ``hive-edge`` CLI, and merge under the
        store's absent-only guard with ``hive-sync/minted`` provenance. ``{}`` (an
        unresolvable anchor / an engine fault) skips silently and the loop stays
        alive — the next tick retries; at most ``_BACKFILL_PER_TICK`` mints per
        tick, the rest carry over. The lock is held ONLY for store access — never
        across a mint."""
        with self._lock:
            todo = self._store.anchors_lacking_fp(row.name)
        if not todo:
            return
        self._git(mirror, "reset", "--hard", "--quiet", tip_sha)
        provenance = f"hive-sync-minted/1:server@{tip_sha} {ref}"
        filled = 0
        # marker: lifting the cap (an un-sliced todo) reds test_cap_carries_over —
        # mint spawns per tick are bounded, the rest stay LACKING for the next sweep.
        for eid, anchor in todo[:_BACKFILL_PER_TICK]:
            minted = self._mint(mirror, anchor)
            if not minted:
                continue                 # unresolvable ⇒ silent skip, loop alive
            with self._lock:
                if self._store.fill_anchor_fp(
                        eid, row.name, anchor,
                        {**minted, "hive-sync/minted": provenance}):
                    filled += 1
                    self._bump_counter_locked(META_BACKFILLED_TOTAL)
        if filled:
            _log.info("sync.backfilled repo=%s carriers=%d tip=%.12s ref=%s",
                      row.name, filled, tip_sha, ref)

    def _mint(self, mirror: Path, anchor: str) -> dict[str, str]:
        """One ``hive-edge mint`` subprocess against the mirror — the edge CLI owns
        every fingerprint computation (a server-side reimplementation would fork the
        token format), so backfilled keys are byte-equal to edge-minted ones.
        ``HIVE_EDGE_HOME`` pins the CLI's cache/state beside the mirrors. Any fault
        (nonzero exit, unparseable stdout) reads as ``{}`` — the caller skips."""
        env = dict(_clean_env())
        env["HIVE_EDGE_HOME"] = self._edge_home
        proc = self._run([self._edge_cli, "mint", "--repo", str(mirror),
                          "--anchor", anchor], env=env,
                         timeout=_ENGINE_TIMEOUT_S)
        if proc.returncode != 0:
            return {}
        try:
            parsed = json.loads(proc.stdout or "")
        except ValueError:
            return {}
        if not isinstance(parsed, dict):
            return {}
        return {str(k): v for k, v in parsed.items() if isinstance(v, str)}

    # ── the drift materializer: (repo, tip, anchor) → wire verdicts (D5) ───────
    def _materialize_drift(self, row, mirror: Path, canonical_tip: str) -> None:
        """Materialize the ``anchor_drift`` cache for THIS repo at the canonical
        tip plus every branch tip recall demanded (``ref_requests`` within the
        demand window): a detached worktree per tip, ``hive-edge verify`` per
        anchor with the STORED fingerprints, verdicts through ``wire_verdict``
        into ``drift_put`` (the cache stores wire vocabulary verbatim). Already-
        materialized (repo, tip, anchor) rows are skipped — that is both the
        carry-over and the cheap steady state; ``drift_prune`` drops every tip no
        longer live. Capped at ``_DRIFT_PER_TICK`` verify spawns per repo per
        tick, the rest carry over next tick."""
        name = row.name
        with self._lock:
            fps = self._repo_fps(name)
        tips = [canonical_tip]
        for ref in self._requested_refs(name):
            sha = self._rev(mirror, f"refs/remotes/origin/{ref}")
            if sha is not None and sha not in tips:
                tips.append(sha)         # a deleted/unknown ref is silently skipped
        if fps:
            anchors = sorted(fps)
            budget = _DRIFT_PER_TICK
            now = int(self._now())
            out: list[tuple[str, str, str, str, str, int]] = []
            for tip in tips:
                if budget <= 0:
                    # marker: lifting the cap (ignoring the budget) reds
                    # test_drift_cap_carries_over — verify spawns per tick are
                    # bounded, the rest stay un-materialized for the next tick.
                    break
                with self._lock:
                    have = self._store.drift_get(name, tip, anchors)
                missing = [a for a in anchors if a not in have]
                if not missing:
                    continue
                batch = missing[:budget]
                budget -= len(batch)
                out.extend(self._verify_at_tip(name, mirror, tip, batch, fps, now))
            if out:
                with self._lock:
                    self._store.drift_put(out)
        with self._lock:
            self._store.drift_prune(name, tips)

    def _repo_fps(self, name: str) -> dict[str, tuple[str, str]]:
        """anchor → (combdrift fp, subgraph fp) over the repo's approved anchor
        bindings — first carrier wins per anchor; an empty/unparseable carrier
        contributes empty tokens (verify then judges existence alone). Raw SQL at
        the driving-adapter boundary (the ``_meta_get`` idiom — the v3 store
        surface exposes the lacking-fp sweep, not this read). Caller HOLDS the
        one global lock."""
        out: dict[str, tuple[str, str]] = {}
        rows = self._store.conn.execute(
            "SELECT ea.anchor, ea.fp_meta FROM episode_anchors ea "
            "JOIN episodes e ON e.id = ea.episode_id "
            "WHERE ea.repo=? AND ea.anchor != '' AND e.status='approved' "
            "ORDER BY ea.episode_id, ea.anchor", (name,))
        for r in rows:
            anchor = r["anchor"]
            if anchor in out:
                continue
            fp = sub = ""
            if r["fp_meta"]:
                try:
                    parsed = json.loads(r["fp_meta"])
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    fp = parsed.get(_FP_KEY) if isinstance(
                        parsed.get(_FP_KEY), str) else ""
                    sub = parsed.get(_SUBGRAPH_KEY) if isinstance(
                        parsed.get(_SUBGRAPH_KEY), str) else ""
            out[anchor] = (fp, sub)
        return out

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
                "WHERE repo=?", (name,)).fetchone()
            newest = row["newest"] if row is not None else None
            if newest is None:
                return []
            cutoff = min(int(self._now()), int(newest)) - _REF_REQUEST_WINDOW_S
            return self._store.requested_refs(name, cutoff)

    def _verify_at_tip(self, name: str, mirror: Path, tip: str,
                       batch: Sequence[str], fps: Mapping[str, tuple[str, str]],
                       now: int) -> list[tuple[str, str, str, str, str, int]]:
        """One detached worktree at ``tip``, one verify per anchor in ``batch``.
        The worktree is removed whole afterwards (a leaked worktree is only disk;
        the next tick's add gets a fresh tmpdir)."""
        worktree = Path(tempfile.mkdtemp(prefix="hive-sync-drift-wt-"))
        out: list[tuple[str, str, str, str, str, int]] = []
        try:
            added = self._run(["git", "-C", str(mirror), "worktree", "add",
                               "--detach", str(worktree), tip], env=_clean_env())
            if added.returncode != 0:
                detail = (added.stderr or added.stdout or "").strip()[-200:]
                raise _SyncFault(f"worktree add failed at {tip[:12]}: {detail}")
            for anchor in batch:
                fp, subgraph = fps[anchor]
                verdict, detail = self._verify_anchor(worktree, anchor, fp, subgraph)
                out.append((name, tip, anchor, verdict, detail, now))
        finally:
            self._run(["git", "-C", str(mirror), "worktree", "remove", "--force",
                       str(worktree)], env=_clean_env())
            shutil.rmtree(worktree, ignore_errors=True)
        return out

    def _verify_anchor(self, worktree: Path, anchor: str, fp: str,
                       subgraph: str) -> tuple[str, str]:
        """One anchor at one tree → ``(wire verdict, compact detail json)``.
        ``hive-edge verify`` owns the verdict logic (version envelopes, prose-
        anchor routing, radius); the output maps through ``wire_verdict`` so the
        cache stores wire vocabulary verbatim. A spawn fault or unparseable
        output reads ``unverifiable`` — fail-safe, never false-stale/fresh."""
        if fp and _incomparable_fp_version(fp):
            # marker (BUG-037): trusting a stale verdict off an incomparable
            # stored token version is the named mutation — CT-6
            # test_version_gate_never_false_stale reds. The engine's own gate
            # covers only the resolved-symbol path; this belt covers the rest.
            return DRIFT_UNVERIFIABLE, json.dumps(
                {"reason": "fingerprint_version_mismatch"}, separators=(",", ":"))
        env = dict(_clean_env())
        env["HIVE_EDGE_HOME"] = self._edge_home
        argv = [self._edge_cli, "verify", "--repo", str(worktree),
                "--anchor", anchor]
        if fp:
            argv += ["--fp", fp]
        if subgraph:
            argv += ["--subgraph-fp", subgraph]
        proc = self._run(argv, env=env, timeout=_ENGINE_TIMEOUT_S)
        if proc.returncode != 0:
            return DRIFT_UNVERIFIABLE, json.dumps(
                {"reason": "verify_failed"}, separators=(",", ":"))
        try:
            parsed = json.loads(proc.stdout or "")
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return DRIFT_UNVERIFIABLE, json.dumps(
                {"reason": "unparseable"}, separators=(",", ":"))
        state = parsed.get("verdict")
        if state == "current" and parsed.get("radius") == "changed":
            state = "radius_changed"     # the wire mapping's radius-tier notation
        verdict = wire_verdict(state, parsed.get("reason", ""))
        detail = {k: parsed[k] for k in ("reason", "radius") if k in parsed}
        return verdict, json.dumps(detail, separators=(",", ":"))

    # ── narrow git/store helpers ───────────────────────────────────────────────
    def _git(self, mirror: Path, *args: str) -> str:
        proc = self._run(["git", "-C", str(mirror), *args], env=_clean_env())
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise _SyncFault(f"git {args[0]} failed: {detail}")
        return proc.stdout

    def _rev(self, mirror: Path, ref: str) -> Optional[str]:
        proc = self._run(["git", "-C", str(mirror), "rev-parse", "--verify",
                          "--quiet", ref], env=_clean_env())
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _is_ancestor(self, mirror: Path, ancestor: str, descendant: str) -> bool:
        proc = self._run(["git", "-C", str(mirror), "merge-base",
                          "--is-ancestor", ancestor, descendant], env=_clean_env())
        return proc.returncode == 0

    def _merge_base(self, mirror: Path, a: str, b: str) -> Optional[str]:
        proc = self._run(["git", "-C", str(mirror), "merge-base", a, b],
                         env=_clean_env())
        out = (proc.stdout or "").strip()
        return out if proc.returncode == 0 and out else None

    def _meta_get(self, key: str) -> Optional[str]:
        # meta READS are raw SQL at the driving-adapter boundary (the healthcheck
        # idiom); callers hold the global lock — the conn is shared, not thread-safe.
        row = self._store.conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else str(row[0])

    def _bump_counter_locked(self, key: str) -> None:
        """Increment an integer ``sync:*`` meta counter. Caller holds the lock."""
        raw = self._meta_get(key)
        count = int(raw) if raw and raw.isdigit() else 0
        self._store.meta_set(key, str(count + 1))

    def _note_error(self, leg: str, exc: BaseException) -> None:
        """Fail-open surfacing for tick-SHELL faults (no repo in scope): logged and
        recorded under the global ``sync:last_error`` — never raised past the tick."""
        message = f"{leg}: {type(exc).__name__}: {exc}"[:500]
        _log.warning("sync.tick_failed leg=%s error=%s", leg, message)
        try:
            with self._lock:
                self._store.meta_set(META_LAST_ERROR, message)
        except Exception:  # noqa: BLE001 — surfacing must never become the fault
            _log.warning("sync.last_error_write_failed leg=%s", leg)

    def _note_repo_error(self, name: str, leg: str, exc: BaseException,
                         token: str) -> None:
        """Per-repo fail-open surfacing: the fault is logged (redacted) and
        recorded under ``sync:<name>:last_error`` — the OTHER repos' keys and the
        tick itself are untouched."""
        message = _redact(f"{leg}: {type(exc).__name__}: {exc}", token)[:500]
        _log.warning("sync.repo_leg_failed repo=%s leg=%s error=%s",
                     name, leg, message)
        try:
            with self._lock:
                self._store.meta_set(last_error_key(name), message)
        except Exception:  # noqa: BLE001 — surfacing must never become the fault
            _log.warning("sync.last_error_write_failed repo=%s leg=%s", name, leg)


def start_sync(cfg, store, evidence: ChangeEvidenceService,
               lock: threading.Lock, *, lifecycle=None) -> threading.Thread:
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
    thread = threading.Thread(target=service.run_forever, args=(stop, nudge),
                              name="hive-sync", daemon=True)
    thread.sync_stop = stop          # type: ignore[attr-defined]
    thread.sync_nudge = nudge        # type: ignore[attr-defined]
    thread.sync_service = service    # type: ignore[attr-defined]
    thread.start()
    _log.info("sync.armed interval_s=%d mirrors=%s", sync_cfg.interval_s,
              service.mirror_base)
    return thread
