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
- `recall.epsilon_explore > 0` is THE validated guardrail-1 floor [A4].
- `db_path` defaults to `":memory:"` (ephemeral — cannot corrupt a persistent store) so a
  no-db_path `Config.load(...)` resolves; an EMPTY string is the fail-fast trigger. The
  "no silent default" prose guards a *persistent* store; `:memory:` is not one.
- env field/group tokens are matched case-INSENSITIVELY (upper-fold) against the dataclass
  names — the `__` separator namespaces group from field, structurally closing the old
  upper-case `CORTEX_D` collision (and sparse `geometry.D` is dropped, so no case-clash
  survives within any group).
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
_AUTHORITATIVE_BACKENDS = frozenset({"exhaustive"})


# ── frozen config groups ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class RuntimeConfig:
    db_path: str = _MEMORY_DB
    tenant_id: str = "default"

    def __post_init__(self) -> None:
        if not self.db_path:
            raise ValueError("runtime.db_path is required (no silent default); set db_path=...")


@dataclass(frozen=True)
class GeometryConfig:
    # Changing d or W_version needs a re-embed (reembed_from_text), not just a restart —
    # the stored vectors were projected through the old geometry.
    d: int = 256
    W_version: int = 1

    def __post_init__(self) -> None:
        if self.d <= 0:
            raise ValueError(f"geometry.d must be > 0 (got {self.d})")
        if self.W_version < 1:
            raise ValueError(f"geometry.W_version must be >= 1 (got {self.W_version})")


@dataclass(frozen=True)
class EmbeddingConfig:
    # The projection head is FIXED to PCA — not a config knob. Random JL was measured
    # worse at every d, so the decision is encoded in code (the local_st adapter builds
    # PCA unconditionally), never offered as a choice an operator could mis-set.
    provider: str = "local_st"
    model: str = "BAAI/bge-small-en-v1.5"   # changing the model needs a re-embed, not just a restart

    def __post_init__(self) -> None:
        from hive.app.registry import EMBEDDING_PROVIDERS   # lazy — no torch at import
        if self.provider not in EMBEDDING_PROVIDERS:
            raise ValueError(
                f"embedding.provider {self.provider!r} unknown; "
                f"valid={sorted(EMBEDDING_PROVIDERS)}")


@dataclass(frozen=True)
class IndexConfig:
    backend: str = "exhaustive"

    def __post_init__(self) -> None:
        from hive.app.registry import INDEX_PROVIDERS    # lazy
        valid = _AUTHORITATIVE_BACKENDS | set(INDEX_PROVIDERS)
        if self.backend not in valid:
            raise ValueError(f"index.backend {self.backend!r} unknown; valid={sorted(valid)}")


@dataclass(frozen=True)
class RecallConfig:
    H_frac_max: float = 0.5
    recall_top_n: int = 10
    epsilon_explore: float = 0.1          # [A4] guardrail-1 ε (>0); INERT on the live path — the Phase-1 surfacer is enabled=False, so it's consumed only by the offline research/eval harnesses
    softmax_beta: float = 16.0            # gate mass temperature (β>0)
    hybrid: bool = False                  # lexical(FTS5)+RRF channel; flips only on channel_eval CI evidence
    shadow: bool = False                  # CV3 serve-time version shadowing; OFF ⇒ byte-identical (golden)
    shadow_tau: float = 0.95              # pairwise cosine at/above which the loser is hidden
    drafts: bool = False                  # self-quarantine resurfacing read channel; OFF ⇒ byte-inert wire
    draft_tau: float = 0.6                # query↔own-quarantined cosine floor to surface a draft

    def __post_init__(self) -> None:
        if not (math.isfinite(self.shadow_tau) and 0.0 < self.shadow_tau <= 1.0):
            raise ValueError(
                f"recall.shadow_tau must be finite in (0, 1] (got {self.shadow_tau})")
        if not (math.isfinite(self.draft_tau) and 0.0 < self.draft_tau <= 1.0):
            raise ValueError(
                f"recall.draft_tau must be finite in (0, 1] (got {self.draft_tau})")
        if not (0.0 < self.H_frac_max <= 1.0):
            raise ValueError(
                f"recall.H_frac_max must be in (0.0, 1.0] (the never-hallucinate floor; "
                f"0 or >1 silently disables the gate); got {self.H_frac_max}")
        if self.recall_top_n < 1:
            raise ValueError(f"recall.recall_top_n must be >= 1 (got {self.recall_top_n})")
        if not self.epsilon_explore > 0.0:
            raise ValueError(
                f"recall.epsilon_explore must be > 0 (guardrail-1 — a 0 starves novel "
                f"memories of exposure); got {self.epsilon_explore}")
        if not self.softmax_beta > 0.0:
            raise ValueError(f"recall.softmax_beta must be > 0 (got {self.softmax_beta})")


