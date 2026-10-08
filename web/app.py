"""Constructicon web app: upload, gallery, and the public /f/{slug} hotlink
route.

#467: users sign in (web/auth.py, core/users.py) and, since step 2, roles are enforced
(core/roles.ENFORCE): every page and API needs at least viewer, except the public doors
(/f hotlinks, /healthz, /login, /setup, static assets). Non-browser clients send the install token.

#547: this file is the assembly point: the app, its exception handler, static
mounts, middleware, startup (background work) and the routers. Routes live in
web/routes/, shared shapers in web/shapes.py, request-scoped helpers and the
templates object in web/common.py, audit logging in web/middleware.py.
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from core import actor as actor_ctx, captions, db, decisions, errors, install_token, ocr, paths
from core import items as item_service  # aliased: web.routes.items (imported below) is a different module
from web import auth as web_auth, content_security, request_guard
from web.common import _STATIC_DIR
from web.middleware import _scrub_secrets, ActorMiddleware, AuditLoggingMiddleware  # noqa: F401 (_scrub_secrets re-exported for scripts/test_request_guard.py)
from web.routes import meta, pages, admin, items, curator, files, cards, hobbies, blog_export, auth as auth_routes

app = FastAPI()


# --- One error shape (#548) ---
# Every error response is {"ok": false, "error": {"code", "message"[, "details"]}, "detail": ...}.
# `detail` stays because the page JS reads it (data.detail); for an AppError it repeats the
# message, for an HTTPException it is the exception's own detail, unchanged.

@app.exception_handler(errors.AppError)
async def _app_error_handler(request: Request, exc: errors.AppError):
    """Any core refusal (CardError, QueueError, decisions.*, InvalidInput, ...) becomes its
    status (CardError: 422, 404 missing card, 409 conflicts/nesting) with the same
    {code, message} the MCP tools return."""
    return JSONResponse(errors.http_body(exc.code, exc.message, exc.details), status_code=exc.http_status)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Plain HTTPExceptions (routes, plus Starlette's own 404/405): same `detail` as before,
    plus ok:false and error{code, message}, the code derived from the status."""
    message = exc.detail if isinstance(exc.detail, str) else json.dumps(exc.detail, default=str)
    return JSONResponse(errors.http_body(errors.code_for_status(exc.status_code), message, detail=exc.detail),
                        status_code=exc.status_code, headers=getattr(exc, "headers", None))


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    """A missing/ill-typed form or query field: FastAPI's 422 `detail` list is kept as it was,
    with ok:false and error{code: "validation_error", message} added."""
    detail = jsonable_encoder(exc.errors())
    parts = []
    for e in detail:
        loc = ".".join(str(x) for x in (e.get("loc") or [])[1:]) or "request"
        parts.append(f"{loc}: {e.get('msg')}")
    return JSONResponse(errors.http_body("validation_error", "; ".join(parts) or "Invalid request", detail=detail),
                        status_code=422)


app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")
# Brand assets (logo, wordmarks, favicons) live at the repo root in
# assets/brand/, independent of web/static/ — see README's "Retained art
# assets" section. Mounted separately rather than copied into web/static so
# there's a single source of truth for them.
_BRAND_DIR = Path(__file__).resolve().parent.parent / "assets" / "brand"
# #581: the image doesn't copy code, so an install whose compose file doesn't mount ./assets has
# no assets/brand. /brand then falls back to the handful of files shipped inside web/static
# (logo, favicons, touch icon) instead of 404ing on every page. The mounted directory wins.
_BRAND_FALLBACK_DIR = _STATIC_DIR / "brand-fallback"


class _BrandFiles(StaticFiles):
    async def get_response(self, path, scope):
        self.all_directories = [d for d in (_BRAND_DIR, _BRAND_FALLBACK_DIR) if d.is_dir()]
        return await super().get_response(path, scope)


app.mount("/brand", _BrandFiles(directory=_BRAND_FALLBACK_DIR), name="brand")

# Preview mount for exported sites — points to the current build directory.
# Ensure the directory exists (even if empty) so the mount doesn't fail at startup.
class _CurrentExportFiles(StaticFiles):
    """#578: serves paths.current_export_dir(), resolved per request (not frozen at import), so a
    test that repoints CONSTRUCTICON_EXPORTS_DIR after importing the app serves its own build."""

    async def get_response(self, path, scope):
        self.all_directories = self.get_directories(paths.current_export_dir(), None)
        response = await super().get_response(path, scope)
        # #610: the generated pages are ours and must render, but media/ holds the owner's uploaded
        # files copied verbatim (an .html or .svg among them): those get the same treatment as /f/.
        if path.replace("\\", "/").lstrip("/").startswith("media/") and getattr(response, "path", None):
            for key, value in content_security.file_headers(response.path, Path(response.path).name).items():
                response.headers[key] = value
        return response


paths.current_export_dir().mkdir(parents=True, exist_ok=True)
app.mount("/preview", _CurrentExportFiles(directory=paths.current_export_dir(), html=True), name="preview")

