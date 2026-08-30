"""Tesseract OCR — runs once a capture-event's file is already saved and
tagged, filling in extracted_text so search picks it up. Best-effort: a
failure here never fails the upload itself, it just leaves extracted_text
empty and ocr_status set to "failed" for that row.

ocr_status lets the frontend poll a single row and know when OCR is actually
done, rather than assuming it finished the moment the upload response came
back — OCR runs after that response, not before it.

Two things keep a burst of uploads from choking the box: OCR_SEMAPHORE caps
how many tesseract processes run at once (each is real CPU work — with no
cap, a batch upload can spawn one tesseract subprocess per image
simultaneously, and on a 4-core box that means 10+ processes fighting over
4 cores, so what should take seconds each takes many minutes for all of
them — this happened for real on 2026-08-27, confirmed via /proc since this
image has no ps/top). OCR_TIMEOUT_SECONDS bounds any single attempt so a
pathological image can't hold a slot forever; a timeout counts as failed,
same as any other OCR error.

Rows can still end up stuck at ocr_status="pending" if the background task
itself never got scheduled (e.g. a restart mid-upload) rather than just
running slowly — see the startup self-heal sweep in app.py/mcp_server, and
the periodic watchdog in app.py that re-fires anything pending well past
what even a fully-queued, timed-out attempt should ever take.

Also auto-tags by matching the extracted text against known client names,
nicknames, and domains (all synced from Hudu — the company's website plus
any Cloudflare-managed zones) — fully automatic, no manual tag-mapping
maintenance. Matching is deliberately permissive (case-insensitive) — false
positives are cheap to remove via the existing tag UI, so it's tuned to
catch real matches over avoiding noise. On a single unambiguous match, also
auto-selects the client field (only if it's currently empty — never
overrides a manual choice); with more than one match, all are still
tagged, but none is auto-selected since picking one would just be a guess.

Perceptual hash and text embedding are computed here too, right alongside
OCR, inside the same OCR_SEMAPHORE slot — both are real CPU work (hashing
decodes and downsamples the image, embedding runs a small transformer
forward pass), and the whole reason the semaphore exists is to bound how
much CPU work a burst of uploads can trigger at once. Adding a second and
third CPU-bound step without folding them into that same cap would just
reopen the exact contention bug the semaphore was built to close. Like OCR
itself, both are best-effort: a failure here never fails OCR overall, it
just leaves that row without a similarity signal.
"""

import re
import threading
from pathlib import Path

import pytesseract
from PIL import Image

from . import db, similarity, storage

MIN_NICKNAME_LEN = 3
OCR_TIMEOUT_SECONDS = 20  # a real screenshot should OCR in a few seconds; past 20s it's not worth the wait
MAX_CONCURRENT_OCR = 2  # leave headroom on a 4-core box so the app itself stays responsive
OCR_SEMAPHORE = threading.Semaphore(MAX_CONCURRENT_OCR)


def _match_client_tags(text):
    if not text:
        return []
    matched = []
    for name, nickname in db.list_client_aliases():
        if re.search(r"\b" + re.escape(name) + r"\b", text, re.IGNORECASE):
            matched.append(name)
            continue
        if nickname and len(nickname) >= MIN_NICKNAME_LEN and re.search(r"\b" + re.escape(nickname) + r"\b", text, re.IGNORECASE):
            matched.append(name)
    for name, domain in db.list_client_domains():
        if name not in matched and re.search(r"\b" + re.escape(domain) + r"\b", text, re.IGNORECASE):
            matched.append(name)
    return matched


def run_ocr(slug):
    row = db.get_by_slug(slug)
    if row is None or row["redacted"]:
        return
    ext = Path(row["filename"]).suffix.lower()
    if ext not in storage.IMAGE_EXTENSIONS:
        return  # not an image — ocr_status was never set to "pending" for this row
    path = storage.path_for(row["stored_filename"])
    if not path.exists():
        db.set_ocr_status(slug, "failed")
        return
    with OCR_SEMAPHORE:
        try:
            text = pytesseract.image_to_string(Image.open(path), timeout=OCR_TIMEOUT_SECONDS)
        except Exception as e:
            # Best-effort — OCR quality issues, a corrupt image, or a timeout
            # shouldn't ever surface as an upload failure, but print so a
            # systematic failure (e.g. tesseract missing) is traceable.
            print(f"OCR failed for {slug}: {e!r}")
            db.set_ocr_status(slug, "failed")
            return
        try:
            db.set_perceptual_hash(slug, similarity.compute_perceptual_hash(path))
        except Exception as e:
            print(f"Perceptual hash failed for {slug}: {e!r}")
        try:
            embedding = similarity.compute_embedding(text)
            if embedding is not None:
                db.set_embedding(slug, embedding)
        except Exception as e:
            print(f"Embedding failed for {slug}: {e!r}")
    text = text.strip()
    db.set_extracted_text(slug, text)
    matched_clients = _match_client_tags(text)
    if matched_clients:
        db.add_tags(slug, matched_clients)
        if len(matched_clients) == 1:
            # Only auto-select the client field on an unambiguous match — with
            # more than one, tagging both is useful, but picking one to assign
            # would just be a guess. Never overrides a client set by hand.
            db.set_client_if_empty(slug, matched_clients[0])
    db.set_ocr_status(slug, "done")