@dataclass(frozen=True)
class UtilityConfig:
    prediction_bias_window_s: int = 604800
    prediction_bias_threshold: float = 0.25
    # utility→weight factor bounds — INERT on the live path (Phase-1 surfacer is
    # enabled=False); consumed only by the offline research/eval harnesses.
    f_min: float = 0.5
    f_max: float = 1.5

    def __post_init__(self) -> None:
        if self.prediction_bias_window_s <= 0:
            raise ValueError(
                f"utility.prediction_bias_window_s must be > 0 (got {self.prediction_bias_window_s})")
        if not (0.0 < self.prediction_bias_threshold):
            raise ValueError(
                f"utility.prediction_bias_threshold must be > 0 (got {self.prediction_bias_threshold})")
        if not (self.f_min <= self.f_max):
            raise ValueError(f"utility.f_min ({self.f_min}) must be <= f_max ({self.f_max})")


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
    # CV2 survival-establish (the 2nd mechanical rung; rides `enabled`):
    survival_e: int = 2             # distinct non-writer identities required
    survival_days: int = 14         # minimum first-to-last exposure span
    survival_min_exposures: int = 5
    # CV1 solo mode: a single-seat fleet swaps the demand rule's identity-
    # diversity clause for elapsed-span demand. Operator-set env, NOT client-gameable.
    solo_mode: bool = False
    solo_min_span_days: int = 1     # first-to-last matched-miss span required to promote
    # CV3 contested-memory report: a miss cluster whose representative sits this
    # close to a SERVABLE row marks that row contested (supersession-review queue).
    contested_tau: float = 0.80

    def __post_init__(self) -> None:
        for name in ("demand_m", "demand_window_days",
                     "quarantine_ttl_days", "provisional_ttl_days",
                     "survival_e", "survival_days", "survival_min_exposures",
                     "solo_min_span_days"):
            v = getattr(self, name)
            if int(v) < 1:
                raise ValueError(f"autonomy.{name} must be >= 1 (got {v})")
        for name in ("demand_tau", "competitor_tau", "contested_tau"):
            v = getattr(self, name)
            if not (math.isfinite(v) and 0.0 < v <= 1.0):
                raise ValueError(f"autonomy.{name} must be finite in (0, 1] (got {v})")


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
    log_max_bytes: int = 10 * 1024 * 1024
    log_backup_count: int = 5
    log_file: Optional[str] = None


# ── the root ──────────────────────────────────────────────────────────────────
_GROUP_TYPES: dict[str, type] = {
    "runtime": RuntimeConfig,
    "geometry": GeometryConfig,
    "embedding": EmbeddingConfig,
    "index": IndexConfig,
    "recall": RecallConfig,
    "utility": UtilityConfig,
    "autonomy": AutonomyConfig,
    "retention": RetentionConfig,
    "obs": ObservabilityConfig,
}
# field-groups constructed (and thus validated) BEFORE runtime, so a field-level error
# (e.g. recall.epsilon_explore) surfaces ahead of the db_path-required check.
_FIELD_GROUP_ORDER = ("geometry", "embedding", "index", "recall",
                      "utility", "autonomy", "retention", "obs")


@dataclass(frozen=True)
class Config:
    runtime: RuntimeConfig
    geometry: GeometryConfig
    embedding: EmbeddingConfig
    index: IndexConfig
    recall: RecallConfig
    utility: UtilityConfig
    autonomy: AutonomyConfig
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
    """Coerce a string env value to a declared field type (int/float/bool/str/tuple)."""
    t = decl if isinstance(decl, type) else _annotation_base(decl)
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


