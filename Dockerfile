FROM python:3.14-slim

# libcairo2: SVG thumbnail rasterization (core/svg.py, issue #30) — cairosvg
# is a pure-Python *package* but dynamically loads a real libcairo.so.2 at
# runtime, confirmed to fail without it. Small (~1.4MB), same category as
# tesseract-ocr below.
# ghostscript: EPS thumbnail rasterization (core/eps.py, issue #30) — EPS is
# PostScript, so an actual PostScript interpreter is unavoidable; confirmed
# installing cleanly (no build-from-source, no exotic packages) at ~47MB,
# mostly URW base-35 fonts pulled in for text layout.
# build-essential: py7zr (archive.py, issue #128) requires building pyppmd
# (a C extension) — needed during pip install, removed after to keep slim.
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libcairo2 ghostscript ffmpeg build-essential git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# This container has no need for CUDA (sentence-transformers' embeddings
# are cheap enough on CPU) even though the box itself has a real GPU (an
# RTX 3060 Ti — used by the separate Ollama container for moondream
# captioning, see core/captions.py; confirmed via nvidia-smi 2026-09-12,
# correcting an earlier "this box has no GPU" note here that predated that
# hardware). Plain PyPI torch ships CUDA libraries by default regardless —
# several GB of nvidia-* packages this container would never use. Installing
# torch's CPU-only build first satisfies that dependency before pip gets a
# chance to reach for the CUDA one.
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# #643: the app runs as a non-root user (it parses every uploaded file with
# native parsers). UID/GID 1000 matches the owner of the data dirs on the
# TrueNAS box (hoop); override with --build-arg APP_UID=... for another host.
# The model cache lives outside /root so that user can read it.
ARG APP_UID=1000
ARG APP_GID=1000
ENV HF_HOME=/opt/hf-cache
RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --home-dir /home/app --shell /usr/sbin/nologin app

# Bake the embedding model weights into the image at build time — the
# alternative is a real (multi-second, network-dependent) download on the
# first similarity check after every fresh container start, which is
# exactly the kind of surprise latency this app has already had enough of.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
    && chmod -R a+rX "$HF_HOME"

# Where the app writes when an install doesn't bind-mount a directory there
# (exports, backups, import, storage, data): owned by the app user, so an
# unmounted path still works. Bind mounts replace these at run time; their host
# directories must be owned by APP_UID (see docker-compose.yml.example).
RUN mkdir -p /app/storage /app/data /app/backups /app/exports /app/import \
    && chown app:app /app/storage /app/data /app/backups /app/exports /app/import

USER app

# core/, web/, and mcp_server/ are bind-mounted at run time, not baked in —
# this image is just the runtime environment (Python + system deps). Code
# deploys are a file copy + container restart, not a rebuild; only changes
# to requirements.txt or system packages need `docker build` again.

EXPOSE 8000 8100
