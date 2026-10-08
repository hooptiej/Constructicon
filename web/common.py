"""Request-scoped helpers shared by the routers (#547): the Jinja templates object and its
globals, and the breadcrumb / revision-note helpers.
Moved verbatim from web/app.py."""

import logging
import os
from pathlib import Path
from urllib.parse import unquote, quote

import anyio.from_thread
from fastapi.templating import Jinja2Templates

from core import besteffort, db, install_config, item_title, markdown_render, object_types, storage, users
from core import version as version_info
from web import auth as web_auth

log = logging.getLogger("constructicon.web")


_STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")


def static_version(relative_path):
    """#258: a browser that already cached /static/style.css (or any JS
    under /static/js/) has no reason to revalidate it after a deploy --
    confirmed 2026-09-10, the server was serving updated CSS but a browser
    kept rendering the old layout until a hard refresh. Appending this as
    a ?v= query string busts the cache exactly when the file's own content
    actually changes (mtime-based, not a single value for every asset on
    every deploy), with no build step or hashed-filename renaming needed.
    Falls back to "0" if the file's missing so a template render never
    hard-fails over a cache-buster.
    """
    try:
        return str(int((_STATIC_DIR / relative_path).stat().st_mtime))
    except OSError as e:
        besteffort.warn(log, "static_version: a template references a static file that can't be read", e,
                        path=relative_path)
        return "0"


templates.env.globals["static_version"] = static_version
# #471: safe Markdown -> HTML for authored bodies (write-ups); raw HTML off.
templates.env.filters["markdown"] = markdown_render.render
templates.env.filters["item_title"] = item_title.title_of  # #542: the one canonical item title

# #310: environment awareness for the browser tab title. constructicon-test
# sets CONSTRUCTICON_ENV=dev in its compose so its tab reads "DEV-..." and is
# distinguishable from production (which defaults to prod → no prefix). Mirrors
# quest-log's QUEST_LOG_ENV pattern. Read once at startup; injected as a Jinja
# global so base.html can prefix the title without threading it through routes.
_IS_DEV = os.getenv("CONSTRUCTICON_ENV", "prod").strip().lower() == "dev"
templates.env.globals["is_dev"] = _IS_DEV

# #508: build version, shown subtly on every page via base.html. A callable
# global so each render re-reads core/VERSION.json (written by deploy.sh).
templates.env.globals["app_version"] = version_info.get_version

# #581: the install's own name and logo (install config, cached per process). Callables so each
# render sees a saved change; "Constructicon" and the bundled logo when unset. The tab title keeps
# its DEV- prefix: base.html puts is_dev in front of whatever name the page's title block makes.
templates.env.globals["app_name"] = install_config.app_name
templates.env.globals["app_logo_url"] = install_config.app_logo_url

# #467 step 1: who is signed in (the header's user chip) and the session's CSRF token (base.html
# injects it only for a signed-in page, so an anonymous page's HTML is unchanged). Both read the
# request's context, set by web/auth.py's SessionMiddleware.
templates.env.globals["current_user"] = users.current_user
templates.env.globals["csrf_token"] = web_auth.csrf_token

# #431: derive the file upload accept list from the object_types registry
# rather than hardcoding it in templates. This ensures web/templates/_upload_drawer.html
# and web/templates/_gallery_drawer.html stay in sync with newly added types.
templates.env.globals["upload_accept"] = ",".join(object_types.accepted_extensions())
templates.env.globals["upload_max_mb"] = storage.MAX_MB

def _rev_note(request, count, show_all):
    """The small "N older revisions hidden / Show older revisions" line under a grid (templates/
    _revisions_note.html). None when the grid has no superseded items. Toggled with ?rev=all."""
    if not count:
        return None
    params = {k: v for k, v in request.query_params.items() if k != "rev"}
    if not show_all:
        params["rev"] = "all"
    from urllib.parse import urlencode
    return {"count": count, "all": show_all, "href": request.url.path + ("?" + urlencode(params) if params else "")}


def _build_breadcrumbs(from_param, current_item_name):
    """Build a breadcrumb trail for the object detail page based on the `from`
    query parameter. Returns a list of dicts with "label" and "href" keys.
    The last item (current_item_name) has no href since it's not a link.

    #137: breadcrumb navigation on object detail page.
    """
    breadcrumbs = []

    if not from_param:
        # No from param — default to just Home
        breadcrumbs.append({"label": "Home", "href": "/"})
    elif from_param == "unfiled":
        breadcrumbs.append({"label": "Home", "href": "/"})
        breadcrumbs.append({"label": "Unfiled", "href": "/unfiled"})
    elif from_param.startswith("project:"):
        # Extract project slug and look up the project
        project_slug = from_param[8:]  # Remove "project:" prefix
        project = db.get_project(project_slug)
        if project:
            breadcrumbs.append({"label": "Home", "href": "/"})
            # No dedicated projects index route, so Projects links to home
            breadcrumbs.append({"label": "Projects", "href": "/"})
            breadcrumbs.append({"label": project["title"], "href": f"/project/{project_slug}"})
        else:
            # Project doesn't exist or was deleted — fall back to Home only
            breadcrumbs.append({"label": "Home", "href": "/"})
    elif from_param.startswith("user:"):
        # Extract and URL-decode the uploader name
        uploader = unquote(from_param[5:])  # Remove "user:" prefix
        breadcrumbs.append({"label": "Home", "href": "/"})
        breadcrumbs.append({"label": f"{uploader}'s uploads", "href": f"/gallery/user/{quote(uploader)}"})
    else:
        # Unrecognized from param — default to Home
        breadcrumbs.append({"label": "Home", "href": "/"})

    # Add the current item as a non-linked breadcrumb
    breadcrumbs.append({"label": current_item_name, "href": None})

    return breadcrumbs


def from_request_thread(async_fn, *args):
    """#550: read the request body from inside a plain `def` route (FastAPI runs those in a worker
    thread, so the blocking work stays off the event loop). `request.form()` / `request.json()` /
    `request.body()` are coroutines; this hops back to the event loop to run one and returns its
    result: `form = from_request_thread(request.form)`. The route's context (the actor) is the
    worker thread's own copy, so nothing about who is acting changes. Only call it from a sync
    route or a function a sync route called: from async code it raises, use `await` there."""
    return anyio.from_thread.run(async_fn, *args)
