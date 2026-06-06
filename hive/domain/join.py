"""OutcomeJoiner — the §11 trace↔outcome state machine. PURE: frozen carriers +
injected ``now: int``; no git, no clock, no SQL. All git I/O is sealed in the
OutcomeSource adapter [D6]; this object owns ALL credit POLICY [A3]:

  - associate : commit → in-window traces (window-primary), or the Hive-Trace
                trailer override; require_stamp drops window association.
  - settle    : a provisional row ripe past settle_at and not clawed back ⇒ +.
  - clawback  : a revert of the SHA, or a bugfix whose modified lines OVERLAP the
                credited commit's introduced lines (blame-line, NOT same-file) ⇒ −.
  - net_emits : fixed hop order — a same-tick clawback cancels the settle + (never
                a stale +).
  - derive_family : git-remote | language | coarse-workflow, at link time.
"""
from __future__ import annotations

import random
import re
from typing import Sequence

from hive.domain.models import (
    CommitFact, JoinerEmit, OutcomeRow, RecallWindow,
)

_DAY_S = 86_400

# coarse-workflow classification
_BUGFIX_RE = re.compile(r"^(fix|bug|hotfix|patch):", re.IGNORECASE)
_BUGFIX_HINTS = re.compile(r"\b(BUG-\d+|regression|crash|race)\b", re.IGNORECASE)
_MANIFESTS = frozenset({
    "requirements.txt", "pyproject.toml", "setup.py", "package.json",
    "package-lock.json", "poetry.lock", "Cargo.toml", "Cargo.lock", "go.mod", "go.sum",
})
_LANG_BY_EXT = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby", ".c": "c",
    ".cc": "cpp", ".cpp": "cpp", ".h": "c", ".hpp": "cpp", ".sh": "shell",
}


class OutcomeJoiner:
    def __init__(
        self, *, assoc_window_s: int, settle_days: int,
        provisional_reward: float, clawback_reward: float,
        require_stamp: bool, assoc_epsilon: float, rng: random.Random,
    ) -> None:
        self.assoc_window_s = int(assoc_window_s)
        self.settle_days = int(settle_days)
        self.provisional_reward = float(provisional_reward)
        self.clawback_reward = float(clawback_reward)   # pre-signed (-1.0)
        self.require_stamp = bool(require_stamp)
        self.assoc_epsilon = float(assoc_epsilon)
        self.rng = rng

    # ── hop 1: associate ──────────────────────────────────────────────────────
    def associate(self, commits: Sequence[CommitFact], window: RecallWindow) -> list[OutcomeRow]:
        """O(commits·traces). Window-primary by default; a Hive-Trace trailer
        OVERRIDES the window with the exact trace set. require_stamp drops the
        window path entirely."""
        rows: list[OutcomeRow] = []
        win_ts = {wt.trace_id: wt.ts for wt in window.items}
        for c in commits:
            if c.trailer_traces:                       # stamp override (higher-credit path)
                trace_ids = list(c.trailer_traces)
            elif self.require_stamp:
                continue                               # unstamped + require_stamp ⇒ no credit
            else:
                trace_ids = [
                    wt.trace_id for wt in window.items
                    if 0 <= (c.ts - wt.ts) <= self.assoc_window_s
                ]
            fam = self.derive_family(c)
            for tid in trace_ids:
                rows.append(OutcomeRow(
                    task_ref=c.sha, trace_id=tid, family_scope=fam,
                    state="provisional", reward=self.provisional_reward,
                    merge_ts=c.ts, settle_at=c.ts + self.settle_days * _DAY_S,
                    introduced_lines=c.introduced_lines,
                    files_touched=c.files_touched, repo=c.repo_remote,
                ))
        return rows

    # ── hop 2: settle ─────────────────────────────────────────────────────────
    def settle(self, rows: Sequence[OutcomeRow], now: int) -> list[JoinerEmit]:
        """A provisional row ripe past settle_at and not clawed back ⇒ +emit.
        Monotone: only provisional → settled_pos emits (clawed_back never settles);
        idempotent (a settled_pos row re-passed emits nothing)."""
        out: list[JoinerEmit] = []
        for r in rows:
            if r.state == "provisional" and r.settle_at <= now:
                out.append(JoinerEmit(
                    task_ref=r.task_ref, trace_id=r.trace_id, family_scope=r.family_scope,
                    reward_sign=+1, magnitude=abs(self.provisional_reward), ts=now,
                ))
        return out

    # ── hop 3: clawback ───────────────────────────────────────────────────────
    def clawback(self, rows: Sequence[OutcomeRow], commits: Sequence[CommitFact], now: int) -> list[JoinerEmit]:
        """A revert of the SHA ⇒ immediate −; a bugfix whose touched_blame OVERLAPS
        the credited row's introduced_lines ⇒ − (blame-line, NOT same-file). A
        coincidental same-file edit with no blame overlap does NOT claw back."""
        out: list[JoinerEmit] = []
        live = [r for r in rows if r.state in ("provisional", "settled_pos")]
        for c in commits:
            if c.kind == "revert" and c.reverts:
                for r in live:
                    if r.task_ref == c.reverts:
                        out.append(self._claw(r, now))
            elif c.kind == "bugfix":
                for r in live:
                    if c.touched_blame and (c.touched_blame & r.introduced_lines):
                        out.append(self._claw(r, now))
        return out

    def _claw(self, r: OutcomeRow, now: int) -> JoinerEmit:
        return JoinerEmit(
            task_ref=r.task_ref, trace_id=r.trace_id, family_scope=r.family_scope,
            reward_sign=-1, magnitude=abs(self.clawback_reward), ts=now,
        )

    # ── hop 4: net (fixed order) ──────────────────────────────────────────────
    @staticmethod
    def net_emits(settle_emits: Sequence[JoinerEmit], clawback_emits: Sequence[JoinerEmit]) -> list[JoinerEmit]:
        """Fixed hop order associate→settle→clawback→emit: a same-tick clawback
        CANCELS the settle + for the same (task_ref, trace_id) — never a stale +."""
        clawed = {(e.task_ref, e.trace_id) for e in clawback_emits}
        kept = [e for e in settle_emits if (e.task_ref, e.trace_id) not in clawed]
        return kept + list(clawback_emits)

    # ── family derivation (at link time, from git facts only) ─────────────────
    @staticmethod
    def derive_family(c: CommitFact) -> str:
        """<remote>|<lang>|<workflow>; byte-identical grammar to the query-side
        resolver [A2]. Empty axes collapse to '*'."""
        remote = c.repo_remote or "*"
        lang = OutcomeJoiner._language(c.files_touched)
        workflow = OutcomeJoiner._workflow(c)
        return f"{remote}|{lang}|{workflow}"

    @staticmethod
    def _language(files: Sequence[str]) -> str:
        counts: dict[str, int] = {}
        for f in files:
            dot = f.rfind(".")
            ext = f[dot:] if dot >= 0 else ""
            lang = _LANG_BY_EXT.get(ext)
            if lang:
                counts[lang] = counts.get(lang, 0) + 1
        if not counts:
            return "*"
        return max(counts.items(), key=lambda kv: kv[1])[0]

    @staticmethod
    def _workflow(c: CommitFact) -> str:
        if c.kind == "bugfix" or _BUGFIX_RE.match(c.message) or _BUGFIX_HINTS.search(c.message):
            return "bugfix"
        files = c.files_touched
        if files and all(f.rsplit("/", 1)[-1] in _MANIFESTS for f in files):
            return "dep-upgrade"
        return "general"
