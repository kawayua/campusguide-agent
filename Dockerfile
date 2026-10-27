# CampusGuide W8 demo image: CPU-only, no data/model/.env baked in.
# Build: docker compose build app
# Run:   docker compose up -d
# In CN networks: --build-arg PYTHON_IMAGE=docker.m.daocloud.io/library/python:3.11-slim

ARG PYTHON_IMAGE=python:3.11-slim
FROM ${PYTHON_IMAGE} AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "uv==0.13.1"

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install the application and W8 runtime deps without torch's CUDA stack.
RUN uv sync --frozen --no-dev --group w2 \
    --no-install-package torch \
    --no-install-package triton \
    --no-install-package cuda-bindings \
    --no-install-package cuda-pathfinder \
    --no-install-package cuda-toolkit \
    --no-install-package nvidia-cublas \
    --no-install-package nvidia-cuda-cupti \
    --no-install-package nvidia-cuda-nvrtc \
    --no-install-package nvidia-cuda-runtime \
    --no-install-package nvidia-cudnn-cu13 \
    --no-install-package nvidia-cufft \
    --no-install-package nvidia-cufile \
    --no-install-package nvidia-curand \
    --no-install-package nvidia-cusolver \
    --no-install-package nvidia-cusparse \
    --no-install-package nvidia-cusparselt-cu13 \
    --no-install-package nvidia-nccl-cu13 \
    --no-install-package nvidia-nvjitlink \
    --no-install-package nvidia-nvshmem-cu13 \
    --no-install-package nvidia-nvtx
RUN uv pip install --python /app/.venv/bin/python --no-cache \
    --index-url https://download.pytorch.org/whl/cpu \
    "torch==2.14.1"

FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PYTHONPATH="/app/src" \
    HF_HOME="/app/hf_cache" \
    GRADIO_SERVER_NAME="0.0.0.0" \
    GRADIO_SERVER_PORT="7860"

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data /app/hf_cache \
    && chown -R app:app /app

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY configs ./configs
COPY scripts ./scripts
COPY README.md ./

USER app
EXPOSE 7860
STOPSIGNAL SIGTERM

CMD ["python", "-m", "campusguide.ui.app"]
