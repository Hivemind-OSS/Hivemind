# Third-Party Notices

Hivemind redistributes the following third-party component inside its container image. Each is
the property of its respective owner and is provided under the license named below.

## Qwen3-Embedding-0.6B (embedding model weights)

- **Source:** https://huggingface.co/Qwen/Qwen3-Embedding-0.6B
- **Copyright:** © Alibaba Cloud / the Qwen Team
- **License:** Apache License 2.0 — https://www.apache.org/licenses/LICENSE-2.0
  (full text included at [`LICENSES/Qwen3-Embedding-0.6B-Apache-2.0.txt`](LICENSES/Qwen3-Embedding-0.6B-Apache-2.0.txt))
- **Modifications:** none. The model is used unmodified; at inference its embeddings are used
  at the native 1024 dimensions and L2-normalized — no model weights are modified.

The weights are baked into the server image at build time (see `Dockerfile`), so the runtime is
hermetically offline (no network model fetch). This redistribution satisfies the Apache-2.0
requirement to provide recipients a copy of the license alongside the Work.
