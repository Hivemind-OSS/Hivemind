"""S4 — the gated-approval reviewer (the human-in-the-loop, as an agent judgment).

The orchestrator is a SEPARATE identity that owns the privileged ``approved_by`` retirements. It
reads the real worklist over MCP (``hive_health(include_conflicts, include_suspect_consensus)``),
asks a ``claude -p`` JUDGMENT which surfaced candidates to retire, and issues ``hive_prune`` /
``hive_supersede`` for ONLY the ones the judgment chose — declining the rest (a declined candidate
is itself a measured judgment). Unlike the fleet agent it needs no MCP of its own: the harness
feeds it the worklist and executes its decision, so the LLM is a pure ``text in → JSON out`` call.

The §10 O7 firewall is LOAD-BEARING and explicit (one guard): the harness applies NO mechanical
auto-prune — no "retire when hurt-count ≥ N", no "retire every surfaced candidate". The retirement
set is gated by the model's decision, never by a candidate's mere presence; that would make the
BENCHMARK solve O7 (automatic resolution) and would measure the harness, not Hivemind. The AGI arm
changes ONLY the ``approved_by`` stamp to the reserved ``AGI_OVERRIDE`` sentinel (byte-
distinguishable from the gated arm); the decision is still a judgment through the shipped tool.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from hive.domain.agi import AGI_OVERRIDE   # the reserved actor sentinel (pure data, not a steer)

_REVIEW_SYSTEM = (
    "You are the orchestrator for a shared team-memory store: the human-in-the-loop who approves "
    "retirements. You are shown a worklist of candidate memories (by episode_id) that an automatic "
    "scan or an agent flagged as possibly redundant, contradictory, or promoted on thin demand. "
    "For each candidate decide ONE verb: 'prune' to retire a memory that is INCORRECT, MALICIOUS, "
    "or MISLEADING (no replacement); 'supersede' to retire a redundant memory in favour of the "
    "better duplicate (give its winner episode_id); or 'keep' to LEAVE a memory that is merely "
    "neutral, off-topic, or actually correct (over-retiring a harmless memory is the failure). "
    "Reply with ONLY a JSON object: "
    '{"decisions":[{"episode_id":N,"verb":"prune|supersede|keep","winner":M,"reason":"..."}]}.')


@dataclass(frozen=True)
class ReviewCandidate:
    """One worklist entry presented for judgment: the retire-target episode and, for a redundancy
    conflict, the ``winner_hint`` (the other side) a supersede would keep."""
    episode_id: int
    source: str                     # "conflict" | "suspect_consensus"
    winner_hint: Optional[int] = None
    note: str = ""


@dataclass(frozen=True)
class Decision:
    verb: str                       # "prune" | "supersede" | "keep"
    winner: Optional[int] = None
    reason: str = ""


@dataclass(frozen=True)
class Retirement:
    episode_id: int
    verb: str                       # the verb actually issued
    status: str                     # the daemon's result status ("pruned" | "superseded")
    approved_by: str


@dataclass(frozen=True)
class Declined:
    episode_id: int
    reason: str


@dataclass(frozen=True)
class OrchestratorObs:
    """One review pass: what was retired, what was declined (a kept candidate is a measured
    judgment), and the full candidate id set the worklist surfaced."""
    retired: tuple = ()
    declined: tuple = ()
    candidates: tuple = ()


def _candidates_from_worklist(snap: dict) -> list[ReviewCandidate]:
    """Reduce a ``hive_health`` worklist to the de-duplicated retire candidates. A conflict's
    retire-target is its ``loser_hint`` (the other side becomes the supersede ``winner_hint``); a
    suspect-consensus entry is its own episode. Defensive: a fail-closed health snap (no worklist
    keys) yields no candidates. // O(worklist)."""
    by_id: dict[int, ReviewCandidate] = {}
    for c in (snap.get("conflicts") or []):
        loser = c.get("loser_hint")
        if loser is None:
            continue
        winner = c.get("b_id") if loser == c.get("a_id") else c.get("a_id")
        eid = int(loser)
        by_id.setdefault(eid, ReviewCandidate(
            episode_id=eid, source="conflict",
            winner_hint=int(winner) if winner is not None else None,
            note=str(c.get("note", ""))))
    for s in (snap.get("suspect_consensus") or []):
        eid = s.get("episode_id")
        if eid is None:
            continue
        by_id.setdefault(int(eid), ReviewCandidate(
            episode_id=int(eid), source="suspect_consensus", note=str(s.get("note", ""))))
    return list(by_id.values())


