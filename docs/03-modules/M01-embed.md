# M01 EmbeddingProvider  (embed)

**One-line:** The single value-encode chain (bge-small ST → frozen PCA head native384→d256 → L2-normalize) hidden behind a narrow `encode/encode_batch` port, used identically by capture and recall, with the lazy-PCA-fit/random-fallback split-brain of the reference deleted so capture and recall can never silently diverge under one W_version.
**Port disposition:** PORT+FLIP+SIMPLIFY of `cls_memory/embedder.py`. PORT the `ProjectionHead.pca` math (embedder.py:74-112) and the `SentenceTransformerEmbedder` ST wrapper (embedder.py:219-317). FLIP the three defaults per spec §4.1/§10: embedder=st (drop the `auto` factory branch), st_projection_head=pca, d=256, model=BAAI/bge-small-en-v1.5. SIMPLIFY by DELETING the verified split-brain defect (embedder.py:289-306: lazy `_ensure_pca` on first batch + `embed()` random-head fallback) and replacing it with an explicit offline `fit_projection()` step that produces a frozen, serialized, W_version-stamped head loaded at construction — `encode(t) == encode_batch([t])[0]` becomes a type-level invariant. DROP `OpenAIEmbedder` and `HashingNgramEmbedder` from the runtime image (a purpose-built `FakeProvider` replaces Hashing as the test fake so no deleted class is load-bearing); DROP `auto_embedder`. KEEP the migration-only native path, renamed/fenced to `encode_native_batch` (embedder.py:308-317), used ONLY by M-migration to re-fit the head. The `ProjectionTrainer`/`fit_zca_projection` ZCA d→d head (ops/projection_trainer.py) is a DIFFERENT head and is OUT OF SCOPE for this module (that is the value-decorrelation head, not the native→d downsizing head this module owns).

---

# Module Spec — M01 EmbeddingProvider (C1 Embedder + C2 PCA head)

## 1. Responsibility (one deep module)

The single **value-encode chain**: `text → bge-small (sentence-transformers, CPU) → frozen PCA projection head (native 384 → d=256) → L2-normalize → Value`. This is the **one** encoder used by **both** capture (`hive_write`) and recall (`hive_recall`) — spec §2 diagram line 91: capture and recall must run the *same encode chain*. The deep work hidden behind a two-method surface (`encode`, `encode_batch`): model lifecycle (load-once, frozen, CPU-local), the unsupervised PCA dimensionality reduction that is *the dominant recall lever* (spec §2 C2, §4.1 — PCA > random JL at every d because it raises effective rank), unit-norm enforcement, and W_version stamping for re-embed migration.

The narrow surface hides a **verified defect deleted from the reference** (DECISIONS DIGEST, embedder-seam decision; `embedder.py:289-306`): the reference fits the PCA head *lazily on the first batch* (`_ensure_pca`) and, when `embed()` is called before any batch, *falls back to a random projection head* and assigns it permanently. Because `hive_write` stages one insight at a time (per-insight `encode`) while recall can batch, the reference could hand **capture a random projection and recall a PCA projection of the same text under one `W_version`** — a latent split-brain in a never-hallucinate store. This module **designs that out**: the PCA head is fit **once, offline**, serialized, W_version-stamped, and loaded at construction; there is no encode-time fit and no random fallback. `encode(t) == encode_batch([t])[0]` is a structural, mutation-tested invariant.

## 2. Public surface + ENFORCED contract

Port (`hive/ports/embedding.py`):

```python
@runtime_checkable
class EmbeddingProvider(Protocol):
    d: int            # output geometry dim == geometry.d == 256
    w_version: int    # geometry.W_version, stamped on every produced value
    native_dim: int   # 384 (migration fence only)
    def encode(self, text: str) -> Value: ...                       # (d,) unit-norm float32
    def encode_batch(self, texts: list[str]) -> NDArray[float32]: ...# (n, d) row-unit-norm
    def encode_native_batch(self, texts: list[str]) -> NDArray[float32]: ...  # (n, 384) MIGRATION ONLY
```

`Value = NDArray[float32]`, shape `(d,)`.

