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
# runtime is CPU (§1) and the CUDA build is multi-GB; this keeps the image lean and the build
# disk-safe. sentence-transformers then sees torch already satisfied.
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir ".[embed]"
COPY hive/ ./hive/
RUN pip install --no-cache-dir ".[embed]" \
 && python -m compileall -q hive
# Bake the bge-small weights OFFLINE into an image layer (no hot-path download).
# --dest is the HF HUB cache dir ($HF_HOME/hub), which is EXACTLY where the offline runtime
# (HF_HOME=/opt/hf-cache, cache_folder unset) resolves the model — bake and load must agree
# on the same root or the runtime finds nothing under --network none.
ENV HF_HOME=/opt/hf-cache
RUN python -m hive.tools.bake_model --model BAAI/bge-small-en-v1.5 --dest /opt/hf-cache/hub

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
