# Admin Agent Skills — Hivemind lifecycle

Six skills that let an **admin agent** run a Hivemind server's full lifecycle: stand it
up, connect the team, read whether the shared memory is converging, tune it for *that
team's shape*, drive content value, and keep it durable. This file DEFINES the skills
(contracts, levers, done-criteria); it is not the implementation.

The skills only orchestrate the landed surface — the `hive` CLI, the 4 MCP tools, and the
boot-time `.env`. They add no in-system machinery.

---

## Operating model (read first — it shapes every skill)

- **Two planes.** *Host shell* runs the `hive` CLI (lifecycle, provisioning, KPIs). *MCP
  client* (one admin seat) is needed ONLY for the gap/contested reports
  (`hive_health(include_gaps=true)`); the convergence KPIs are now host-side via
  `hive health`, so the common loop needs no seat.
- **Tuning is boot-time, not live.** Every knob is a frozen `Config` field resolved once at
  boot. To change one: edit `.env` (`HIVE_<GROUP>__<FIELD>`) → `hive up` (recreates the
  container in place; the `hive-data` volume persists) → re-measure. There is **no live
  reload** (deferred — see "Not a skill"). Per-field validators fail-fast: a bad value
  aborts boot loudly rather than silently clamping.
- **The loop is sensor → actuator → re-measure.** `/hive-observe` reads KPIs;
  `/hive-tune` maps a symptom to one knob, applies it, and **confirms the KPI moved**;
  `/hive-curate` is the parallel *content* lever (act on gaps, resolve contradictions).
- **Identity diversity is the fuel.** Promotion out of quarantine requires demand from ≥1
  identity other than the writer. One token per seat is therefore a correctness invariant,
  not just hygiene — a fleet sharing one token structurally cannot promote its own captures.
- **"Value for a team" = convergence**, measured: `confident_rate` rising,
  `demand_entropy` falling, `dead_capture_ratio` bounded, promotions flowing, and the gap
  report shrinking as the team writes to it.

---

## The six skills

Each skill below shares one shape: **Purpose · Trigger · Levers · Inputs · Outputs ·
Guardrails · Done when**.

### `/hive-provision` — stand up a server (first boot)
- **Purpose:** Bring a healthy daemon up and choose the team's starting profile.
- **Trigger:** "set up the hive", new host, fresh clone.
- **Levers:** `hive up` (or `hive up --tunnel`); `.env` seeded from `.env.example`;
  guarantee knobs (`HIVE_RECALL__H_FRAC_MAX`, `HIVE_AUTONOMY__ENABLED`); team shape
  (`HIVE_AUTONOMY__SOLO_MODE` for a single-identity dev).
- **Inputs:** team size/identity count; whether teammates are remote (→ `--tunnel` +
  `NGROK_AUTHTOKEN`/`NGROK_DOMAIN`).
- **Outputs:** a daemon that passed the bounded health-wait; the `hive connect` line.
- **Guardrails:** never expose `0.0.0.0`; `--tunnel` fail-fasts on missing secrets; if boot
  crash-loops after a rebuild, the volume predates the schema → `hive nuke` then `hive up`
  (no silent migration).
- **Done when:** `hive status` reports `up (healthy)` and the connect line is in hand.

### `/hive-enroll` — connect, verify, and offboard seats
- **Purpose:** Put each agent on its own seat and prove it works.
- **Trigger:** "add Alice", onboarding a teammate, rotating/removing a seat.
- **Levers:** `hive token <seat>` (printed once), `hive connect`, `hive tokens`,
  `hive revoke <seat>`. Onboarding is self-serve: the connected agent installs the rules
  block carried in the `hive_health` tool description.
- **Inputs:** one seat label per agent (e.g. `alice-laptop`).
- **Outputs:** a registered seat; a verified `hive_write`→`hive_recall` round-trip.
- **Guardrails:** **one token per seat, never shared** (the promotion-integrity invariant);
  hand tokens over via a secret manager; tokens are never echoed except once on mint.
- **Done when:** the seat round-trips, and `hive tokens` shows exactly the intended labels.

### `/hive-observe` — convergence KPI dashboard (sensor, read-only)
- **Purpose:** A verdict on whether the fleet's memory is converging FOR THIS TEAM.
- **Trigger:** "how's the hive doing", weekly check, before `/hive-tune`.
- **Levers:** `hive status`; `hive health` (trends, host-side); `hive_health(include_gaps=
  true)` over MCP for the gap/contested queues.
- **Inputs:** none (reads the warm store).
- **Reads:** `confident_rate↑`, `demand_entropy↓`, `dead_capture_ratio` bounded,
  `n_promotions>0`, `median_days_to_promotion`, `est_tokens_served`; `trust_counts`,
  `n_misses_7d`; the `solo_hint` (single-seat traffic wasting demand).
- **Outputs:** a ranked symptom list + a converging/stalled verdict → feeds `/hive-tune`
  and `/hive-curate`.
- **Guardrails:** read-only — never writes `.env` or memory.
- **Done when:** every KPI has a direction and at least one named next action (tune or
  curate), or the verdict is "healthy".

### `/hive-tune` — optimize the loop for this team (actuator)
- **Purpose:** Move a stalled KPI by changing exactly one knob, then confirm it moved.
- **Trigger:** a symptom from `/hive-observe` (e.g. "captures never promote").
- **Levers:** a bounded `.env` edit (`autonomy.*`, `recall.*`, `retention.backup_keep`) →
  `hive up` → re-measure with `hive health`. See the symptom→knob table below.
