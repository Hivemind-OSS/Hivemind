"""M11 — the composition-root substrate: ONE frozen, fail-fast-validated Config
resolved from a 3-layer precedence stack (group defaults < HIVE_* env < explicit overrides).

The surface is narrow (`Config.load(...)`); the depth is the whole layering +
type-coercion + per-field validation machine. Config is applied only at boot (a full
restart) — there is no live reload path. The hard per-field `__post_init__` validators are
the guarantee floor: an out-of-range value still fails boot loudly.

Boundary: this module WIRES, it does not COMPUTE. It depends on the registry only for
*key membership* validation (lazy import — importing `config` never imports torch). It
never reaches into `core/` domain logic.

Grounded deviations (built-decision-wins):
- `db_path` defaults to `":memory:"` (ephemeral — cannot corrupt a persistent store) so a
  no-db_path `Config.load(...)` resolves; an EMPTY string is the fail-fast trigger. The
  "no silent default" prose guards a *persistent* store; `:memory:` is not one.
- env field/group tokens are matched case-INSENSITIVELY (upper-fold) against the dataclass
  names — the `__` separator namespaces group from field, structurally closing the old
  upper-case `CORTEX_D` collision — the `__` group/field split prevents any case-clash
  within a group.
"""
from __future__ import annotations

import dataclasses
import logging
import math
import os
from dataclasses import dataclass, fields
from typing import Any, Mapping, Optional

_log = logging.getLogger("hive.config")

_MEMORY_DB = ":memory:"


# ── frozen config groups ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuntimeConfig:
    db_path: str = _MEMORY_DB
    tenant_id: str = "default"

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ValueError("runtime.db_path is required (no silent default); set db_path=...")


@dataclass(frozen=True)
class EmbeddingConfig:
    # The embedder emits the model's NATIVE vector unchanged — there is no projection head and
    # no dimension knob. The native (stored-vector) dim is a property of the model, resolved by
    # native_dim_for and asserted against the model at load(), never an operator-settable choice.
    provider: str = "local_st"
    model: str = "Qwen/Qwen3-Embedding-0.6B"

    def __post_init__(self) -> None:
        if self.provider != "local_st":
            raise ValueError(
                f"embedding.provider {self.provider!r} unknown; valid=['local_st']")


@dataclass(frozen=True)
class RecallConfig:
    recall_top_n: int = 10
    # The absolute-cosine serve floor: the gate suppresses unless at least ``k_min``
    # candidates clear ``tau_serve``. This is the never-hallucinate floor — a shift-invariant
    # entropy is gone; "flat-but-all-relevant" SERVES, "flat-but-weak" and "peaked-but-weak"
    # ABSTAIN. 1.0 ⇒ only an exact-match field serves. CALIBRATED on the real
    # query distribution; the 0.70 default is the prior flat-serve posture, calibration-grounded.
    # Meaningful only because stored/searched vectors are EXACT unit-norm (BUG-008).
    tau_serve: float = 0.70
    k_min: int = 1                        # min absolutely-relevant hits to serve (>= 1)
    # decorrelated serve-set selection is UNCONDITIONAL (no knob): overscan resolves
    # recall_top_n*overscan candidates so select_served can backfill servable hits past unservable
    # top ranks, then runs trust-dominance + MMR over that pool, capped at recall_top_n.
    overscan: int = 3

    def __post_init__(self) -> None:
        if self.recall_top_n < 1:
            raise ValueError(f"recall.recall_top_n must be >= 1 (got {self.recall_top_n})")
        if not (math.isfinite(self.tau_serve) and 0.0 < self.tau_serve <= 1.0):
            raise ValueError(
                f"recall.tau_serve must be finite in (0.0, 1.0] (the never-hallucinate "
                f"serve floor); got {self.tau_serve}")
        if int(self.k_min) < 1:
            raise ValueError(f"recall.k_min must be >= 1 (got {self.k_min})")
        if int(self.overscan) < 1:
            raise ValueError(f"recall.overscan must be >= 1 (got {self.overscan})")