**Invariants (enforced by tests in §8, not prose):**
- **I1 (geometry):** every `encode`/`encode_batch` output has shape `(…, d)`, `dtype==float32`. *Units:* dimensionless cosine space.
- **I2 (unit-norm):** `‖encode(t)‖₂ == 1 ± 1e-5` (and every batch row). C3's cosine-kNN and C4's normalized-entropy gate *assume* unit-norm; a non-unit value silently corrupts both. This is the keystone postcondition.
- **I3 (no split-brain):** `encode(t) == encode_batch([t])[0]` (atol 1e-6). One chain, one geometry, for capture and recall.
- **I4 (determinism):** `encode(t)` is byte-identical across calls and processes given the same loaded head — no randomness, no re-fit.
- **I5 (W_version):** `provider.w_version == cfg.geometry.W_version`; the caller stamps this onto the persisted `value` so a geometry change is detectable.
- **I6 (no hot-path network):** construction loads a baked local model; `encode`/`encode_batch` make **no** network call (spec §1 local-&-self-contained, §1 hot-path no-network).

**Error semantics:**
- Construction with `head is None` (or a head whose `d_in != native_dim` or `d_out != d`) raises `GeometryError` at `__init__` — **fail fast at startup**, never at first encode.
- Model-load failure raises `ImportError`/`ModelLoadError` at construction (logged, see §6); the server does not start half-wired.
- `encode_native_batch` is the **only** native-width surface and is documented MIGRATION-ONLY; it is never called on the hot path.

**Precondition DESIGNED OUT (not documented):** the reference's "must call `embed_batch` before `embed` or you get a random head" call-order precondition is *eliminated* — the head is required and frozen at construction, so there is no order-dependent state. Likewise the native(384)-vs-projected(256) conflation is removed from the hot surface by fencing the native path to one explicitly-named migration method.

## 3. Swap seam

This module **is** the embedding-provider port (spec swap mandate; DECISIONS DIGEST embedder-seam winner B).

- **Port:** `EmbeddingProvider` Protocol above (`runtime_checkable`).
- **Default adapter:** `LocalStProvider` (bge-small via sentence-transformers, baked into the image, CPU). Selected by `embedding.transport="local"` through a fail-fast `build_provider(cfg, head_bytes)` factory.
- **A second adapter must implement exactly:** `encode`, `encode_batch`, `encode_native_batch`, and the attributes `d`, `w_version`, `native_dim`, satisfying I1–I6 (the parametrized contract test in §8 runs against it unchanged). Example second adapters: a `RemoteLoopbackProvider` (sidecar over a **loopback-only** rail — an `__init__`/health assertion that the URL is 127.0.0.1, operationalizing the hot-path no-network invariant), or an OpenAI provider. **Proof the swap needs no core change:** C3/C4/C5 consume only `Value` (a `(d,)` unit-norm float32) and never reference a concrete adapter; selection is one config key; the factory fails fast on an unknown transport. The `FakeProvider` is itself a conforming second adapter that lets the entire store/ranker/gate suite run at hash-speed offline.

## 4. Data owned

**No SQL tables.** Owns one durable **blob**: the serialized frozen PCA head, persisted by key `reembed_head_<W_version>` (reference idiom verified at `ops/migration.py:128-152`, `meta.set`/`meta.get`). Format: `ProjectionHead.to_bytes()` (the `(d_out, d_in)` float32 `W` + `w_version`). The Store (M-store) owns the kv row; this module owns the **codec** (`to_bytes`/`from_bytes`).

