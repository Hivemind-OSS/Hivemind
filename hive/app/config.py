"""M11 — the composition-root substrate: ONE frozen, fail-fast-validated Config
resolved from a 4-layer precedence stack, plus the reload-tier state machine.

The surface is narrow (`Config.load(...)` + `reload` + `diff_tier`); the depth is the
whole layering + type-coercion + validation machine and the tier guard that refuses an
unsafe hot-swap instead of silently corrupting a warm store.

Boundary: this module WIRES, it does not COMPUTE. It depends on the registry only for
*key membership* validation (lazy import — importing `config` never imports torch). It
never reaches into `core/` domain logic.

Grounded deviations (built-decision-wins; recorded in the design deliverable):
- `recall.epsilon_explore > 0` is THE validated guardrail-1 floor [A4]; the reference's
  misplaced `producer.epsilon_explore` is DELETED (producer has no such field).
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
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping, Optional

try:                                  # py311 stdlib; present per pyproject requires-python
    import tomllib
except ModuleNotFoundError:           # pragma: no cover - defensive
    tomllib = None                    # type: ignore[assignment]

_log = logging.getLogger("hive.config")

_DEFAULT_TOML = "/data/hive.toml"
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
    d: int = 256
    W_version: int = 1

    def __post_init__(self) -> None:
        if self.d <= 0:
            raise ValueError(f"geometry.d must be > 0 (got {self.d})")
        if self.W_version < 1:
            raise ValueError(f"geometry.W_version must be >= 1 (got {self.W_version})")


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "local_st"
    model: str = "BAAI/bge-small-en-v1.5"
    st_projection_head: str = "pca"

    def __post_init__(self) -> None:
        if self.st_projection_head != "pca":
            raise ValueError(
                f"embedding.st_projection_head must be 'pca' (random JL rejected — measured "
                f"worse at every d; spec §4.1); got {self.st_projection_head!r}")
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
    epsilon_explore: float = 0.1          # [A4] guardrail-1 — the ONE validated ε (>0)
    softmax_beta: float = 16.0            # gate mass temperature (β>0)

    def __post_init__(self) -> None:
        if not (0.0 < self.H_frac_max <= 1.0):
            raise ValueError(
                f"recall.H_frac_max must be in (0.0, 1.0] (the never-hallucinate floor; "
                f"0 or >1 silently disables the gate); got {self.H_frac_max}")
        if self.recall_top_n < 1:
            raise ValueError(f"recall.recall_top_n must be >= 1 (got {self.recall_top_n})")
        if not self.epsilon_explore > 0.0:
            raise ValueError(
                f"recall.epsilon_explore must be > 0 (§4.7 guardrail-1 — a 0 starves novel "
                f"memories of exposure); got {self.epsilon_explore}")
        if not self.softmax_beta > 0.0:
            raise ValueError(f"recall.softmax_beta must be > 0 (got {self.softmax_beta})")


@dataclass(frozen=True)
class ProducerConfig:
    """Vestigial after the producer strip: only ``stamp_trailer`` survives — the git
    trailer key the (deferred) credit Walk would key on, and the single source for the
    onboarding rules block's ``<TRAILER_KEY>``. The watch_repos / assoc / poll / provider
    fields went with the producer subsystem. An empty trailer is rejected downstream
    (onboard.render_rules_block / RulesBlock), where it would actually cause harm."""
    stamp_trailer: str = "Hive-Trace"


@dataclass(frozen=True)
class UtilityConfig:
    isolation_frac: float = 0.05         # [A5] guardrail-2 held-out slice (tier A)
    prediction_bias_window_s: int = 604800
    prediction_bias_threshold: float = 0.25
    f_min: float = 0.5                   # utility→weight factor bounds (surfacer; observed-only P1)
    f_max: float = 1.5

    def __post_init__(self) -> None:
        if not (0.0 <= self.isolation_frac < 1.0):
            raise ValueError(
                f"utility.isolation_frac must be in [0.0, 1.0) (got {self.isolation_frac})")
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
    "producer": ProducerConfig,
    "utility": UtilityConfig,
    "autonomy": AutonomyConfig,
    "retention": RetentionConfig,
    "obs": ObservabilityConfig,
}
# field-groups constructed (and thus validated) BEFORE runtime, so a field-level error
# (e.g. recall.epsilon_explore) surfaces ahead of the db_path-required check.
_FIELD_GROUP_ORDER = ("geometry", "embedding", "index", "recall",
                      "producer", "utility", "autonomy", "retention", "obs")


@dataclass(frozen=True)
class Config:
    runtime: RuntimeConfig
    geometry: GeometryConfig
    embedding: EmbeddingConfig
    index: IndexConfig
    recall: RecallConfig
    producer: ProducerConfig
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
        toml_path: Optional[str] = _DEFAULT_TOML,
        env: Optional[Mapping[str, str]] = None,
        **overrides: Mapping[str, Any],
    ) -> "Config":
        """Resolve a frozen Config from group defaults < TOML < HIVE_*env < explicit overrides.

        `db_path` (positional/kw) or `overrides["runtime"]["db_path"]` set the store path
        (default `:memory:`). `toml_path=None` skips the file layer (used by tests).
        Validation is fail-fast: the first illegal field raises `ValueError`.
        """
        env = os.environ if env is None else env
        # layer 1: per-group default kwargs
        merged: dict[str, dict[str, Any]] = {g: {} for g in _GROUP_TYPES}
        # layer 2: TOML
        if toml_path:
            _apply_toml(merged, toml_path)
        # layer 3: HIVE_<GROUP>__<FIELD> env
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
    flds = {f.name: f for f in fields(typ)}
    kwargs: dict[str, Any] = {}
    for k, v in vals.items():
        if k not in flds:
            continue
        # a TOML/override sequence (list) for a tuple-declared field is normalized to a tuple,
        # so a frozen Config stays hashable and diff_tier compares like-for-like (env already
        # coerces to tuple; this closes the TOML/override path).
        if isinstance(v, list) and "tuple" in str(flds[k].type):
            v = tuple(v)
        kwargs[k] = v
    return typ(**kwargs)


def _apply_toml(merged: dict[str, dict[str, Any]], toml_path: str) -> None:
    if tomllib is None or not os.path.exists(toml_path):
        _log.info("config.no_toml path=%s — env-only config", toml_path)
        return
    try:
        with open(toml_path, "rb") as fh:
            data = tomllib.load(fh)
    except Exception as exc:                                  # noqa: BLE001 — never crash on bad TOML
        _log.warning("config.toml_ignored path=%s reason=%s", toml_path, type(exc).__name__)
        return
    for group, vals in data.items():
        if group in merged and isinstance(vals, dict):
            known = {f.name for f in fields(_GROUP_TYPES[group])}
            for k in vals:
                if k not in known:
                    _log.warning("config.toml_unknown_field field=%s.%s ignored", group, k)
            merged[group].update(vals)
        else:
            _log.warning("config.toml_unknown_group group=%s ignored", group)


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
    if t is tuple:                       # comma-separated list → tuple[str,...]
        return tuple(s for s in (p.strip() for p in value.split(",")) if s)
    return value                         # str / Optional[str] / unknown → leave as string


def _annotation_base(decl: Any) -> Any:
    """Resolve a string/forward annotation to a coercion base type (best-effort)."""
    s = str(decl)
    if "int" in s and "tuple" not in s:
        return int
    if "float" in s:
        return float
    if "bool" in s:
        return bool
    if "tuple" in s:
        return tuple
    return str


# ── reload tier state machine ─────────────────────────────────────────────────
class TierViolation(RuntimeError):
    """A reload that changes a tier-C (restart) or tier-D (re-embed/migration) field is
    refused — applying it live would corrupt a warm store or silently diverge the index."""


# RELOAD_TIER: "<group>.<field>" -> "A".."D". Greppable pure data. D=re-embed/migration,
# C=restart, B=hot-swap, A=next-run. Strictest changed field governs.
RELOAD_TIER: dict[str, str] = {
    # D — changing these requires a re-embed / store migration
    "geometry.d": "D",
    "geometry.W_version": "D",
    "embedding.model": "D",
    "embedding.st_projection_head": "D",
    # C — safe only across a process restart
    "embedding.provider": "C",
    "index.backend": "C",
    "runtime.db_path": "C",
    "runtime.tenant_id": "C",
    "autonomy.enabled": "C",        # flips tool behavior + trigger wiring → restart
    # B — hot-swappable live
    "autonomy.demand_m": "B",
    "autonomy.demand_window_days": "B",
    "autonomy.demand_tau": "B",
    "autonomy.competitor_tau": "B",
    "autonomy.quarantine_ttl_days": "B",
    "autonomy.provisional_ttl_days": "B",
    "recall.H_frac_max": "B",
    "recall.epsilon_explore": "B",
    "recall.softmax_beta": "B",
    "utility.f_min": "B",
    "utility.f_max": "B",
    "producer.stamp_trailer": "B",
    # A — applied on next run (no restart, no migration)
    "recall.recall_top_n": "A",
    "utility.isolation_frac": "A",            # tier A per build-plan §638
    "utility.prediction_bias_window_s": "A",
    "utility.prediction_bias_threshold": "A",
    "retention.backup_keep": "A",
    "retention.backup_dir": "A",
    "obs.log_level": "A",
    "obs.log_max_bytes": "A",
    "obs.log_backup_count": "A",
    "obs.log_file": "A",
}

_TIER_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}
_TIER_REMEDY = {
    "C": "restart the server to apply this change",
    "D": "bump geometry.W_version and run the re-embed migration",
}


def _flatten(cfg: Config) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for f in fields(cfg):
        group = getattr(cfg, f.name)
        if is_dataclass(group):
            for gf in fields(group):
                out[f"{f.name}.{gf.name}"] = getattr(group, gf.name)
    return out


def diff_tier(old: Config, new: Config) -> str:
    """The strictest reload tier among all changed fields. No change ⇒ "A" (loosest).
    An unknown changed field is treated as "D" (conservative — refuse). // O(#fields)."""
    of, nf = _flatten(old), _flatten(new)
    strictest = "A"
    for key in set(of) | set(nf):
        if of.get(key) != nf.get(key):
            tier = RELOAD_TIER.get(key, "D")
            if _TIER_RANK[tier] > _TIER_RANK[strictest]:
                strictest = tier
    return strictest


def reload(old: Config, new: Config) -> Config:
    """Apply `new` iff every changed field is tier A/B (hot-swap/next-run safe); else raise
    `TierViolation` naming the field, its tier, and the exact remediation."""
    of, nf = _flatten(old), _flatten(new)
    offenders: list[tuple[str, str]] = []
    for key in set(of) | set(nf):
        if of.get(key) != nf.get(key):
            tier = RELOAD_TIER.get(key, "D")
            if tier in ("C", "D"):
                offenders.append((key, tier))
    if offenders:
        field_name, tier = sorted(offenders, key=lambda kv: -_TIER_RANK[kv[1]])[0]
        remedy = _TIER_REMEDY[tier]
        _log.warning("config.reload_refused field=%s tier=%s remedy=%s",
                     field_name, tier, remedy)
        raise TierViolation(
            f"reload refused: {field_name} is tier {tier}; {remedy}")
    return new