- **Inputs:** the current KPI snapshot + the team profile.
- **Outputs:** an applied `.env` diff, a restart, and a before/after KPI comparison
  (revert if the KPI didn't move or a guardrail KPI regressed).
- **Guardrails:** change **one knob per cycle** (else you can't attribute the effect); stay
  within validator bounds; **never weaken** the guarantee knobs (`H_frac_max`,
  `autonomy.enabled`); re-measure on the 7-day window before concluding (a tuning change
  needs time to show in the KPIs).
- **Done when:** the targeted KPI improved without a guardrail KPI (e.g.
  `dead_capture_ratio`) regressing, or the change is reverted and the symptom re-escalated.

### `/hive-curate` — drive content value (gaps + contradictions)
- **Purpose:** Turn the demand signal into written knowledge and resolve contradictions.
- **Trigger:** a non-empty gap report or contested queue from `/hive-observe`.
- **Levers:** `hive_health(include_gaps=true)` → for top demand-gap clusters, prompt the
  team (or write with approval) the missing insight; for the contested queue, resolve each
  with one `hive_write(replaces=<episode_id>, approved_by=<human>)`.
- **Inputs:** the demand-gap clusters and the contested/supersession queue.
- **Outputs:** new `established` memories filling real gaps; retired contradictory rows.
- **Guardrails:** `hive_write` requires a real human approver (naming the approver IS the
  approval); curate fills *demanded* gaps, never speculative bulk writes.
- **Done when:** the top gaps have coverage or an owner, and the contested queue is drained.

### `/hive-maintain` — durability, recovery, exposure, seat hygiene (day-2)
- **Purpose:** Keep the store safe and the surface secure between tuning cycles.
- **Trigger:** scheduled cadence, an incident, or a schema-generation upgrade.
- **Levers:** `hive backup` (+ `retention.backup_keep`); `hive logs [svc]`; `hive down`/`up`;
  `hive nuke` (typed confirm) for the schema-generation reset; `hive tokens`/`revoke` for
  stale-seat cleanup; tunnel/exposure check via `hive status`.
- **Inputs:** backup cadence; the live seat roster vs `hive tokens`.
- **Outputs:** fresh snapshots within retention; a clean log triage; revoked stale seats; a
  confirmed-healthy restart.
- **Guardrails:** `hive down` preserves the volume, `hive nuke` destroys it (typed
  confirm); upgrades across schema generations are nuke+up, not migration; never publish
  `0.0.0.0` — remote access is the tunnel (TLS at the ngrok edge) or SSH only.
- **Done when:** a valid recent backup exists, no stale seats remain, and `hive status` is
  healthy with exposure as intended.

---

## Symptom → knob (the heart of `/hive-tune`)

| KPI symptom | Likely cause | Knob move (`.env`, then `hive up`) |
|---|---|---|
| `n_promotions ≈ 0`, quarantine piling up | demand too strict for a small/sparse team | ↓ `AUTONOMY__DEMAND_M`, ↑ `AUTONOMY__DEMAND_WINDOW_DAYS`, ↓ `AUTONOMY__DEMAND_TAU` |
| Promotions ≈ 0 **and** single identity (`solo_hint` set) | anti-gaming clause unsatisfiable (one writer) | `AUTONOMY__SOLO_MODE=true` + `AUTONOMY__SOLO_MIN_SPAN_DAYS` |
| `demand_entropy` high & flat | demand is diffuse noise, not fillable gaps | hand to `/hive-curate`; do **not** loosen promotion |
| `dead_capture_ratio` rising | fleet writing junk, or TTL too short | review capture discipline; ↑ `AUTONOMY__QUARANTINE_TTL_DAYS` cautiously |
| `confident_rate` low despite stock | recall gate too tight / lexical blind spot | enable `RECALL__HYBRID=true` (needs FTS5); revisit `RECALL__RECALL_TOP_N`, `RECALL__SOFTMAX_BETA` — **never** weaken `RECALL__H_FRAC_MAX` |
| repeated abstains near a servable row (contested) | stale/contradictory `established` memory | route to `/hive-curate` → `hive_write(replaces=…)` |
| provisional rows vanishing too fast | `provisional_ttl_days` too short for cadence | ↑ `AUTONOMY__PROVISIONAL_TTL_DAYS` |

Restart-tier knobs that need more than a bounce: `EMBEDDING__MODEL` and `GEOMETRY__D` change
the vector space → a re-embed, not just `hive up`. `RECALL__HYBRID` requires an FTS5-enabled
SQLite (the daemon fail-fasts at boot otherwise).

---

## What is deliberately NOT a skill (and why)

- **No autonomous closed-loop tuner.** Minimization removed the in-system config-tuning
  agent and live reload on purpose. `/hive-tune` is an out-of-band, human-checkpointed
  actuator (edit `.env` → restart → re-measure). Re-introducing a fully autonomous tuner
  re-opens that decision; do it knowingly, after the symptom→knob mapping above has been
  trusted manually — not by default.
- **No agent-driven `established` trust.** Promotion to `established` is the survival rule
  (`survival_e` distinct non-writer identities over `survival_days` with
  `survival_min_exposures`) or a human `hive_write(approved_by=…)`. An admin skill never
  manufactures human approval.
- **No host-side gaps/contested (yet).** Those need the live servable index, which a
  read-only host-side read cannot build; they stay on `hive_health(include_gaps=true)` over
  MCP. (`hive health` covers the trends KPIs host-side.)