# #467 step 1: the per-session CSRF check, innermost so its refusals reach the request log.
app.add_middleware(web_auth.CsrfMiddleware)
app.add_middleware(AuditLoggingMiddleware)
# #467 step 2: the install token (actor `token`, admin) and the sign-in gate (anonymous pages ->
# /login or /setup, anonymous API -> 401, mounts gated by web.roles.NON_ROUTE_ROLES). Outside the
# audit logger so a refused anonymous request never has its body read.
app.add_middleware(web_auth.AccessMiddleware, routes=lambda: app.routes)
# #467 step 1: a live session cookie -> the signed-in user and actor "user:<name>" (web/auth.py).
app.add_middleware(web_auth.SessionMiddleware)
# #560: sets the request's actor (owner-ui) around the audit logger and the route.
app.add_middleware(ActorMiddleware)
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
    actor_ctx.spawn(ocr.run_ocr, slug)  # #560: carries the caller's actor (system at boot/watchdog)




async def _ocr_watchdog():
    while True:
        await asyncio.sleep(OCR_WATCHDOG_INTERVAL_SECONDS)
        try:
            # #550: the DB reads/writes run in a worker thread (to_thread copies this task's `system` actor).
            stale = await asyncio.to_thread(db.list_stale_pending_ocr, OCR_STALE_THRESHOLD_SECONDS)
            for row in stale:
                print(f"OCR watchdog: re-firing {row['slug']} — pending for over {OCR_STALE_THRESHOLD_SECONDS}s")
                await asyncio.to_thread(_refire_ocr, row["slug"])
        except Exception as e:
            print(f"OCR watchdog error: {e!r}")


CAPTION_SELFHEAL_INTERVAL_SECONDS = 300


def _heal_captions():
    try:
        n = captions.requeue_orphans()
        if n:
            print(f"caption self-heal: re-queued {n} item(s) left pending with no queue row", flush=True)
    except Exception as e:
        print(f"caption self-heal error: {e!r}", flush=True)


async def _caption_selfheal_loop():
    """#592: the caption twin of the OCR watchdog. asyncio.to_thread copies this task's context,
    so it runs as `system`."""
    while True:
        await asyncio.sleep(CAPTION_SELFHEAL_INTERVAL_SECONDS)
        await asyncio.to_thread(_heal_captions)


TRASH_PURGE_INTERVAL_SECONDS = 3600 # #541: deleted files leave the trash after items.TRASH_DAYS


async def _trash_purge_loop():
    """Web owns background work (#549): the hourly pass that permanently removes trash entries
    past their expiry. asyncio.to_thread copies this task's context, so it runs as `system`."""
    await asyncio.sleep(60)  # let boot settle first
    while True:
        try:
            result = await asyncio.to_thread(item_service.purge_expired)
            if result["purged"]:
                print(f"trash purge: removed {result['purged']} expired item(s), {result['bytes']} bytes", flush=True)
        except Exception as e:
            print(f"trash purge error: {e!r}", flush=True)
        await asyncio.sleep(TRASH_PURGE_INTERVAL_SECONDS)


def _sweep_stale_decisions():
    """#551 item 3: questions that can't be answered any more (their object, card or candidates
    are gone) are resolved by this explicit, logged, undoable sweep, never by a GET. Reads already
    leave them out (decisions.list_open), so the sweep only tidies the table."""
    try:
        result = decisions.sweep_stale()
        if result.data["count"]:
            print(f"decision sweep: resolved {result.data['count']} stale question(s) {result.data['by_kind']} "
                  f"(batch {result.batch_id})", flush=True)
    except Exception as e:
        print(f"decision sweep error: {e!r}", flush=True)


async def _decision_sweep_loop():
    """Web owns background work (#549): the stale-decision sweep every
    decisions.SWEEP_INTERVAL_SECONDS (the startup pass runs in _startup_as_system). Runs as `system`
    (asyncio.to_thread copies this task's context)."""
    while True:
        await asyncio.sleep(decisions.SWEEP_INTERVAL_SECONDS)
        await asyncio.to_thread(_sweep_stale_decisions)


@app.on_event("startup")
async def startup():
    # #560: boot work (migrations, OCR self-heal, the watchdog task created below, which copies
    # this context) is the 'system' actor.
    with actor_ctx.acting_as(actor_ctx.ACTOR_SYSTEM):
        _startup_as_system()


def _startup_as_system():
    # #467 step 2: a broken install-token configuration (unreadable/empty file, too short) stops the
    # app here, loudly, like the MCP. No token at all is fine: sessions work, bearer clients get 401.
    try:
        token_on = install_token.enabled()
    except install_token.TokenConfigError as exc:
        raise RuntimeError(f"constructicon-web: refusing to start: {exc}") from None
    print(f"install token: {'configured (Bearer clients = admin)' if token_on else 'NOT configured (Bearer clients refused)'}",
          flush=True)
    db.init_db()
    # #562: the imagerepo IT-client seed ("Unknown", "Not Business", "Internal Infrastructure")
    # is no longer inserted on every boot: nothing in Constructicon reads those rows (only
    # /api/clients lists them, and no page calls it). sync_clients.py still seeds them.
    # #551 item 3: once at startup, after the migrations; then hourly (_decision_sweep_loop).
    _sweep_stale_decisions()
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
    # #592: every web caption is persisted into that table first, so a restart just resumes it.
    # Anything left pending with no queue row (queued by an older process) is re-queued here and
    # then every CAPTION_SELFHEAL_INTERVAL_SECONDS.
    _heal_captions()
    captions.start_queue_worker()
    asyncio.create_task(_caption_selfheal_loop())
    asyncio.create_task(_trash_purge_loop())
    asyncio.create_task(_decision_sweep_loop())


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
app.include_router(auth_routes.router)  # #467 step 1: login, setup, logout, users, my password