@dataclass(frozen=True)
class AutonomyConfig:
    """The mechanical memory-lifecycle knobs (quarantine → demand-promotion →
    decay). ``enabled=False`` flips the whole subsystem inert: capture refused,
    no promotion/decay, no ledger writes on the read path — byte-stable with the
    pre-lifecycle build (labels stay, additive-only)."""
    enabled: bool = True
    demand_m: int = 3               # window misses required to promote
    demand_window_days: int = 14
    demand_tau: float = 0.75        # miss ↔ candidate cosine floor
    competitor_tau: float = 0.85    # candidate ↔ servable cosine ⇒ demand already answered
    quarantine_ttl_days: int = 14
    provisional_ttl_days: int = 45
    # promotion-time embedding-cluster anomaly flag (detection-only, advisory). A candidate
    # with >= anomaly_min_cluster quarantined neighbors within cos >= anomaly_tau is flagged
    # in the promote audit (the MINJA/AgentPoison flood signature); it NEVER blocks promotion.
    anomaly_tau: float = 0.95       # compact-cluster cosine floor (tighter than near-dup)
    anomaly_min_cluster: int = 5    # >= this many near-identical neighbors ⇒ flag
    # the verified-promotion rung (HIVE_AUTONOMY__VERIFIED_PROMOTION): when ON, a
    # quarantined memory carrying a SHA-bound outcome_verified_helped audit promotes at
    # the next demand tick (competitor veto retained). It adds a second mechanical path out
    # of quarantine — a safety-loosening rung — but GRADUATED to default-ON on the L4 powered
    # gate (+0.243 success, FSR 0.0 at power; the §9.9 graduation exception). Opt OUT with
    # HIVE_AUTONOMY__VERIFIED_PROMOTION=false ⇒ no reader handle wired, the rung unreachable,
    # the envelope byte-identical to the strict build. Receipt authenticity is out of scope by
    # design: receipts are unsigned and the ingest door verifies no signature — authenticity
    # rides the transport/auth channel (loopback or the authctl per-seat token tier), not the receipt.
    verified_promotion: bool = True

    def __post_init__(self) -> None:
        for name in ("demand_m", "demand_window_days", "quarantine_ttl_days",
                     "provisional_ttl_days", "anomaly_min_cluster"):
            v = getattr(self, name)
            if int(v) < 1:
                raise ValueError(f"autonomy.{name} must be >= 1 (got {v})")
        for name in ("demand_tau", "competitor_tau", "anomaly_tau"):
            v = getattr(self, name)
            if not (math.isfinite(v) and 0.0 < v <= 1.0):
                raise ValueError(f"autonomy.{name} must be finite in (0, 1] (got {v})")


@dataclass(frozen=True)
class ConflictConfig:
    """Conflict/redundancy SURFACING knobs (detection + the advisory flag). ``enabled``
    gates DETECTION (the recall ``conflicts`` carrier + the health worklist) and the
    ``hive_flag`` advisory verb. Serve-time PRUNING of a strictly-lower-trust near-dup twin
    is no longer a separate switch — it is folded into the unconditional ``select_served`` stage,
    which sources its near-dup floor from this ``tau`` (single owner). The
    resolution verb ``hive_supersede`` is ALWAYS on (Law 3 human vouch) and retirement stays
    human. DETECTION (``enabled``) ships ON by default so the fleet surfaces conflicts for
    human resolution; set ``enabled=false`` to restore the byte-inert envelope (no ``conflicts``
    key, ``hive_flag`` → disabled). The detection METHOD (cosine+polarity) is encoded in code,
    never a swap knob (THEORY §14); ``tau`` is the one genuinely-empirical knob (the near-dup
    cosine floor, shared with select_served). Default 0.80: measured Qwen3 cosines put genuine
    paraphrase/contradiction pairs at ~0.81-0.87 while distinct same-subsystem facts top out
    ~0.69, so 0.80 sits in that gap (recall over the stricter 0.85 at no measured false-positive
    cost, with margin above the distinct-fact ceiling). It stays a per-deployment knob —
    recalibrate on the real corpus via the benchmark; the asymmetric cost (a false positive can
    lead a human to retire a real memory) argues for keeping margin rather than chasing the
    lowest-cosine conflicts."""
    enabled: bool = True
    tau: float = 0.80
    top_n: int = 10

    def __post_init__(self) -> None:
        if not (math.isfinite(self.tau) and 0.0 < self.tau <= 1.0):
            raise ValueError(f"conflict.tau must be finite in (0, 1] (got {self.tau})")
        if self.top_n < 1:
            raise ValueError(f"conflict.top_n must be >= 1 (got {self.top_n})")


