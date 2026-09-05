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
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr libcairo2 ghostscript ffmpeg build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# This box has no GPU (confirmed: no nvidia-smi, no nvidia PCI device), but
# sentence-transformers pulls in torch, and plain PyPI torch ships CUDA
# libraries by default — several GB of nvidia-* packages this box can never
# use. Installing torch's CPU-only build first satisfies that dependency
# before pip gets a chance to reach for the CUDA one.
RUN pip install --no-cache-dir torch==2.13.0 --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the embedding model weights into the image at build time — the
# alternative is a real (multi-second, network-dependent) download on the
# first similarity check after every fresh container start, which is
# exactly the kind of surprise latency this app has already had enough of.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# core/, web/, and mcp_server/ are bind-mounted at run time, not baked in —
# this image is just the runtime environment (Python + system deps). Code
# deploys are a file copy + container restart, not a rebuild; only changes
# to requirements.txt or system packages need `docker build` again.

EXPOSE 8000 8100
