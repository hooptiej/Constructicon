# Base image pinned by digest (#676) so a rebuild can't pick up a different Python, Debian or pip
# underneath the pinned requirements. This is the python:3.14-slim the 2026-10-12 image was built on
# (Python 3.14.7, built 2026-09-01; it also fixes pip, which the image doesn't upgrade).
# To bump it: `docker pull python:3.14-slim`, then
# `docker image inspect python:3.14-slim --format '{{index .RepoDigests 0}}'`, paste the new digest
# here, rebuild, and run scripts/check_requirements_freeze.py (pip's version shows as a note) and
# scripts/check_dependency_audit.py in the new image before deploying.
FROM python:3.14-slim@sha256:656d12e70054d5fda18a045e2494c96701e9792dd1445f95b3d038df954f57e9

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
# #658: with the model baked in, nothing at run time needs the Hugging Face Hub. Without this, every
# load still asked the Hub for the latest files (144 s of retries with no network; one hung
# connection stalled OCR). core/similarity.py also defaults to offline on its own; this covers any
# other library in the image. Set after the bake above, which is the one step that downloads.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1

# Where the app writes when an install doesn't bind-mount a directory there
# (exports, backups, import, storage, data): owned by the app user, so an
# unmounted path still works. Bind mounts replace these at run time; their host
# directories must be owned by APP_UID (see docker-compose.yml.example).
RUN mkdir -p /app/storage /app/data /app/backups /app/exports /app/import \
    && chown app:app /app/storage /app/data /app/backups /app/exports /app/import

# #688: the app code is baked in, so an image IS a version (prod runs one immutable, versioned
# image; rollback = run the previous tag). Everything above changes only with the Dockerfile or
# requirements.txt, so a code change rebuilds just these last few small layers. What enters the
# build context is decided by .dockerignore (an allowlist: no secrets, data, .git, worktrees,
# backups, tests or scripts/). The code is owned by root and read-only to the app user: a
# compromised parser can't rewrite the app. Precompiled here because the app user can't write
# __pycache__ next to it.
# scripts/ is deliberately NOT in the image (tests, release/deploy tooling; the emergency tools
# move into core/manage/ with #634). Dev/test installs bind-mount the code over these copies with
# a compose override (docker-compose.override.yml.example), so "copy files in, restart" still works.
COPY core/ /app/core/
COPY web/ /app/web/
COPY mcp_server/ /app/mcp_server/
COPY assets/ /app/assets/
RUN chmod -R u=rwX,go=rX /app/core /app/web /app/mcp_server /app/assets \
    && python -m compileall -q /app/core /app/web /app/mcp_server

# #688: the version, stamped at build time (scripts/deploy.sh passes these from `git describe`).
# core/version.py reads /app/core/VERSION.json; with no CONSTRUCTICON_VERSION the file isn't
# written and the app reports "dev". CONSTRUCTICON_ENV=dev adds the "-dev" suffix at run time.
# Declared here, after the code, so a new version never invalidates an earlier layer.
ARG CONSTRUCTICON_VERSION=""
ARG CONSTRUCTICON_COMMIT=""
ARG CONSTRUCTICON_BUILT_AT=""
LABEL org.opencontainers.image.title="Constructicon" \
      org.opencontainers.image.version="$CONSTRUCTICON_VERSION" \
      org.opencontainers.image.revision="$CONSTRUCTICON_COMMIT" \
      org.opencontainers.image.created="$CONSTRUCTICON_BUILT_AT"
RUN if [ -n "$CONSTRUCTICON_VERSION" ]; then \
      python -c 'import json, os; e = os.environ; json.dump({"version": e["CONSTRUCTICON_VERSION"], "commit": e.get("CONSTRUCTICON_COMMIT") or None, "deployed_at": e.get("CONSTRUCTICON_BUILT_AT") or None}, open("/app/core/VERSION.json", "w")); print(open("/app/core/VERSION.json").read())' \
      && chmod 644 /app/core/VERSION.json; \
    fi

USER app

EXPOSE 8000 8100