@dataclass(frozen=True)
class SuspectConsensusConfig:
    """The suspect-consensus worklist knobs (detection-only — surfaces provisional promotions
    whose demand had thin EFFECTIVE INDEPENDENCE, the confidently-wrong-but-popular failure
    mode). The worklist is gated SOLELY by the ``hive_health(include_suspect_consensus=true)``
    request flag — there is no config ``enabled`` toggle (the served envelope stays byte-inert
    when the flag is unset, the same single-gate posture as the conflict worklist). It NEVER
    changes a promote boolean or retires a row — it reads the stamped promote audit and renders a
    human worklist (Law 3, THEORY §10 O7). ``n_eff_frac_max`` is the thin-vote floor: a
    provisional with ``n_eff / k < n_eff_frac_max`` is flagged (0.5 ⇒ flag any promotion driven by
    fewer than half as many INDEPENDENT votes as raw asks). ``top_n`` caps the worklist."""
    n_eff_frac_max: float = 0.5
    top_n: int = 10

    def __post_init__(self) -> None:
        if not (math.isfinite(self.n_eff_frac_max) and 0.0 < self.n_eff_frac_max <= 1.0):
            raise ValueError(
                f"suspect_consensus.n_eff_frac_max must be finite in (0, 1] "
                f"(got {self.n_eff_frac_max})")
        if self.top_n < 1:
            raise ValueError(f"suspect_consensus.top_n must be >= 1 (got {self.top_n})")


@dataclass(frozen=True)
class RetentionConfig:
    backup_keep: int = 30
    backup_dir: str = ""                 # "" ⇒ computed as <db_dir>/backups at load()

    def __post_init__(self) -> None:
        if self.backup_keep < 1:
            raise ValueError(f"retention.backup_keep must be >= 1 (got {self.backup_keep})")


@dataclass(frozen=True)
class ObservabilityConfig:
    log_level: int = logging.INFO


@dataclass(frozen=True)
class AgiConfig:
    """The AGI-MODE operator opt-in (``HIVE_AGI__MODE``). When ON, the boundary HONORS the
    reserved ``approved_by="AGI_OVERRIDE"`` sentinel so an agent may self-authorize the
    currently human-gated trust actions (establish / supersede / prune); when OFF (default),
    that sentinel is REJECTED fail-closed and every existing envelope is byte-identical. It
    LOOSENS a safety gate, so default-OFF is strict and non-negotiable (THEORY §9.9): unlike
    the two sanctioned default-ON exceptions, an operator must explicitly opt in. The flag is a
    transport concern — the pure domain reacts to the sentinel VALUE only (Law 4); this group
    carries the one bool the boundary reads.

    Scope note: today this switch is mostly decorative — it gates one narrow check (whether the
    AGI_OVERRIDE sentinel is honored on write/supersede/prune) and does not actually enforce a
    fully autonomous mode. It will be made rigorous in later updates."""
    mode: bool = False


