# syntax=docker/dockerfile:1.7
# ---------- builder ----------
FROM python:3.12-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN apt-get update && apt-get install -y --no-install-recommends git build-essential \
 && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# CPU-ONLY torch, pinned BEFORE sentence-transformers pulls its default (CUDA) wheel — the
# runtime is CPU and the CUDA build is multi-GB; this keeps the image lean and the build
# disk-safe. sentence-transformers then sees torch already satisfied.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir ".[embed]"
# Bake the Qwen3-Embedding-0.6B weights OFFLINE into an image layer (no hot-path download) BEFORE
# the volatile `COPY hive/`, so a hive/ source edit never invalidates — and re-downloads — this
# ~1.2 GB weights layer (fp32; budget ~2.5–3 GB resident RAM on CPU at runtime). bake_model.py is
# self-contained (stdlib + a lazy sentence_transformers import; both package __init__.py are empty),
# so the model layer depends on ONLY these three copied files and rebuilds only when bake_model.py
# itself changes. --dest is the HF HUB cache dir ($HF_HOME/hub), EXACTLY where the offline runtime
# (HF_HOME=/opt/hf-cache, cache_folder unset) resolves the model — bake and load must agree on the
# same root or the runtime finds nothing under --network none.
ENV HF_HOME=/opt/hf-cache
COPY hive/__init__.py ./hive/__init__.py
COPY hive/tools/__init__.py ./hive/tools/__init__.py
COPY hive/tools/bake_model.py ./hive/tools/bake_model.py
RUN python -m hive.tools.bake_model --model Qwen/Qwen3-Embedding-0.6B --dest /opt/hf-cache/hub
# Volatile source — copied AFTER the model layer so editing it never re-bakes the weights.
COPY hive/ ./hive/
RUN pip install --no-cache-dir ".[embed]" \
 && python -m compileall -q hive

# ---------- runtime ----------
FROM python:3.12-slim AS runtime
# Hard offline: a runtime model fetch is impossible, not merely discouraged.
ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/opt/hf-cache \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN groupadd --system hive && useradd --system --gid hive --home /home/hive --create-home hive
COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/hf-cache /opt/hf-cache
COPY --from=builder /build/hive /opt/venv/lib/python3.12/site-packages/hive
RUN mkdir -p /data && chown -R hive:hive /data /opt/hf-cache
VOLUME ["/data"]
# Healthy IFF the embedder is resident (not merely importable).
HEALTHCHECK --interval=15s --timeout=10s --start-period=120s --retries=10 \
  CMD ["python", "-m", "hive.tools.healthcheck"]
USER hive
ENTRYPOINT ["python", "-m", "hive.tools.entrypoint"]
