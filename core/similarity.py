"""Visual and semantic similarity — perceptual hashing (imagehash) for "same
panel, captured twice" and text embeddings (sentence-transformers, a small
local model, no cloud call) for "same kind of error, different client".
Both run alongside OCR, not as a separate pass — a capture-event's
perceptual_hash and embedding are populated the same time extracted_text is.

Comparison is always live — queried against every other row at request
time, never precomputed or cached. At the data volumes this app actually
has (hundreds to low thousands of rows), a linear scan over stored hashes
and vectors is microseconds, and skipping a cache means there's nothing to
invalidate as new images come in.

Local-first on purpose, same call as OCR: screenshot content doesn't leave
the box, and there's no per-request cost or new cloud credential to manage.
"""

import threading

import imagehash
import numpy as np
from PIL import Image, ImageStat
from sentence_transformers import SentenceTransformer

from . import db

EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
PHASH_MATCH_THRESHOLD = 8         # Hamming distance out of 64 bits — lower is more similar
EMBEDDING_MATCH_THRESHOLD = 0.6   # cosine similarity — higher is more similar
MAX_SIMILAR_RESULTS = 12

_model = None
# run_ocr runs under a 2-wide semaphore (core/ocr.py) and the startup
# self-heal requeues every pending row at once, so on a cold start two
# threads routinely race into the lazy init below at the same time -- without
# the lock both would construct a SentenceTransformer (double load time,
# double memory) (#224).
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                _model = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _model


# #587 item 2: a nearly uniform image hashes to a degenerate value (all zeros, or one lone bit such
# as 8000000000000000, the DC term), and every blank-ish image then "matches" every other. Such a
# hash carries no information, so it is treated as "no hash": never stored for a new image, and
# ignored when comparing rows that already hold one.
MIN_HASH_BITS = 3                # a 64-bit hash with fewer (or, mirrored, more) set bits is degenerate
MIN_IMAGE_STDDEV = 3.0           # grey-level standard deviation (0-255) below which an image is "blank"


def is_degenerate_hash(hex_hash):
    """True for a missing/unparseable hash or one with near-zero entropy: at most
    MIN_HASH_BITS - 1 bits set, or at most as many clear (all ones, one lone zero bit)."""
    if not hex_hash:
        return True
    try:
        bits = bin(int(hex_hash, 16)).count("1")
    except ValueError:  # silent-ok: an unparseable hash is "no hash", the documented meaning here
        return True
    return bits < MIN_HASH_BITS or (64 - bits) < MIN_HASH_BITS


def _is_low_variance(img):
    grey = img.convert("L")
    grey.thumbnail((256, 256))
    return ImageStat.Stat(grey).stddev[0] < MIN_IMAGE_STDDEV


def compute_perceptual_hash(path):
    """The image's perceptual hash as hex, or None when the image is too uniform (or the hash too
    degenerate) to say anything about similarity."""
    img = Image.open(path)
    if _is_low_variance(img):
        return None
    h = str(imagehash.phash(img))
    return None if is_degenerate_hash(h) else h


def compute_embedding(text):
    """Raw float32 bytes, or None if there's no meaningful text to embed."""
    if not text or not text.strip():
        return None
    vector = _get_model().encode(text, normalize_embeddings=True)
    return np.asarray(vector, dtype=np.float32).tobytes()


def _phash_distance(a, b):
    return imagehash.hex_to_hash(a) - imagehash.hex_to_hash(b)


def _cosine_similarity(a_bytes, b_bytes):
    a = np.frombuffer(a_bytes, dtype=np.float32)
    b = np.frombuffer(b_bytes, dtype=np.float32)
    denom = np.linalg.norm(a) * np.linalg.norm(b)
    if denom == 0:
        return 0.0
    return float(np.dot(a, b) / denom)


def find_similar(slug):
    """Auto-detected similar capture-events for `slug`: visual match (same
    panel, re-saved under a different name), text match (same kind of
    issue, worded differently), or both. Excludes the row itself and
    anything already manually related — that's a curated relation, showing
    it again as a "suggestion" is just noise. Ranked best first.
    Returns [{"slug": ..., "reason": "visual"|"text"|"both", "score": 0-1}]
    """
    row = db.get_by_slug(slug)
    if row is None:
        return []
    already_related = {r["slug"] for r in db.list_related(slug)}
    candidates = db.list_hash_and_embedding_candidates(exclude_slug=slug)

    matches = {}
    for c in candidates:
        if c["slug"] in already_related:
            continue
        visual_score = None
        if not is_degenerate_hash(row["perceptual_hash"]) and not is_degenerate_hash(c["perceptual_hash"]):
            dist = _phash_distance(row["perceptual_hash"], c["perceptual_hash"])
            if dist <= PHASH_MATCH_THRESHOLD:
                visual_score = 1 - (dist / 64)
        text_score = None
        if row["embedding"] and c["embedding"]:
            sim = _cosine_similarity(row["embedding"], c["embedding"])
            if sim >= EMBEDDING_MATCH_THRESHOLD:
                text_score = sim
        if visual_score is None and text_score is None:
            continue
        if visual_score is not None and text_score is not None:
            reason, score = "both", max(visual_score, text_score)
        elif visual_score is not None:
            reason, score = "visual", visual_score
        else:
            reason, score = "text", text_score
        matches[c["slug"]] = {"slug": c["slug"], "reason": reason, "score": score}

    return sorted(matches.values(), key=lambda m: -m["score"])[:MAX_SIMILAR_RESULTS]