@dataclass(frozen=True)
class SecretScanConfig:
    """The credential secret-floor operator opt-OUT (``HIVE_SECRET_SCAN__ENABLED``). The
    deterministic scan that runs BEFORE any persistence is ON by default (``enabled=True`` —
    byte-identical to the always-on floor). Set it False to BYPASS the scan: raw text (including
    credentials) is then persisted unscanned. Disabling LOOSENS a safety gate, so the loosened
    (bypass) state is opt-in only — the default is the SAFE direction (floor on), the same posture
    as AGI_MODE expressed with inverted polarity (THEORY §9.9). The bypass lives in the
    DefaultSecretScanner adapter (the injected scanner returns CLEAN when off); the pure-domain
    scan is untouched (Law 4). A disabled floor is never silent — it is surfaced loudly at boot
    (a WARN) and in ``hive_health`` (a ``secret_scan_disabled`` key, present only when off)."""
    enabled: bool = True


@dataclass(frozen=True)
class CensusConfig:
    """Census/ledger scoping (HIVE_CENSUS__*). canonical_ref names the repo's canonical
    line (e.g. "master"): when set, the stale remediation / last_verified rider derives
    only from receipts stamped with that ref (legacy ref-less rows still count — the
    absence rule). "" (default) ⇒ byte-identical to the unscoped build (§9.9)."""
    canonical_ref: str = ""


# ── the root ──────────────────────────────────────────────────────────────────
# Auth is NOT a config group: it is a property of the listening socket (a tokenless
# loopback door + a token-required tunnel door, bound by the entrypoint), so there is no
# HIVE_AUTH__MODE switch to resolve here.
_GROUP_TYPES: dict[str, type] = {
    "runtime": RuntimeConfig,
    "embedding": EmbeddingConfig,
    "recall": RecallConfig,
    "autonomy": AutonomyConfig,
    "conflict": ConflictConfig,
    "suspect_consensus": SuspectConsensusConfig,
    "retention": RetentionConfig,
    "obs": ObservabilityConfig,
    "agi": AgiConfig,
    "secret_scan": SecretScanConfig,
    "census": CensusConfig,
}
# field-groups constructed (and thus validated) BEFORE runtime, so a field-level error
# (e.g. recall.tau_serve) surfaces ahead of the db_path-required check. Derived from
# _GROUP_TYPES (its insertion order) minus runtime — one source of truth for the group set.
_FIELD_GROUP_ORDER = tuple(g for g in _GROUP_TYPES if g != "runtime")


@dataclass(frozen=True)
class Config:
    runtime: RuntimeConfig
    embedding: EmbeddingConfig
    recall: RecallConfig
    autonomy: AutonomyConfig
    conflict: ConflictConfig
    suspect_consensus: SuspectConsensusConfig
    retention: RetentionConfig
    obs: ObservabilityConfig
    agi: AgiConfig
    secret_scan: SecretScanConfig
    census: CensusConfig

    @property
    def db_path(self) -> str:
        return self.runtime.db_path

    @classmethod
    def load(
        cls,
        db_path: str = _MEMORY_DB,
        *,
        env: Optional[Mapping[str, str]] = None,
        **overrides: Mapping[str, Any],
    ) -> "Config":
        """Resolve a frozen Config from group defaults < HIVE_* env < explicit overrides.

        `db_path` (positional/kw) or `overrides["runtime"]["db_path"]` set the store path
        (default `:memory:`). Validation is fail-fast: the first illegal field raises
        `ValueError`.
        """
        env = os.environ if env is None else env
        # layer 1: per-group default kwargs
        merged: dict[str, dict[str, Any]] = {g: {} for g in _GROUP_TYPES}
        # layer 2: HIVE_<GROUP>__<FIELD> env
        _apply_env(merged, env)
        # layer 4: explicit overrides (per-group dicts). An unknown FIELD here is a typo in the
        # highest-precedence, most-explicit layer (e.g. recall={"tau_servee": 0.4}) — fail fast
        # rather than silently dropping it and leaving the floor at its default.
        for g, vals in overrides.items():
            if g not in _GROUP_TYPES:
                raise ValueError(f"unknown config group override {g!r}; "
                                 f"valid={sorted(_GROUP_TYPES)}")
            known = {f.name for f in fields(_GROUP_TYPES[g])}
            for k in vals:
                if k not in known:
                    raise ValueError(
                        f"unknown config override {g}.{k!r}; valid={sorted(known)}")
            merged[g].update(dict(vals))
        # db_path: explicit arg wins over a runtime override dict, else default
        if db_path != _MEMORY_DB or "db_path" not in merged["runtime"]:
            merged["runtime"]["db_path"] = db_path

        # construct field-groups first (validate), runtime last (db_path-required check last)
        groups: dict[str, Any] = {}
        for g in _FIELD_GROUP_ORDER:
            groups[g] = _construct_group(g, merged[g])
        groups["runtime"] = _construct_group("runtime", merged["runtime"])

        # default backup_dir = <db_dir>/backups (computed once, after db_path is known)
        if not groups["retention"].backup_dir:
            db = groups["runtime"].db_path
            base = os.path.dirname(os.path.abspath(db)) if db != _MEMORY_DB else os.getcwd()
            groups["retention"] = dataclasses.replace(
                groups["retention"], backup_dir=os.path.join(base, "backups"))

        return cls(**groups)