def _review_prompt(candidates: list[ReviewCandidate]) -> str:
    lines = ["Review these candidate memories and decide a verb for EACH:"]
    for c in candidates:
        hint = f" (redundancy winner candidate: {c.winner_hint})" if c.winner_hint is not None else ""
        lines.append(f"- episode_id={c.episode_id} [{c.source}]{hint}: {c.note}")
    lines.append('Reply with ONLY the JSON {"decisions":[...]} described in the system prompt.')
    return "\n".join(lines)


def _extract_json(text: str) -> str:
    """The first ``{...}`` block (models often wrap JSON in prose / markdown fences). // O(n)."""
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if 0 <= start < end else ""


def _parse_decisions(reply: str) -> dict[int, Decision]:
    """Parse the orchestrator's JSON judgment into ``{episode_id: Decision}``. DEFENSIVE
    (BUG-002/003 class): an unparseable reply, a non-object, a missing/garbled field, or an
    unknown verb yields NO decision for that candidate — which the caller treats as a decline, so
    a broken judgment retires NOTHING (fail-safe) rather than crashing or over-retiring. // O(n)."""
    out: dict[int, Decision] = {}
    try:
        data = json.loads(_extract_json(reply))
    except (ValueError, TypeError):
        return out
    decisions = data.get("decisions") if isinstance(data, dict) else None
    for d in (decisions or []):
        if not isinstance(d, dict):
            continue
        try:
            eid = int(d.get("episode_id"))
        except (TypeError, ValueError):
            continue
        verb = d.get("verb")
        if verb not in ("prune", "supersede", "keep"):
            continue
        try:
            winner = int(d["winner"]) if d.get("winner") is not None else None
        except (TypeError, ValueError):
            winner = None
        out[eid] = Decision(verb=verb, winner=winner, reason=str(d.get("reason", "")))
    return out


def _issue(client, cand: ReviewCandidate, dec: Decision, approved_by: str) -> dict:
    """Issue the chosen retirement through the shipped tool. A supersede needs a winner (the
    decision's, else the worklist hint); with neither it is a no-op (nothing retired)."""
    if dec.verb == "supersede":
        winner = dec.winner if dec.winner is not None else cand.winner_hint
        if winner is None:
            return {"status": "noop", "reason": "supersede needs a winner"}
        return client.supersede(cand.episode_id, winner, approved_by=approved_by)
    return client.prune(cand.episode_id, approved_by=approved_by)


def run_orchestrator_review(*, client, llm, agi_mode: bool = False,
                            approver: str = "orchestrator",
                            surfaced_hurts: tuple = ()) -> OrchestratorObs:
    """Read the worklist, ask the LLM which candidates to retire, and issue ONLY those — the
    human-in-the-loop gate as an agent judgment. ``agi_mode`` flips the ``approved_by`` stamp to
    ``AGI_OVERRIDE`` (the self-authorized arm); the decision is still a judgment, never a
    mechanical rule (§6 O7 firewall). An empty candidate set asks for no judgment (no LLM call).

    ``surfaced_hurts`` are episode_ids the fleet reported as HURT during real work (via
    ``hive_outcome`` — pure evidence that surfaces a candidate but holds no retirement handle, so
    it mutates no trust). They join the worklist as prune candidates the orchestrator JUDGES: this
    is the path by which an agent's detection leads to a retirement WITHOUT any mechanical
    auto-rule (the standalone hurt-flagged bad fact appears on no shipped worklist, so the harness
    presents it for judgment — O7-safe)."""
    snap = client.health(include_conflicts=True, include_suspect_consensus=True)
    candidates = _candidates_from_worklist(snap)
    seen = {c.episode_id for c in candidates}
    for eid in surfaced_hurts:
        if int(eid) not in seen:
            candidates.append(ReviewCandidate(
                episode_id=int(eid), source="hurt",
                note="a fleet agent reported this memory hurt its task"))
            seen.add(int(eid))
    if not candidates:
        return OrchestratorObs(retired=(), declined=(), candidates=())
    decisions = _parse_decisions(llm.complete(_review_prompt(candidates), system=_REVIEW_SYSTEM))
    approved_by = AGI_OVERRIDE if agi_mode else approver
    retired: list[Retirement] = []
    declined: list[Declined] = []
    for cand in candidates:
        dec = decisions.get(cand.episode_id)
        if dec is None or dec.verb == "keep":          # O7 firewall: no judgment ⇒ no retirement
            declined.append(Declined(cand.episode_id,
                                     dec.reason if dec else "no judgment — left as-is"))
            continue
        res = _issue(client, cand, dec, approved_by)
        status = res.get("status") if isinstance(res, dict) else None
        if status in ("pruned", "superseded"):
            retired.append(Retirement(cand.episode_id, dec.verb, status, approved_by))
        else:
            declined.append(Declined(cand.episode_id, f"retirement returned {status}"))
    return OrchestratorObs(retired=tuple(retired), declined=tuple(declined),
                           candidates=tuple(c.episode_id for c in candidates))
