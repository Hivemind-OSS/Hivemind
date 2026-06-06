"""OutcomeProducer — the in-process, single-writer tick driver. PURE (operates on
injected ports; no git/clock/SQL of its own). One tick = ONE transaction [A7]:

    associate → settle → clawback → net → drain(split + apply_credit) → advance watermark

NEVER raises (I1 liveness): a bad repo / policy failure is counted into the typed
ProducerTick.errors and the loop survives. The whole tick rolls back atomically on
any failure, so partial credit (a settled row whose posterior never moved) is
impossible.
"""
from __future__ import annotations

from hive.domain.attribution import Attributor
from hive.domain.join import OutcomeJoiner
from hive.domain.models import ProducerTick, SettledOutcome
from hive.domain.ports import Clock, EpisodeStore, OutcomeSource, UtilityStore

_WATERMARK_KEY = "_last_drain_ts"


class OutcomeProducer:
    def __init__(
        self, *, source: OutcomeSource, joiner: OutcomeJoiner, attributor: Attributor,
        store: EpisodeStore, utility_store: UtilityStore, clock: Clock,
        source_agent: str = "agent",
    ) -> None:
        self.source = source
        self.joiner = joiner
        self.attributor = attributor
        self.store = store
        self.utility_store = utility_store
        self.clock = clock
        self.source_agent = source_agent

    def step(self, now: int) -> ProducerTick:
        associated = settled = clawed = drained = stamp_hits = window_assoc = 0
        poll_commits = 0
        try:
            with self.store.transaction():
                poll = self.source.poll()
                if not poll.ok:
                    raise RuntimeError(poll.error or "source not ok")
                commits = list(poll.commits)
                poll_commits = len(commits)

                # hop 1 — associate (window-primary, trailer override)
                window = self.store.recall_window(now - self.joiner.assoc_window_s)
                rows = self.joiner.associate(commits, window)
                for r in rows:
                    self.store.link_task(r)
                    self.store.set_task_ref(r.trace_id, r.task_ref)
                associated = len(rows)
                stamp_hits = sum(1 for c in commits if c.trailer_traces)
                window_assoc = sum(1 for c in commits if not c.trailer_traces)

                # hop 2 — settle (ripe provisional only)
                due_rows = [
                    row for tr in self.store.due_settlements(now)
                    for row in self.store.outcomes_for_task(tr)
                ]
                settle_emits = self.joiner.settle(due_rows, now)

                # hop 3 — clawback (revert / blame-overlap bugfix)
                clawback_emits = self.joiner.clawback(self.store.all_live_outcomes(), commits, now)

                # hop 4 — net (a same-tick clawback cancels the settle +)
                net = self.joiner.net_emits(settle_emits, clawback_emits)

                # hop 5 — drain: apply state transition + credit posteriors (watermark-gated)
                watermark = int(self.store.meta_get(_WATERMARK_KEY) or -1)
                max_seen = watermark
                isolation = self.utility_store.isolation_episode_ids()
                for e in net:
                    if e.reward_sign == 1:
                        self.store.settle(e.task_ref, e.trace_id, +e.magnitude)
                        settled += 1
                    else:
                        self.store.clawback(e.task_ref, e.trace_id, -e.magnitude)
                        clawed += 1
                    if e.ts <= watermark:
                        continue  # already drained — no double-credit
                    outcome = SettledOutcome(
                        family_scope=e.family_scope, reward_sign=e.reward_sign,
                        magnitude=e.magnitude, source_agent=self.source_agent)
                    deltas = self.attributor.split(outcome, self.store.exposed_for(e.trace_id), isolation)
                    self.utility_store.apply_credit(deltas)
                    max_seen = max(max_seen, e.ts)
                    drained += 1
                self.store.meta_set(_WATERMARK_KEY, str(max_seen))

            return ProducerTick(
                associated=associated, settled=settled, clawed_back=clawed, drained=drained,
                stamp_hits=stamp_hits, window_assoc=window_assoc, errors=0, poll_commits=poll_commits)
        except Exception:
            # I1 liveness: never let a bad outcome stall the loop.
            return ProducerTick(errors=1, poll_commits=poll_commits)