# ── load helpers ──────────────────────────────────────────────────────────────
def _construct_group(group: str, vals: Mapping[str, Any]):
    typ = _GROUP_TYPES[group]
    flds = {f.name for f in fields(typ)}
    kwargs = {k: v for k, v in vals.items() if k in flds}
    return typ(**kwargs)


def _apply_env(merged: dict[str, dict[str, Any]], env: Mapping[str, str]) -> None:
    """HIVE_<GROUP>__<FIELD> → coerce to the field's declared type. group/field matched
    case-insensitively (upper-fold); the `__` separator namespaces group from field so
    the old upper-case `CORTEX_D` collision cannot recur. A non-coercible value is skipped
    with a WARN that NEVER echoes the raw value (it could be a secret)."""
    # case-insensitive lookups: UPPER(name) -> (group_name, type) / field_name
    group_by_upper = {g.upper(): g for g in _GROUP_TYPES}
    for raw_key, raw_val in env.items():
        if not raw_key.startswith("HIVE_") or "__" not in raw_key:
            continue
        body = raw_key[len("HIVE_"):]
        group_tok, _, field_tok = body.partition("__")
        group = group_by_upper.get(group_tok.upper())
        if group is None:
            _log.warning("config.env_unknown_group key=%s ignored", raw_key)
            continue
        typ = _GROUP_TYPES[group]
        field_by_upper = {f.name.upper(): f for f in fields(typ)}
        fld = field_by_upper.get(field_tok.upper())
        if fld is None:
            _log.warning("config.env_unknown_field key=%s ignored", raw_key)
            continue
        try:
            merged[group][fld.name] = _coerce(raw_val, fld.type, fld.name)
        except (ValueError, TypeError):
            # WARN with field name + target type ONLY — never the raw value (secret-safe)
            _log.warning("config.env_not_coercible field=%s.%s target_type=%s ignored",
                         group, fld.name, _type_name(fld.type))


def _type_name(decl: Any) -> str:
    return getattr(decl, "__name__", str(decl))


def _coerce(value: str, decl: Any, field_name: str) -> Any:
    """Coerce a string env value to a declared field type (int/float/bool/str). ``decl`` is the
    field's annotation, which under ``from __future__ import annotations`` is always a string, so
    the base type is resolved from that string."""
    t = _annotation_base(decl)
    if t is bool:
        low = value.strip().lower()
        if low in ("1", "true", "yes", "on"):
            return True
        if low in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"not a bool: {field_name}")
    if t is int:
        return int(value)
    if t is float:
        return float(value)
    return value                         # str / Optional[str] / unknown → leave as string


def _annotation_base(decl: Any) -> Any:
    """Resolve a string/forward annotation to a coercion base type (best-effort)."""
    s = str(decl)
    if "int" in s:
        return int
    if "float" in s:
        return float
    if "bool" in s:
        return bool
    return str


