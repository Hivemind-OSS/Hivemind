"""In-memory conforming fakes for the pure-domain test suite. Each satisfies its
runtime_checkable Protocol; behavior is the minimum the slice needs (richer
behavior is added as later chunks require it). NO sqlite/git/torch/wall-clock."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
from typing import Iterator, Optional, Sequence

import numpy as np

from hive.domain.models import (
    RULES_BLOCK_END, RULES_BLOCK_START,
    Episode, InstallPlan, RulesBlock, SettledExposure,
    UtilityPosterior, content_hash, rules_block_version_marker,
)
from hive.domain.secret_scan import REFUSE, ScanVerdict
from hive.domain.secret_scan import scan as _scan

from hive.app.onboard import HOOK_MANIFEST, build_recipe, resolve_profile


class FakeClock:
    def __init__(self, t: int = 0) -> None:
        self._t = int(t)

    def now(self) -> int:
        return self._t

    def advance(self, dt: int) -> None:
        self._t += int(dt)

    def set(self, t: int) -> None:
        self._t = int(t)


class FakeProvider:
    """Deterministic hash-based unit vectors — similar text → similar vectors."""
    def __init__(self, d: int = 256, w_version: int = 1) -> None:
        self.d = int(d)
        self.w_version = int(w_version)

    def _vec(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode(self, text: str) -> np.ndarray:
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self._vec(t) for t in texts], axis=0)


class FakeWarmProvider:
    """A deterministic hash-based ``EmbeddingProvider`` that ALSO satisfies the M12 warm
    contract (``.loaded`` / ``.load()`` / ``.name`` / ``.head_bytes()``) — for fast
    container-WIRING tests that must not pay the real bge model load. Hash-per-text means
    only IDENTICAL text matches (semantic recall quality is proven against the real
    embedder in tests/acceptance/*)."""
    name = "fake-warm"

    def __init__(self, d: int = 256, w_version: int = 1, loaded: bool = False) -> None:
        self.d = int(d)
        self.w_version = int(w_version)
        self.loaded = bool(loaded)

    def load(self) -> "FakeWarmProvider":
        self.loaded = True
        return self

    def head_bytes(self) -> bytes:
        return b"FAKEHEAD\x00" + self.w_version.to_bytes(2, "big")

    def _vec(self, text: str) -> np.ndarray:
        h = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
        v = rng.standard_normal(self.d).astype(np.float32)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def encode(self, text: str) -> np.ndarray:
        if not self.loaded:
            self.load()
        return self._vec(text)

    def encode_batch(self, texts: Sequence[str]) -> np.ndarray:
        if not self.loaded:
            self.load()
        rows = list(texts)
        if not rows:
            return np.zeros((0, self.d), dtype=np.float32)
        return np.stack([self._vec(t) for t in rows], axis=0)


class FakeIndex:
    """Exhaustive cosine over an in-memory {eid: unit-vec}. Authoritative."""
    def __init__(self) -> None:
        self._rows: dict[int, np.ndarray] = {}

    def add(self, episode_id: int, value: np.ndarray) -> None:
        self._rows[int(episode_id)] = np.asarray(value, dtype=np.float32)

    def remove(self, episode_id: int) -> None:
        self._rows.pop(int(episode_id), None)

    def rebuild_from_store(self, store) -> None:  # noqa: ANN001 (test fake)
        self._rows.clear()

    def search(self, query: np.ndarray, k: int) -> list[tuple[int, float]]:
        q = np.asarray(query, dtype=np.float32)
        scored = [(eid, float(np.dot(q, v))) for eid, v in self._rows.items()]
        scored.sort(key=lambda t: t[1], reverse=True)
        return scored[:k]

    def is_authoritative(self) -> bool:
        return True

    def size(self) -> int:
        return len(self._rows)


def make_episode(episode_id: int, text: str, weight: float = 1.0,
                 *, status: str = "approved", trust: Optional[str] = None,
                 last_active_ts: int = 0, ts: int = 0,
                 value: Optional[np.ndarray] = None) -> Episode:
    """A valid (self-asserting) Episode for resolve-seam tests — content_hash binds
    text. An approved episode defaults to trust='established' (mirroring the real
    store, where the human-vouched flip stamps established); pass ``trust`` to model
    provisional/quarantined rows. ``value`` defaults to None (margin/surface tests
    read only text + weight + labels); the CV3 shadow tests pass the vector — the
    real store's get_episode always populates it."""
    approved = status == "approved"
    if trust is None:
        trust = "established" if approved else "quarantined"
    return Episode(
        id=int(episode_id), tenant_id="default", text=text, weight=float(weight),
        ts=int(ts), source="test", tags="", content_hash=content_hash(text),
        status=status, proposed_by="tester",
        approved_by="approver" if approved else None,
        approved_ts=0 if approved else None, version=0,
        trust=trust, last_active_ts=int(last_active_ts),
        value=None if value is None else np.asarray(value, dtype=np.float32),
    )


class FakeEpisodeReader:
    """EpisodeReader: resolves eid → Episode(text, weight). Unknown eid → None."""
    def __init__(self) -> None:
        self._by_id: dict[int, Episode] = {}

    def add(self, episode_id: int, text: str, weight: float = 1.0,
            **episode_kwargs) -> Episode:
        ep = make_episode(episode_id, text, weight, **episode_kwargs)
        self._by_id[int(episode_id)] = ep
        return ep

    def get_episode(self, episode_id: int) -> Optional[Episode]:
        return self._by_id.get(int(episode_id))


class FakeStore:
    """EpisodeStore slice (transaction + meta kv), in-memory. The exposure /
    task_outcomes ledger methods were removed with the producer subsystem."""
    def __init__(self) -> None:
        self._meta: dict[str, str] = {}

    @contextmanager
    def transaction(self) -> Iterator[None]:
        yield  # fakes are single-threaded in-memory; no real tx needed

    def meta_get(self, key: str) -> Optional[str]:
        return self._meta.get(key)

    def meta_set(self, key: str, value: str) -> None:
        self._meta[key] = value


class FakeUtilityStore:
    """Beta-Bernoulli posterior, dict-backed; same Protocol as the SQLite adapter.
    Deliberately exposes NO weight setter and NO episodes handle [A3]."""
    def __init__(self, isolation: Optional[set[int]] = None) -> None:
        self._post: dict[tuple[int, str], dict] = {}
        self._sources: dict[tuple[int, str], set[str]] = {}
        self._isolation: set[int] = set(isolation or ())
        self._layer_version = 1
        self._settled: list[SettledExposure] = []   # the guardrail-3 settled stream (A6)

    def apply_credit(self, deltas: Sequence) -> None:
        for d in deltas:
            key = (int(d.episode_id), d.family_scope)
            p = self._post.setdefault(key, {"wins": 0.0, "losses": 0.0, "version": 1})
            p["wins"] += float(d.d_wins)
            p["losses"] += float(d.d_losses)
            p["version"] += 1
            if getattr(d, "source_agent", ""):
                self._sources.setdefault(key, set()).add(d.source_agent)

    def posterior(self, episode_id: int, family_scope: str) -> UtilityPosterior:
        key = (int(episode_id), family_scope)
        p = self._post.get(key, {"wins": 0.0, "losses": 0.0, "version": 1})
        return UtilityPosterior(
            episode_id=int(episode_id), family_scope=family_scope,
            wins=p["wins"], losses=p["losses"],
            n_sources=len(self._sources.get(key, set())),
            version=p["version"], isolation=int(episode_id) in self._isolation,
        )

    def utility_map(self, family_scope: str, *, confident_only: bool = True) -> dict[int, float]:
        out: dict[int, float] = {}
        for (eid, fam), p in self._post.items():
            if fam != family_scope:
                continue
            post = self.posterior(eid, fam)
            if confident_only and not post.ci_excludes_half():
                continue
            out[eid] = post.mean()
        return out

    def isolation_episode_ids(self) -> set[int]:
        return set(self._isolation)

    # ── guardrail-3 settled stream (A6) ──────────────────────────────────────────
    def plant_settled(self, family_scope: str, *, reward_sign: int,
                      exposed, settle_ts: int) -> None:
        """Test helper: stage one settled outcome joined with the eids it exposed.
        Stands in for what the producer tick persists when it settles a task."""
        self._settled.append(SettledExposure(
            family_scope=family_scope, reward_sign=int(reward_sign),
            exposed=tuple(int(e) for e in exposed), settle_ts=int(settle_ts)))

    def settled_exposures_since(self, family_scope: str, since_ts: int):
        """Windowed read: this family's settled outcomes with settle_ts ≥ since_ts.
        The SQLite adapter does this with idx_task_outcomes_settle ⋈ exposures; the
        list scan here is O(k) at test scale."""
        return [s for s in self._settled
                if s.family_scope == family_scope and s.settle_ts >= since_ts]

    def mark_isolation(self, episode_id: int) -> None:
        self._isolation.add(int(episode_id))

    def zero_utility_layer(self) -> None:
        self._layer_version += 1
        for p in self._post.values():
            p["wins"] = 0.0
            p["losses"] = 0.0


class FakeScanner:
    """SecretScanner wrapping the real deterministic scan. ``mode`` selects the
    secret disposition: REFUSE (default, fail-closed) or REDACT (mask-and-stage)."""
    def __init__(self, mode: str = REFUSE) -> None:
        self._mode = mode

    def scan(self, text: str) -> ScanVerdict:
        return _scan(text, mode=self._mode)


class FakeLedger:
    """ExposureLedger: in-memory recording of exposures + misses (the recall
    side-channel the pipeline writes)."""
    def __init__(self) -> None:
        self.exposures: list[dict] = []
        self.misses: list[dict] = []

    def record_exposure(self, trace_id: str, items, *, agent_id: str, ts: int) -> None:
        self.exposures.append({"trace_id": trace_id,
                               "items": [(int(e), float(m)) for e, m in items],
                               "agent_id": agent_id, "ts": int(ts)})

    def record_miss(self, query_text: str, query_vector, agent_id: str,
                    miss_type: str, *, ts: int) -> None:
        self.misses.append({"query_text": query_text, "query_vector": query_vector,
                            "agent_id": agent_id, "miss_type": miss_type, "ts": int(ts)})


class FakeInstallPlanner:
    """InstallPlanner double for the MCP surface. ``trailer_key`` is single-sourced
    from the injected ``stamp_trailer`` (the CONFIG_DRIFT guard the MCP layer
    must surface verbatim); ``plan`` does zero writes; ``confirm`` links on a matching
    hash (lie-proof); ``link_status`` backs the additive hive_health link field."""
    def __init__(self, *, stamp_trailer: str = "Hive-Trace", block_version: int = 1) -> None:
        self.stamp_trailer = str(stamp_trailer)
        self.block_version = int(block_version)
        self.linked: dict[str, bytes] = {}
        self._expected: dict[str, bytes] = {}        # repo_path -> canonical block_hash from plan()

    def _render(self, repo_path: str, harness: str, rules_file: Optional[str]) -> RulesBlock:
        body = (f"{RULES_BLOCK_START}\n"
                f"{rules_block_version_marker(self.block_version)}\n"
                f"repo={repo_path} harness={harness} "
                f"rules_file={rules_file or 'CLAUDE.md'}\n"
                f"On a durable insight, call hive_write(text=...). "
                f"Stamp commits with {self.stamp_trailer}: <trace_id>.\n"
                f"{RULES_BLOCK_END}")
        return RulesBlock(rendered_text=body, trailer_key=self.stamp_trailer,
                          block_version=self.block_version,
                          block_hash=hashlib.sha256(body.encode("utf-8")).digest())

    def plan(self, repo_path: str, harness: str,
             rules_file: Optional[str] = None) -> InstallPlan:
        block = self._render(repo_path, harness, rules_file)
        self._expected[repo_path] = block.block_hash      # remember what a faithful install hashes to
        return InstallPlan(rules_file=rules_file or "CLAUDE.md", harness=harness,
                           rules_block=block, expected_confirm_hash=block.block_hash,
                           manifest=HOOK_MANIFEST, recipe=build_recipe(harness))

    def confirm(self, repo_path: str, confirm_hash: bytes, harness: str = "generic") -> dict:
        # LIE-PROOF (mirror the real adapter): link ONLY on an exact hash match; a stale or
        # un-planned hash writes nothing and reports stale_or_wrong_block.
        expected = self._expected.get(repo_path)
        if expected is None or confirm_hash != expected:
            return {"linked": False, "error": "stale_or_wrong_block",
                    "expected": expected.hex() if expected is not None else None}
        self.linked[repo_path] = confirm_hash
        profile = resolve_profile(harness)               # mirror the real link record
        return {"linked": True, "rules_file": "CLAUDE.md", "error": None,
                "link": {"manifest_version": HOOK_MANIFEST.manifest_version,
                         "harness": profile.harness, "tier": profile.max_tier}}

    def link_status(self, repo_path: str):
        if repo_path in self.linked:
            return True, {"rules_file": "CLAUDE.md"}
        return False, None
