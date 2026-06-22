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
    H_frac_max: float = 0.5
    recall_top_n: int = 10
    softmax_beta: float = 16.0            # gate mass temperature (β>0)
    # the top-1 score-gap abstention floor: suppress when the top hit's softmax mass is
    # below tau_top1. Ships inert (0.0 ⇒ never fires, masses ∈ [0,1]); only ADDS abstentions.
    # No upper clamp — a value > 1 is a legal permanent-abstain config.
    tau_top1: float = 0.0

    def __post_init__(self) -> None:
        if not (0.0 < self.H_frac_max <= 1.0):
            raise ValueError(
                f"recall.H_frac_max must be in (0.0, 1.0] (the never-hallucinate floor; "
                f"0 or >1 silently disables the gate); got {self.H_frac_max}")
        if self.recall_top_n < 1:
            raise ValueError(f"recall.recall_top_n must be >= 1 (got {self.recall_top_n})")
        if not self.softmax_beta > 0.0:
            raise ValueError(f"recall.softmax_beta must be > 0 (got {self.softmax_beta})")
        if not (math.isfinite(self.tau_top1) and self.tau_top1 >= 0.0):
            raise ValueError(
                f"recall.tau_top1 must be finite and >= 0.0 (got {self.tau_top1})")


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

    def __post_init__(self) -> None:
        for name in ("demand_m", "demand_window_days",
                     "quarantine_ttl_days", "provisional_ttl_days"):
            v = getattr(self, name)
            if int(v) < 1:
                raise ValueError(f"autonomy.{name} must be >= 1 (got {v})")
        for name in ("demand_tau", "competitor_tau"):
            v = getattr(self, name)
            if not (math.isfinite(v) and 0.0 < v <= 1.0):
                raise ValueError(f"autonomy.{name} must be finite in (0, 1] (got {v})")


@dataclass(frozen=True)
class ConflictConfig:
    """Conflict/redundancy SURFACING knobs (detection + the advisory flag). ``enabled``
    gates DETECTION (the recall carrier + the health worklist) and the ``hive_flag``
    advisory verb ONLY. The orthogonal ``suppress`` switch gates SERVE-TIME PRUNING (recall
    drops the strictly-lower-trust member of a detected near-dup/contradiction pair) — transient
    per-query presentation (the §8.4 dedup/shadow slot), never O7 auto-resolution; the resolution
    verb ``hive_supersede`` is ALWAYS on (Law 3 human vouch) and retirement stays human under
    either switch. DETECTION (``enabled``) ships ON by default — the recall ``conflicts`` carrier,
    the ``hive_health`` worklist, and the ``hive_flag`` advisory verb are live so the fleet surfaces
    conflicts for human resolution by default; ``suppress`` ships OFF (opt-in). Set ``enabled=false``
    to restore the byte-inert envelope (no ``conflicts`` key, ``hive_flag`` → disabled). The
    detection METHOD (cosine+polarity) is encoded in code, never a swap knob (THEORY §14);
    ``tau`` is the one genuinely-empirical knob (the near-dup cosine floor). Default 0.80:
    measured Qwen3 cosines put genuine paraphrase/contradiction pairs at ~0.81-0.87 while
    distinct same-subsystem facts top out ~0.69, so 0.80 sits in that gap (recall over the
    stricter 0.85 at no measured false-positive cost, with margin above the distinct-fact
    ceiling). It stays a per-deployment knob — recalibrate on the real corpus via the
    benchmark; the asymmetric cost (a false positive can lead a human to retire a real
    memory) argues for keeping margin rather than chasing the lowest-cosine conflicts."""
    enabled: bool = True
    tau: float = 0.80
    top_n: int = 10
    suppress: bool = False

    def __post_init__(self) -> None:
        if not (math.isfinite(self.tau) and 0.0 < self.tau <= 1.0):
            raise ValueError(f"conflict.tau must be finite in (0, 1] (got {self.tau})")
        if self.top_n < 1:
            raise ValueError(f"conflict.top_n must be >= 1 (got {self.top_n})")


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
    "retention": RetentionConfig,
    "obs": ObservabilityConfig,
}
# field-groups constructed (and thus validated) BEFORE runtime, so a field-level error
# (e.g. recall.H_frac_max) surfaces ahead of the db_path-required check. Derived from
# _GROUP_TYPES (its insertion order) minus runtime — one source of truth for the group set.
_FIELD_GROUP_ORDER = tuple(g for g in _GROUP_TYPES if g != "runtime")


@dataclass(frozen=True)
class Config:
    runtime: RuntimeConfig
    embedding: EmbeddingConfig
    recall: RecallConfig
    autonomy: AutonomyConfig
    conflict: ConflictConfig
    retention: RetentionConfig
    obs: ObservabilityConfig

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
        # highest-precedence, most-explicit layer (e.g. recall={"H_frac_maxx": 0.4}) — fail fast
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