**Config keys read** (group.field; tiers from spec §4.1, §10 flips applied):
- `embedding.transport` = `"local"` (tier C — adapter selection)
- `embedding.embedder` = `"st"` (tier D; FLIP from `auto`)
- `embedding.st_model_name` = `"BAAI/bge-small-en-v1.5"` (tier D; FLIP from `all-MiniLM-L6-v2`)
- `embedding.st_projection_head` = `"pca"` (tier D; FLIP from `random`)
- `embedding.st_native_dim` = `384` (asserted against the loaded model, not trusted)
- `geometry.d` = `256` (tier D; promotes the dense value width per §4.1 knob-reality note — today's `d`=32/`D`=256)
- `geometry.W_version` = monotonic int (tier D; bumped on any geometry change → re-embed)

## 5. Dependencies

- **Depends on:** `sentence-transformers` + `numpy` (the only third-party libs); the hive config object (read-only, for the keys in §4); the Store's kv-meta surface to *load* its persisted head bytes at construction (passed in by the composition root as `head_bytes`, not a direct Store import — keeps the port leaf-level).
- **Must NOT know about:** the ranker (C3), the gate (C4), the staging/approval state machine, the Store's episode rows, the producer, telemetry. The boundary: this module emits a `Value` and stops. It has **zero** knowledge of whether a value is being captured or recalled, of `status=pending|approved`, or of utility. Information hiding: the projection policy (PCA, frozen, no-fallback) is fully enclosed — a caller cannot tell, and must not need to tell, PCA from random; the surface returns only `Value`.

## 6. Failure-mode logging (per engineering standard; secrets never logged)

| Boundary | Level | Context (structured JSON; **never the input text**, never secrets) |
|---|---|---|
| Model load at construction | INFO on success / **ERROR** on failure | `event=embedder_loaded`/`embedder_load_failed`, `model_name`, `native_dim`, `d`, `w_version`, `device`, `load_ms`, on failure `error`+`stack` |
| PCA head fit (offline) | INFO | `event=pca_head_fit`, `n_samples`, `d_in`, `d_out`, `w_version`, `effective_rank`, `fit_ms` |
| Head fit with too few samples | **ERROR** then raise | `event=pca_fit_underpowered`, `n_samples`, `d_out` (the reference silently random-fell-back here — now a hard, logged failure) |
| Head load at construction | INFO / **WARN** on absent / **ERROR** on incompatible | `event=head_loaded`/`head_missing`/`head_incompatible`, `w_version`, `d_in`, `d_out` |
| Geometry assertion failure (`d_in!=native_dim` or `d_out!=d`) | **ERROR** then raise `GeometryError` | `event=geometry_mismatch`, expected vs got dims, `w_version` |
| Native migration encode | DEBUG | `event=encode_native`, `n_texts`, `native_dim` (migration tick only) |
| Per-encode hot path | (no per-call log — hot path) | aggregate `encode_ms` p50/p99 emitted by the caller's telemetry, not here |

Log the **text length** at DEBUG if needed, never the text content (insights may contain near-secret context); never log the model weights or any value vector.

## 7. Port disposition vs §10 map

**PORT+FLIP+SIMPLIFY** of `embedder.py` (§10 rows C1 + C2 are both PORT+FLIP; this module additionally SIMPLIFIES by deleting the lazy-fit defect):
- **PORT** the PCA math `ProjectionHead.pca` (`embedder.py:74-112`) and the ST wrapper (`embedder.py:219-317`).
- **FLIP** the three §4.1 defaults: `embedder=st`, `st_projection_head=pca`, `st_model_name=BAAI/bge-small-en-v1.5`, `geometry.d=256`.
- **SIMPLIFY/DELETE** the split-brain (`embedder.py:281-306`: `_ensure_pca` lazy fit + `embed()` random fallback) — replaced by an offline `ProjectionHead.fit(...)` step + a frozen, serialized, W_version-stamped head loaded at construction.
- **DROP** `OpenAIEmbedder`, `auto_embedder`, and `HashingNgramEmbedder` from the runtime image; a purpose-built `FakeProvider` (conforming to the port) replaces Hashing as the test fake, so no deleted class is load-bearing (avoids the reference's over-trim footgun).
- **KEEP, fenced:** the native path (`embed_native_batch` → `encode_native_batch`) for the §6.1#4 migration re-fit (reference `ops/migration.py:39-126` reembed-from-text path).
- **OUT OF SCOPE:** `ProjectionTrainer`/`fit_zca_projection` (the d→d value-decorrelation ZCA head, a *different* head) — owned by the migration module, not this one.

## 8. TEST CONTRACT (test-first; full functional coverage)

(See `test_contract` field for the compact list; expanded mapping below.)

**Happy path:** `test_encode_returns_unit_norm_float32_d`, `test_encode_batch_rows_unit_norm`, `test_pca_fit_preserves_rank` (PCA actually raises effective rank vs random JL — the §4.1 recall lever).

**Every invariant in §2:**
- I1 → `test_encode_returns_unit_norm_float32_d` (shape/dtype).
- I2 → `test_encode_returns_unit_norm_float32_d` + `test_encode_batch_rows_unit_norm` (the C3/C4 keystone postcondition).
- I3 → `test_encode_eq_encode_batch_single` (the split-brain killer).
- I4 → `test_encode_deterministic`.
- I5 → `test_w_version_stamped`.
- I6 → asserted in `LocalStProvider`/`RemoteLoopbackProvider` health (loopback-only `__init__` assertion has its own named test on the remote adapter when added; the local adapter makes no socket).

**Every failure mode in §6:**
- model-load failure → `test_model_load_failure_logs_and_raises`.
- underpowered PCA fit → `test_pca_fit_too_few_samples_raises`.
- geometry mismatch → `test_head_rejects_dim_mismatch`, `test_geometry_assert_on_construct`.
- head persistence corruption → `test_head_roundtrip_bytes`.
- unknown transport / missing head at wiring → `test_unknown_transport_fails_fast`, `test_local_requires_head`.

**MUTATION tests (RULE 2):**
1. *Unit-norm fault* — delete `out = out / n` in `ProjectionHead.__call__`. `test_encode_returns_unit_norm_float32_d` **and** `test_encode_eq_encode_batch_single` go red; restore → green. Proves the unit-norm invariant the whole downstream (C3 cosine, C4 entropy) depends on is genuinely under test.
2. *Split-brain fault* — reintroduce a lazy `if head is None: head = ProjectionHead.random(...)` in `encode()`. `test_no_lazy_fit_no_random_fallback` goes red; restore → green. Proves the deleted reference defect cannot silently return.

**§6 acceptance-gate mapping:** this module owns its half of **§6.1 #4 (migration round-trip)** — `test_head_roundtrip_bytes` + `test_w_version_stamped` prove a `W_version` bump re-keys through a serializable frozen head with no silent corruption. (The full re-embed sweep over the corpus is owned by the migration module; this module guarantees the head it persists round-trips and stamps correctly.) The §6.4 mutation-testing mandate for "any code implementing a gate or ranker" is satisfied here for the *encoder* via the two mutations above; the gate/ranker mutations live in their own module specs.

**Coverage claim:** every public method, every invariant I1–I6, every §6 boundary, and both reintroduction-of-deleted-defect risks have a named failing-first test — no functional path is untested.

---

## Design review (independent pass)

**Verdict:** STRONG design, NOT YET build-ready on the test contract. This is a genuinely DEEP module (APOSD #4): a two-method hot surface (encode/encode_batch) hides model lifecycle, the dominant-recall-lever PCA reduction, unit-norm enforcement, and W_version stamping. Its single best move is applying 'Define errors out of existence' (APOSD #11 / agent-native §7) to the reference's verified split-brain: the spec converts an order-dependent precondition (must call embed_batch before embed or get a random head, embedder.py:289-299 — CONFIRMED at those lines: embed() assigns ProjectionHead.random when projection is None while embed_batch() fits PCA via _ensure_pca) into a structural impossibility (head frozen at construction), and makes I3 encode(t)==encode_batch([t])[0] a mutation-tested invariant. The port-disposition, swap seam (FakeProvider as a conforming second adapter proving zero core change), and failure-mode logging table are all well-grounded in the spec and reference. Scores are held back from a clean sign-off by three things the spec itself can fix: (1) a SERIALIZATION CONTRACT GAP — the spec asserts it PORTs ProjectionHead.to_bytes()/from_bytes(), but the reference has NO such method; the real codec is np.save+base64+JSON in migration.py:157-181 and critically does NOT serialize W_version (version is the dict key only, load_head injects it from the caller). The spec's new to_bytes claims to embed w_version, which is a net-new contract, yet test_head_roundtrip_bytes only asserts byte round-trip and test_w_version_stamped asserts the provider attribute equals cfg — NEITHER test proves w_version survives the serialize/deserialize boundary, which is the exact silent-corruption the migration gate (§6.1#4) exists to catch. (2) Determinism (I4) is claimed byte-identical 'across processes' but bge-small via sentence-transformers on CPU is not guaranteed bitwise-reproducible across BLAS/threading; the test as worded risks being either flaky or quietly downgraded to atol. (3) The hot-path no-network invariant (I6) — a PRODUCT invariant — has NO test on the shipped LocalStProvider; the spec explicitly defers it to a not-yet-built remote adapter, so the one adapter that actually ships has its keystone invariant unenforced. Fix the three must_fix items and this is a build-ready, exemplary module spec.

**Scores (1–10):**
- design_complexity: 3
- cognitive_load: 3
- information_leakage: 3
- extensibility_fit: 8
- agent_navigability: 8
- contract_enforcement: 7
- test_coverage: 6

**Red flags:**
- Prose-Only / Stale Contract @ §7 'PORT the PCA math ... and to_bytes/from_bytes codec' — claims to port a serialization method that does not exist in the reference (grep of embedder.py + types.py: no to_bytes/from_bytes); the real codec lives in migration.py:157-181 with a different format and no W_version-in-blob. root: obscurity -> an agent following §7 will look for a method to port, not find it, and either invent a format or miscopy the migration codec; the disposition lies about reuse.
- Special-General Mixture @ §2 encode_native_batch on the EmbeddingProvider Protocol — a MIGRATION-ONLY, native-width (384) method sits on the same surface every hot-path caller (C3/C4) sees; the spec fences it by docstring/naming only, not by type. root: obscurity -> a caller can call the un-projected native path on the hot path and corrupt the d-dim geometry with no compile/type error; the fence is prose, not enforced. Mitigated (good naming, MIGRATION-ONLY label) but it is still surface a common-case caller must learn to ignore (Overexposure-adjacent).
- Missing Feedback Signal @ I4 determinism + I6 no-network on LocalStProvider — both are asserted as invariants but the shipping adapter has no enforcing test for either (I4 untestable as worded across processes; I6 deferred to a future remote adapter). root: obscurity -> the two invariants that distinguish 'self-contained local store' from 'flaky networked encoder' have no failing-first test an agent could use to detect a regression (e.g. someone adds a model-download-on-miss path, or a non-deterministic pooling op).
- Hard-to-Describe precondition residue @ §6 'Head fit with too few samples -> ERROR then raise (pca_fit_underpowered)' — the offline fit path's minimum-n is named in the log table and a test (test_pca_fit_too_few_samples_raises) but the THRESHOLD (n >= ? as a function of d_out) is unspecified; PCA needs n > d_in for a full-rank covariance, not merely n > d_out. root: obscurity -> the test asserts 'raises' without pinning the boundary, so the implementation's chosen threshold can be wrong (e.g. n==d_out+1, a rank-deficient covariance) and still pass. Specify n >= native_dim (or the explicit rule) so the test has a real boundary to defend.

**Must-fix:**
- SERIALIZATION CONTRACT: The spec claims §7 PORTs ProjectionHead.to_bytes()/from_bytes(), but no such method exists in embedder.py or types.py — the reference codec is np.save+base64+JSON inside migration.py:157-181 and does NOT serialize W_version (it is the meta-key only; load_head injects version from the caller). Either (a) explicitly mark to_bytes/from_bytes as BUILD-NEW (not PORT) and define the exact byte layout (magic/version header, dtype, shape, w_version field, endianness), or (b) reuse the reference codec. Without this the §7 disposition is a lying contract (agent-native red flag: Stale/Prose-Only Contract).
- TEST GAP — W_version must survive the codec boundary: Add a test (e.g. test_head_bytes_roundtrip_preserves_w_version) asserting from_bytes(to_bytes(head)).w_version == head.w_version AND .W is bit-identical AND d_in/d_out match. Current test_head_roundtrip_bytes (bytes round-trip) + test_w_version_stamped (provider attr == cfg) leave a hole exactly where the reference silently dropped version into the dict key — this is the §6.1#4 migration-corruption the gate must catch, and it is presently untested.
- TEST GAP — hot-path no-network (I6) is untested on the SHIPPING adapter: I6 is a PRODUCT invariant (local & self-contained, no hot-path network) but §8 only assigns it a test on the not-yet-built RemoteLoopbackProvider. Add a test on LocalStProvider that asserts encode()/encode_batch() open no socket (monkeypatch socket.socket / socket.create_connection to raise inside the encode call). The one adapter that ships in the image must prove its own keystone invariant, not inherit it from a future adapter.
- DETERMINISM (I4) is over-claimed and untestable as worded: 'byte-identical across calls and processes' is not guaranteed for bge-small/sentence-transformers on CPU across BLAS backends and thread counts. Re-scope I4 to byte-identical within one loaded provider in one process (which IS testable and is what the no-refit/no-randomness design actually guarantees), and weaken any cross-process claim to atol-equality or drop it. As written test_encode_deterministic is either flaky or a no-op.
- TEST GAP — empty / degenerate input has no contract: encode('') and encode_batch([]) have undefined behavior. The reference HashingNgramEmbedder mapped '' to '<empty>', but bge-small on '' yields a real (possibly near-zero) vector that the L2-normalize divide-guard (n>0 in ProjectionHead.__call__) silently leaves UN-normalized, breaking I2 for that row. Specify the contract (normalize, or raise ValueError) and add test_encode_empty_string and test_encode_batch_empty_list — this is the exact branch the mutation 'delete out=out/n' hides behind because the divide-guard already skips normalization at zero norm.
