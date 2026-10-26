# ══════════════════════════════════════════════════════════════════════════════
#  NL2SQL — Dockerfile (GPU)
#  Utilisé par : model_service (SQLCoder 7B) + rewrite_service (Qwen2.5 3B)
#
#  RTX 5070 Ti Laptop = sm_120 (Blackwell)
#  PyTorch nightly cu128 — seule version supportant sm_120
#  bitsandbytes SUPPRIMÉ — incompatible cu128
#  Chargement en float16 — ~7 Go VRAM sur 11.6 disponibles
# ══════════════════════════════════════════════════════════════════════════════

FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu22.04 AS base

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-dev python3-pip \
        curl git libgomp1 \
    && ln -sf /usr/bin/python3.11 /usr/bin/python3 \
    && ln -sf /usr/bin/python3    /usr/bin/python \
    && rm -rf /var/lib/apt/lists/*

# ── PyTorch nightly cu128 ─────────────────────────────────────────────────────
FROM base AS deps

WORKDIR /deps

RUN pip install --upgrade pip && \
    pip install --pre \
        torch torchvision torchaudio \
        --index-url https://download.pytorch.org/whl/nightly/cu128

# ── Stack ML (sans bitsandbytes) ──────────────────────────────────────────────
RUN pip install \
        transformers==4.44.2 \
        accelerate==0.34.2 \
        sentencepiece==0.2.0 \
        einops==0.8.0 \
        flask==3.0.3 \
        requests==2.32.3 \
        python-dotenv==1.0.1

# ── Image finale ───────────────────────────────────────────────────────────────
FROM base AS final

COPY --from=deps /usr/local/lib/python3.11/dist-packages \
                 /usr/local/lib/python3.11/dist-packages
COPY --from=deps /usr/local/bin /usr/local/bin

WORKDIR /app

COPY model_service/   ./model_service/
COPY rewrite_service/ ./rewrite_service/

ENV PYTHONPATH=/app \
    OMP_NUM_THREADS=16 \
    MKL_NUM_THREADS=16 \
    NUMEXPR_NUM_THREADS=16 \
    CUDA_VISIBLE_DEVICES=0 \
    TORCH_CUDA_ARCH_LIST="12.0" \
    PYTORCH_CUDA_ALLOC_CONF="max_split_size_mb:256,expandable_segments:True" \
    TRANSFORMERS_OFFLINE=1 \
    HF_DATASETS_OFFLINE=1 \
    TOKENIZERS_PARALLELISM=false

EXPOSE 8001 8003

CMD ["python3", "-c", "print('Specify command via docker-compose')"]
