# Tuning runbook — for the config-tuning agent

You are a **trusted tuning agent**. Your job: hill-climb the convergence KPIs by editing
**one file** (`hive.config.toml`), one knob per window, and proving each change with
`hive trends`. Safety-critical knobs are firewalled — you *cannot* change them, and you
should not try.

## 0. Read the contract FIRST (every session)

```
grep -A40 'CONFIG_AUTHORITY' hive/app/config.py
```

- **`CONFIG_AUTHORITY["group.field"]`** — `"agent"` = you may set it; `"operator"` = you must not (it is the operator's; see the firewall below).
- **`SAFE_BOUNDS["group.field"]`** — the advisory `(lo, hi)` you should stay within (tighter than the hard validators).
- **`KPI_MOVED["group.field"]`** — which KPI the knob moves, and the direction. Knobs marked **INERT** do nothing on the live path — do not waste a window on them.

The only file you may edit is **`hive.config.toml`** (repo root). It is precedence layer 2
(TOML); the operator's env-pins are layer 3 and win.

## 1. The loop (one change per window)

1. **Observe** — `hive trends` prints `{current, previous, deltas}` JSON. Read `current`
   and `deltas` for the four KPIs:
   - `confident_rate` ↑ is good (served vs abstained/missed).
   - `demand_entropy` ↓ is good (unmet demand concentrating into fillable gaps, not diffuse noise).
   - `n_promotions` — memories the fleet earned into the servable set this window.
   - `dead_capture_ratio` — captures that expired unpromoted; keep it bounded (not climbing).
2. **Diagnose** — pick the SINGLE highest-leverage `agent` knob from `KPI_MOVED` for the KPI you want to move.
3. **Edit** `hive.config.toml` — set that ONE knob, inside its `SAFE_BOUNDS`. Example:
   ```toml
   [autonomy]
   demand_tau = 0.70
   ```
4. **Pre-check (optional, host-side)**:
   ```
   python -c "from hive.app.config import Config; Config.load(toml_path='hive.config.toml')"
   ```
   Exit 0 ⇒ your values pass the hard validators. **Boundaries (read these):**
   - This reflects your edits to **agent** knobs accurately.
   - It does **NOT** show the operator's runtime env-pins for **guarantee** knobs (you must not edit those anyway) — host-side it shows your TOML value, not the effective pinned value.
   - A malformed-TOML *syntax* error or an *unknown-key typo* is **swallowed** (logged `config.toml_*`, falls back to defaults) — it does NOT fail this check. Your signal that a typo'd change didn't apply is the KPIs not moving (and the boot log).
5. **Apply** — `hive down && hive up`. Warm boot (store + embeddings cached); the embedder reload is the pole, so expect tens of seconds, not instant.
6. **Record** — commit, with the rationale. Git is the audit log and your memory across cycles:
   ```
   git commit -am "tune: autonomy.demand_tau 0.75->0.70 — lift n_promotions (was 3, confident_rate sagging)"
   ```
7. **Verify** — next window, `hive trends` again. Did the target KPI move the right way?
   Keep it, or `git revert` + warm boot.

## 2. The firewall (why some edits silently do nothing)

The four **guarantee knobs** (`config.GUARANTEE_KNOBS`: `recall.H_frac_max`,
`recall.epsilon_explore`, `utility.isolation_frac`, `autonomy.enabled`) are pinned by the
operator at the env layer in `compose.yaml`. If you write one in `hive.config.toml`, the
env-pin **silently overrides it at boot** — the change will not take effect, and `hive
trends` will not move. The contract already tells you these are `operator`. Do not try to
earn `confident_rate` by loosening `H_frac_max`; earn it through demand/promotion tuning.

## 3. Do NOT touch

- Any `operator`-authority knob (the contract names them).
- **`compose.yaml` and `.env`** — the firewall lives there. Editing them is outside your
  sanctioned action; that is the operator's job.
- If a value is out of the hard range, boot **fails loudly** (`hive up` exits non-zero) —
  downtime, never a silently-wrong guarantee.

## 4. Cadence reality

The KPIs are **14-day windowed** aggregates compared window-over-window. A change needs
days of real traffic to show signal. So: **one change per window**, never confounded
(two knobs at once are unattributable). This is a slow hill-climb, not a fast control loop.
