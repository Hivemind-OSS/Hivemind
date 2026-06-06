# 03 — Module Specs (index)

> Step 3: each module fully spec'd in parallel, with a first-class test contract and an independent design-review.

| # | Module | Disposition | Review scores (cplx/cogload/leak/ext/nav/contract/test) | Verdict |
|---|---|---|---|---|
| 1 | [M01 EmbeddingProvider](M01-embed.md) | PORT+FLIP+SIMPLIFY of `cls_memory/embedd | 3/3/3/8/8/7/6 | STRONG design, NOT YET build-ready on the test contract. Thi |
| 2 | [M02 VectorIndex](M02-index.md) | PORT+SIMPLIFY of storage/vector_index.py | 3/3/2/8/8/7/7 | STRONG DESIGN, NEAR BUILD-READY — sign-off blocked by a smal |
| 3 | [M03 EpisodeStore + ledgers](M03-store.md) | PORT+SIMPLIFY of storage/ (persistence.p | 6/6/4/7/7/7/5 | A genuinely deep module with a strong, mostly enforced contr |
| 4 | [M04 Recall pipeline](M04-recall.md) | PORT+SIMPLIFY serving/sources/native_sou | 3/4/3/8/8/6/5 | STRONG design, NOT YET build-ready. The architecture is genu |
| 5 | [M05 Admission path](M05-admit.md) | BUILD-NEW for the two genuinely-absent p | 4/4/3/7/5/6/5 | CONDITIONAL — strong information-hiding design with a genuin |
| 6 | [M06 MCP surface](M06-mcp.md) | PORT+EXTEND of serving/mcp_server.py + s | 3/4/3/8/8/7/5 | STRONG DESIGN, TEST CONTRACT NOT BUILD-READY. M06 is a genui |
| 7 | [M07 Onboarding / hive_init](M07-onboard.md) | BUILD-NEW (the hive_init handshake, the  | 3/4/3/8/8/7/5 | STRONG DESIGN, TEST CONTRACT NOT YET BUILD-READY. M07 is a g |
| 8 | [M08 Utility loop](M08-loop.md) | PORT+SIMPLIFY federation/controller.py a | 4/6/4/8/5/5/5 | STRONG DESIGN, NOT BUILD-READY. The decomposition is genuine |
| 9 | [M09 Outcome producer](M09-produce.md) | BUILD-NEW (the one genuinely new compone | 4/5/4/8/7/7/5 | STRONG ARCHITECTURE, NOT YET BUILD-READY. The core decomposi |
| 10 | [M10 Eval membrane](M10-eval.md) | PORT (per §10 map: "Eval membrane (C8) E | 4/5/3/7/7/6/6 | CONDITIONALLY BUILD-READY for the ~80% PORT surface (metrics |
| 11 | [M11 Config + observability](M11-config.md) | PORT+SIMPLIFY + two targeted BUILD-NEW f | 3/4/3/8/7/6/5 | STRONG DESIGN, NOT YET BUILD-READY. M11 is a genuinely deep  |
| 12 | [M12 Container / compose](M12-container.md) | BUILD-NEW (no reference file). The refer | 3/4/3/8/8/7/6 | CONDITIONAL — a genuinely deep, well-scoped runtime envelope |
