"""Constructicon web app: upload, gallery, and the public /f/{slug} hotlink
route.

No auth — this runs on a LAN-only dev server with no port forward, so the
network perimeter is the security boundary, not a login gate.

#547: this file is the assembly point: the app, its exception handler, static
mounts, middleware, startup (background work) and the routers. Routes live in
web/routes/, shared shapers in web/shapes.py, request-scoped helpers and the
templates object in web/common.py, audit logging in web/middleware.py.
"""

import asyncio
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from core import captions, card_rules, db, ocr
from web import request_guard
from web.common import _STATIC_DIR
from web.middleware import _scrub_secrets, AuditLoggingMiddleware  # noqa: F401 (_scrub_secrets re-exported for scripts/test_request_guard.py)
from web.routes import meta, pages, admin, items, curator, files, cards, hobbies, blog_export

app = FastAPI()


@app.exception_handler(card_rules.CardError)
async def _card_error_handler(request: Request, exc: card_rules.CardError):
    """V2 cards (spec section 5): a rule violation from core/ becomes HTTP 422
    (404 missing card, 409 conflicts/nesting) with the same {code, message} the
    MCP tools return. `detail` repeats the message so existing UI error toasts
    (which read data.detail) keep working."""
    return JSONResponse(
        {"ok": False, "error": exc.to_dict(), "detail": exc.message},
        status_code=exc.http_status,
    )


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
# Brand assets (logo, wordmarks, favicons) live at the repo root in
# assets/brand/, independent of web/static/ — see README's "Retained art
# assets" section. Mounted separately rather than copied into web/static so
# there's a single source of truth for them.
_BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"
if _BRAND_DIR.is_dir():
    app.mount("/brand", StaticFiles(directory=_BRAND_DIR), name="brand")

# Preview mount for exported sites — points to the current build directory.
# Ensure the directory exists (even if empty) so the mount doesn't fail at startup.
_PREVIEW_DIR = Path(__file__).resolve().parent.parent / "exports" / "current"
_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/preview", StaticFiles(directory=_PREVIEW_DIR, html=True), name="preview")

app.add_middleware(AuditLoggingMiddleware)
# #558: outermost, so a forged cross-origin request is refused before anything runs.
app.add_middleware(request_guard.OriginGuardMiddleware)


OCR_WATCHDOG_INTERVAL_SECONDS = 60
OCR_STALE_THRESHOLD_SECONDS = 600  # 10 min — well past normal queueing even under a big batch


def _refire_ocr(slug):
    # set_ocr_status(..., "pending") stamps a fresh ocr_started_at, so this
    # attempt gets its own staleness clock — without that, a row re-fired
    # here would still look exactly as stale to the watchdog on its very
    # next tick, and get fired again every interval instead of once.
    db.set_ocr_status(slug, "pending")
    threading.Thread(target=ocr.run_ocr, args=(slug,), daemon=True).start()




async def _ocr_watchdog():
    while True:
        await asyncio.sleep(OCR_WATCHDOG_INTERVAL_SECONDS)
        try:
            stale = db.list_stale_pending_ocr(OCR_STALE_THRESHOLD_SECONDS)
            for row in stale:
                print(f"OCR watchdog: re-firing {row['slug']} — pending for over {OCR_STALE_THRESHOLD_SECONDS}s")
                _refire_ocr(row["slug"])
        except Exception as e:
            print(f"OCR watchdog error: {e!r}")


@app.on_event("startup")
async def startup():
    db.init_db()
    db.ensure_special_clients()
    # Self-heal: a redeploy/restart while OCR was still queued or running for
    # a row leaves it stuck at ocr_status="pending" forever otherwise, since
    # nothing else will ever retry it. Fired as background threads, not run
    # here directly — this must not block the app from starting up, which is
    # exactly what happened before this fix when several rows were stuck at
    # once (each blocking, one after another, on the way to accepting any
    # requests at all).
    stuck = db.list_pending_ocr()
    if stuck:
        print(f"re-running OCR for {len(stuck)} row(s) left pending by a prior process", flush=True)
        for row in stuck:
            _refire_ocr(row["slug"])
    asyncio.create_task(_ocr_watchdog())
    # #549: web is the ONLY process that captions: it drains the caption_queue table (filled by
    # the MCP process), one at a time, through captions.run_caption (lock + breaker + cooldown).
    captions.start_queue_worker()


# --- Routers (#547) ---
# Order matters where two routes could match the same URL: the first one registered wins.
# Today every such pair lives inside one router, in its original order. A new route that
# could collide with one in another router needs this order (or its placement) checked;
# scripts/golden_master.py compares resolution before/after mechanically.
app.include_router(meta.router)
app.include_router(pages.router)
app.include_router(admin.router)
app.include_router(items.router)
app.include_router(curator.router)
app.include_router(files.router)
app.include_router(cards.router)
app.include_router(hobbies.router)
app.include_router(blog_export.router)
