"""HTML page routes (#547): home, project / object / hobby pages, unfiled, user gallery,
brand, wallpaper, account, admin, curator, captions review, plus the legacy redirects."""

from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse

from pathlib import Path

from core import card_rules, cards, curation_queue, curator, db, install_config, markdown_render, object_types, revisions, timeline
from core import hobbies, physical_piece, provenance_options
from core.db import PROJECT_STATUSES, BRAND_ROLES
from web.common import _build_breadcrumbs, _rev_note, templates
from web.shapes import _card_items, _datetime_local_value, _friendly_date, _friendly_datetime, _has_thumbnail, _project_cover_url, _project_effective_cover_url, _should_advertise_thumb, _split_revisions, _to_card_face, _to_content_public, _to_object_detail, _to_public, _to_timeline_project
from core import policy, roles, users
from web.roles import RoleRouter, requires

router = RoleRouter(default_role=roles.VIEWER)  # #557: routes without their own label are viewer


# --- Pages ---

@router.get("/", response_class=HTMLResponse)
def home_page(request: Request, hobby: str = "", ref: str = "", rev: str = ""):
    """Home is the gallery itself (left third) plus a curated Projects
    section (right two-thirds) — see README's Projects/tag-tree note for why
    projects and blog_tags are separate concepts. The pill row filters by
    hobby (?hobby=<slug>) or by reference status (?ref=1); the gallery pane
    (client-side, /api/gallery) is unaffected by it.

    #370: Pills are now hobby-based, not project-derived. Selecting a hobby
    pill (?hobby=<slug>) shows only projects attached to that hobby;
    selecting the Reference pill (?ref=1) shows only reference-status projects;
    no params shows all top-level projects.
    """
    all_projects = db.list_projects()
    # #370: Hobby pills replace project-tag pills. Build the pill row from
    # hobbies, with an "All" pill (no filter) and a "Reference" pill at the end.
    hobby_pills = db.list_hobbies()

    # Resolve selected hobby and determine filtered projects
    selected_hobby = None
    selected_hobby_slug = None
    projects = [p for p in all_projects if p.get("parent_id") is None]  # #149: top-level only

    if hobby:
        selected_hobby = db.get_hobby(hobby)
        if selected_hobby:
            selected_hobby_slug = selected_hobby["slug"]
            # Show only projects in this hobby
            hobby_projects = db.list_projects_for_hobby(selected_hobby["id"])
            hobby_project_ids = {p["id"] for p in hobby_projects}
            projects = [p for p in projects if p["id"] in hobby_project_ids]
    elif ref:
        # Show only reference projects: V2 collections (v1's reference-only cards
        # migrated to kind=collection, stamped provenance='referenced' by piece 5),
        # plus any card the owner marks provenance='referenced'.
        projects = [p for p in projects
                    if p.get("provenance") == "referenced"
                    or (p.get("kind") == "collection" and not p.get("provenance"))]
    # #370 follow-up: Compute loose reference objects (provenance='reference',
    # not in any project) for the Reference pill view. Shape for cards:
    # {slug, title, thumb_url}
    reference_objects = []
    if ref:
        loose_ref_rows = policy.filter_visible(db.list_loose_reference_objects())  # #557
        for row in loose_ref_rows:
            media_type = row.get("media_type") or "image"
            spec = object_types.get_object_type(media_type)
            has_thumb = _should_advertise_thumb(row, spec)
            title = row.get("display_name") or row.get("content_description") or row["filename"] or row["slug"]
            # Asset card (8.1, #596): display name, the file's effective date, thumbnail, type
            # line = media type label; loose, so no "Stacked" box; no provenance on the face.
            reference_objects.append({
                "slug": row["slug"],
                "title": title,
                "thumb_url": f"/f/{row['slug']}/thumb" if has_thumb else None,
                "card": cards.file_face(
                    slug=None, title=title, href=f"/object/{row['slug']}",
                    dates=cards.date_range_label(timeline.resolve_item_date(row), None),
                    cover_url=f"/f/{row['slug']}/thumb" if has_thumb else None,
                    type_line=spec.label if spec else media_type),
            })
    # Owner name/initials for the combined gallery+upload pop-out's tab (#17): the install's
    # owner name (#562, core/install_config.py; "Owner" until one is set), initials derived from
    # it ("Hooptie J" -> "HJ").
    _owner_label = install_config.display_owner_name()
    _owner_initials = install_config.owner_initials()
    # #256: Unfiled and Files used to be two separate right-column widgets
    # (#41 and #107) — merged into one "Files" widget covering every upload,
    # type-tabbed, with unfiled items marked inline (see the lamp next to
    # each card's filename in home.html) rather than isolated in their own
    # section. unfiled_slugs is still sourced from db.list_unfiled_items
    # (project-membership based — NOT the same thing as the free-text
    # `client` field, which several past widget versions conflated with
    # "no project"), just reduced to slugs since that's all the merged
    # card's per-item marking needs.
    show_all_revs = rev == "all"  # #477: Files panel lists current revisions only unless ?rev=all
    unfiled_slugs = [r["slug"] for r in db.list_unfiled_items(include_superseded=True)]
    # #107/#256: every uploaded item, independent per media_type (each type
    # contributes its own full list rather than competing within one global
    # pool), so every type that has uploads gets a tab and "all files" really
    # means all of them — same unbounded-limit precedent db.list_unfiled_items
    # already set for the old Unfiled widget, not a new perf tradeoff.
    files_by_type = {}
    older_revs = 0
    for mt, rows in db.list_recent_items_by_type(limit_per_type=10000, include_superseded=True).items():
        rows, n_sup = _split_revisions(policy.filter_visible(rows), show_all_revs)  # #557 (+ the browse clause in db)
        older_revs += n_sup
        if rows:
            files_by_type[mt] = _card_items(rows)
    # Timeline feature: the gallery rail shows every project (including
    # children, with an is_child flag) in date order -- deliberately built
    # from all_projects, not the top-level-only `projects` local above that
    # #149 scoped to the grid.
    timeline_projects = [_to_timeline_project(p) for p in all_projects]
    # V2 cards (8.2): every card once. Unfiltered, a family/collection member is
    # represented by its family's card (members N) instead of repeating as a tile; a
    # hobby- or reference-filtered view shows members individually. The featured card is
    # the highlighted one, then the one with the most recent real end date.
    cards_all = [_to_card_face(p) for p in projects]
    if not hobby and not ref:
        shown_ids = {c["card_id"] for c in cards_all}
        cards_all = [c for c in cards_all if not (set(c["family_ids"]) & shown_ids)]
    active_cards = [c for c in cards_all if c["activity"] == "active"]
    inactive_cards = [c for c in cards_all if c["activity"] != "active"]
    featured_card = None
    if active_cards:
        featured_card = max(active_cards, key=lambda c: (c["highlight"], c["effective_end"] or 0, bool(c["cover_url"])))
        active_cards = [c for c in active_cards if c is not featured_card]
    active_cards.sort(key=lambda c: c["effective_start"] or c["created_at"], reverse=True)
    inactive_cards.sort(key=lambda c: c["effective_start"] or c["created_at"], reverse=True)
    return templates.TemplateResponse(
        request, "home.html",
        {
            "active": "home",
            "top_tags": hobby_pills,  # #370: hobby pills, kept as top_tags for template reuse
            "selected_tag_slug": selected_hobby_slug,  # #370: selected hobby slug for pill highlighting
            "show_reference_pill": True,  # #370: always show Reference pill
            "show_reference_selected": bool(ref),  # #370: highlight if ?ref=1
            "featured_card": featured_card,
            "active_cards": active_cards,
            "inactive_cards": inactive_cards,
            "has_cards": bool(cards_all),
            "reference_objects": reference_objects,  # #370 follow-up: loose reference objects
            "owner_name": _owner_label,
            "owner_initials": _owner_initials,
            "unfiled_slugs": unfiled_slugs,
            "files_by_type": files_by_type,
            "rev_note": _rev_note(request, older_revs, show_all_revs),
            "timeline_projects": timeline_projects,
        },
    )


@router.get("/upload")
def upload_page_redirect():
    # Upload is now a pane on the home page, not its own screen.
    return RedirectResponse("/", status_code=308)


@router.get("/gallery", response_class=HTMLResponse)
def gallery_page_redirect(request: Request):
    # The gallery is now the home page itself — kept as an alias so old
    # links/bookmarks still land somewhere sensible.
    return RedirectResponse("/", status_code=308)


def _card_queue_strip(slug=None, hobby=None):
    """#515/#519: this card's slice of the Curator queue, for the project page's "Needs your
    input" strip. The same items the Curator drawer shows for the card (questions, nudges,
    needs), open ones first and then the deferred ones, each carrying the suggested key(s)
    for the Accept button (no suggestion = no Accept, only Choose)."""
    q = curation_queue.build_queue(card=slug, hobby=hobby)
    items = [i for g in q["groups"] for i in g["items"]] + [i for g in q["deferred"] for i in g["items"]]
    return {"items": items, "counts": q["counts"]}


@router.get("/project/{slug}", response_class=HTMLResponse)
def project_detail_page(request: Request, slug: str, rev: str = ""):
    project = db.get_project(slug)
    if project is None:
        raise HTTPException(status_code=404, detail="not found")
    # #557: a cover the actor may not see isn't named on the page either (today every cover is visible).
    cover_row = db.get_by_slug(project["cover_slug"]) if project.get("cover_slug") else None
    if cover_row is not None and not policy.can_view(cover_row):
        project = {**project, "cover_slug": None}
    # raw_items feeds both the card grid (via _to_content_public below) and
    # the Timeline feature's span resolution (core/timeline.py), which needs
    # the raw capture_events fields _to_content_public's card shape drops.
    # #477: only the current revision of a chain is a card/stack member unless ?rev=all.
    # #557: what this card shows is the item policy's call (today: every item, restricted too).
    raw_items, n_sup = _split_revisions(policy.filter_visible(db.list_project_items(project["id"])), rev == "all")
    items = [_to_content_public(r, project_slug=slug) for r in raw_items]
    child_projects = db.list_child_projects(project["id"])
    ancestors = db.list_project_ancestors(project["id"])
    # #156: fetch the writeup document and pass its body to the template
    writeup_body = None
    if project.get("writeup_slug"):
        writeup_doc = db.get_by_slug(project["writeup_slug"])
        if writeup_doc:
            writeup_body = object_types.writeup_body(writeup_doc) or ""
    effective_start, effective_end = timeline.resolve_project_span(project, raw_items)
    # Timeline feature: per-item effective dates for this project's own
    # content rail -- single points (no endDate), unlike the gallery
    # rail's project spans. Built from raw_items, not `items`, since
    # _to_content_public's card shape drops the raw date columns. The
    # write-up itself is excluded -- same reasoning as
    # core.timeline.resolve_project_span: it's not a chronological event,
    # it's documentation of the project, generated whenever someone got
    # around to writing it up. Still a normal browsable item in the grid
    # above, just not a timeline event.
    timeline_items = [
        {
            "slug": r["slug"],
            "thumb_url": f"/f/{r['slug']}/thumb" if _has_thumbnail(r) and not r.get("redacted") else None,
            "title": r.get("content_description") or r.get("description") or r.get("filename") or r["slug"],
            "effective_date": timeline.resolve_item_date(r),
        }
        for r in raw_items
        if r["slug"] != project.get("writeup_slug")
    ]
    # Horizontal in-page timeline (distinct from the gallery's vertical
    # rail): sub-projects render as blocks (they're spans), this project's
    # own items render as point events -- each child needs its own
    # resolved span, same as the gallery rail's per-project computation.
    timeline_children = []
    # #300: the card grid mixes this project's own items and its
    # sub-projects into ONE chronological sequence (no separate
    # "Sub-projects" section). Each entry carries a `kind` the template
    # branches on and a `sort_date`: an item's resolved effective date, a
    # sub-project's effective_start (the natural "when did this begin"
    # moment for a span). One stable sort over the merged list keeps
    # list_project_items' own sort_order tie-break for same-instant items.
    grid_entries = []
    older_slugs = db.superseded_slugs()  # #477: marks older revisions when ?rev=all shows them
    for raw, public in zip(raw_items, items):
        effective_date = timeline.resolve_item_date(raw)
        grid_entries.append({
            "kind": "item",
            "superseded": raw["slug"] in older_slugs,
            "sort_date": effective_date,
            "date_display": _friendly_date(effective_date),
            "writeup_capable": object_types.can_be_writeup(raw),
            **public,
        })
    for child in child_projects:
        child_items = policy.filter_visible(db.list_project_items(child["id"]))  # #557
        child_start, child_end = timeline.resolve_project_span(child, child_items)
        timeline_children.append({
            "id": child["id"],
            "slug": child["slug"],
            "title": child["title"],
            "effective_start": child_start,
            "effective_end": child_end,
        })
        start_display = _friendly_date(child_start)
        end_display = _friendly_date(child_end)
        grid_entries.append({
            "kind": "project",
            "sort_date": child_start,
            "id": child["id"],  # #376: template needs it for data-project-id (orphan/cover-by-child)
            "slug": child["slug"],
            "title": child["title"],
            "description": child.get("description"),
            "status": child.get("status"),
            "kind": child.get("kind") or "project",
            "stage": child.get("stage"),
            "stage_label": card_rules.stage_label(child.get("stage")),
            "activity": child.get("activity"),
            "cover_url": _project_effective_cover_url(child),
            "item_count": len(child_items),
            "date_display": start_display if start_display == end_display else f"{start_display} – {end_display}",
        })
    grid_entries.sort(key=lambda entry: entry["sort_date"])

    # Curator Stage 2: per-project health score
    project_score = curator.score_project(project)

    # Fetch hobbies this project is in (#365)
    project_hobbies = db.list_hobbies_for_project(project["id"])

    # V2 cards 8.3: the header card, per-type piles, and nested / member cards beside them.
    header_card = _to_card_face(project)
    header_card["href"] = "#stacks"
    stacks = cards.file_stacks(project, raw_items, thumb_fn=lambda r: (
        f"/f/{r['slug']}/thumb" if _should_advertise_thumb(r) else None))
    family_info = cards.family_fields(project)
    beside_cards = [_to_card_face(c) for c in child_projects]
    beside_cards += [_to_card_face(db.get_project(m["id"])) for m in family_info["members"]]
    home_crumbs = []
    for h in reversed(cards.home_chain(project["id"])):
        if h.get("title"):
            home_crumbs.append({"title": h["title"],
                                "href": f"/hobby/{h['slug']}" if h["type"] == "hobby" else f"/project/{h['slug']}"})

    return templates.TemplateResponse(
        request, "project_detail.html",
        {
            "project": project,
            "cover_url": _project_effective_cover_url(project),
            "grid_entries": grid_entries,
            "ancestors": ancestors,
            "writeup_body": writeup_body,
            "start_date_input": _datetime_local_value(project.get("start_date_override")),
            "end_date_input": _datetime_local_value(project.get("end_date_override")),
            "effective_start_display": _friendly_datetime(effective_start),
            "effective_end_display": _friendly_datetime(effective_end),
            "timeline_items": timeline_items,
            "timeline_children": timeline_children,
            "PROJECT_STATUSES": PROJECT_STATUSES,
            # V2 cards: the kind / stage / stop-reason controls on the detail page.
            "card_kinds": [{"key": k, "label": card_rules.KIND_LABELS[k]} for k in card_rules.KINDS],
            "card_stages": [{"key": s, "label": card_rules.STAGE_LABELS[s], "activity": card_rules.ACTIVITY_OF[s]}
                            for s in card_rules.STAGES],
            "card_stop_reasons": [{"key": r, "label": card_rules.STOP_REASON_LABELS[r]} for r in card_rules.STOP_REASONS],
            "card_status": cards.status_fields(project),
            # V2 cards 3.4 / 3.5 / 3.12: whereabouts, card provenance + credit, highlight.
            "card_extra": cards.whereabouts_fields(project),
            # #596: the face text (synopsis, flavor, the cached write-up lead) for the ABOUT group.
            "card_text": cards.text_fields(project),
            "card_text_limits": card_rules.CARD_TEXT_LIMITS,
            # #523: autocomplete suggestions for the free-text credit / whereabouts-note inputs.
            "suggest_credit": db.distinct_card_values("provenance_credit"),
            "suggest_whereabouts_note": db.distinct_card_values("whereabouts_note"),
            "card_whereabouts_options": [{"key": k, "label": card_rules.WHEREABOUTS_LABELS[k]} for k in card_rules.WHEREABOUTS],
            # #529: the live editable list, plus this card's own value if it has since been retired.
            "card_provenance_options": provenance_options.picker_options("card", project.get("provenance")),
            "card_queue": _card_queue_strip(project["slug"]),
            # #515: the "Part of" parent, for the Identity group's fact sheet.
            "parent_card": db.get_project(project["parent_id"]) if project.get("parent_id") else None,
            # V2 cards 3.10: resolved home + the choices for the manual override.
            "card_home": cards.resolve_home(project["id"]),
            "card_home_value": (f"{project['home_kind']}:{project['home_ref']}" if project.get("home_kind") else ""),
            "home_card_options": [{"value": f"card:{c['id']}", "label": c["title"]}
                                  for c in sorted(db.list_projects(), key=lambda c: c["title"].lower())
                                  if c["id"] != project["id"]],
            "home_hobby_options": [{"value": f"hobby:{h['id']}", "label": h["name"]} for h in db.list_hobbies()],
            # V2 cards 3.6: families this card is in / members of this family or collection.
            "family_info": family_info,
            "header_card": header_card,
            "stacks": stacks,
            "rev_note": _rev_note(request, n_sup, rev == "all"),
            "beside_cards": beside_cards,
            "home_crumbs": home_crumbs,
            "project_score": project_score,
            "project_hobbies": [{"id": h["id"], "name": h["name"], "slug": h["slug"]} for h in project_hobbies],
            # #408 / V2 3.8: every link (typed + related, both directions) for the Links row.
            "links": cards.list_links(slug),
            "link_types": [{"key": t, "forward": card_rules.LINK_LABELS[t][0], "reverse": card_rules.LINK_LABELS[t][1]}
                           for t in card_rules.LINK_TYPES],
            # #414: blog entries that feature this project, for the "Featured
            # in" row (reverse of the blog-side entry->projects link).
            "featured_entries": [
                {"slug": e["slug"], "title": e["title"], "status": e["status"]}
                for e in db.list_entries_for_project(project["id"])
            ],
        },
    )


@router.get("/unfiled", response_class=HTMLResponse)
def unfiled_page(request: Request, rev: str = ""):
    """Issue #98: full-page version of home.html's compact Unfiled widget,
    with bulk selection/filing tools the widget has no room for. Reuses the
    exact same db.list_unfiled_items()/_to_public() data shape the widget
    already uses, so the gallery-card markup is identical everywhere."""
    rows, n_sup = _split_revisions(policy.filter_visible(db.list_unfiled_items(include_superseded=True)),
                                   rev == "all")  # #477, #557
    unfiled_items = _card_items(rows)
    return templates.TemplateResponse(
        request, "unfiled.html",
        {"unfiled_items": unfiled_items, "file_provenance_options": provenance_options.picker_options("file"),
         "rev_note": _rev_note(request, n_sup, rev == "all")},
    )


@router.get("/hobbies", response_class=HTMLResponse)
def hobbies_page(request: Request):
    """Hobbies listing page (#360) showing all hobby interests."""
    return templates.TemplateResponse(request, "hobbies.html", {})


@router.get("/hobby/{slug}", response_class=HTMLResponse)
def hobby_detail_page(request: Request, slug: str, rev: str = ""):
    """Hobby page (#360, rebuilt #525 on the same patterns as the project page and home):
    the hobby's own card + a details panel (STATUS / IDENTITY / DATES), the Curator strip for
    this hobby, each member project as a small card with a pile of its files, and the loose
    objects (tagged with the hobby, in none of its member projects)."""
    hobby = db.get_hobby(slug)
    if hobby is None:
        raise HTTPException(status_code=404, detail="hobby not found")

    fields = cards.hobby_fields(hobby)
    projects = db.list_projects_for_hobby(hobby["id"])
    items_by_project = {p["id"]: policy.filter_visible(db.list_project_items(p["id"])) for p in projects}  # #557
    queue = _card_queue_strip(hobby=hobby["slug"])

    def thumb_fn(r):
        return f"/f/{r['slug']}/thumb" if _should_advertise_thumb(r) else None

    # One node per member project: its small card + a pile of its files. A nested child
    # shows under its parent when both are members; every project appears exactly once.
    nodes = {}
    for p in projects:
        face = _to_card_face(p, items_by_project[p["id"]])
        nodes[p["id"]] = {
            "id": p["id"], "slug": p["slug"], "title": p["title"], "face": face,
            "pile": cards.project_pile(p, items_by_project[p["id"]], thumb_fn=thumb_fn),
            "children": [], "parent_id": p.get("parent_id"),
        }

    def _newest(n):
        return -(n["face"]["effective_start"] or n["face"]["created_at"] or 0)

    roots = []
    for n in nodes.values():
        parent = nodes.get(n["parent_id"]) if n["parent_id"] != n["id"] else None
        # Walk up the member chain to catch a (corrupt) cycle: a looping chain is treated as a root.
        seen, cur = {n["id"]}, parent
        while cur is not None and cur["id"] not in seen:
            seen.add(cur["id"])
            cur = nodes.get(cur["parent_id"])
        if parent is not None and cur is None:
            parent["children"].append(n)
        else:
            roots.append(n)
    for n in nodes.values():
        n["children"].sort(key=_newest)
    roots.sort(key=_newest)
    active_roots = [n for n in roots if n["face"]["activity"] == "active"]
    inactive_roots = [n for n in roots if n["face"]["activity"] != "active"]

    hobby_face = cards.hobby_card_face(hobby, projects, items_by_project, needs_input=bool(queue["items"]))
    hobby_face["cover_url"] = _project_cover_url(hobby_face["cover_slug"]) if hobby_face["cover_slug"] else None

    loose_rows, n_sup = _split_revisions(policy.filter_visible(db.list_loose_hobby_objects(hobby["id"], include_superseded=True)),
                                     rev == "all")  # #477, #557
    loose = _card_items(loose_rows)
    start, end = hobby_face["effective_start"], hobby_face["effective_end"]

    return templates.TemplateResponse(
        request, "hobby.html",
        {
            "hobby": fields,
            "hobby_card": hobby_face,
            "hobby_queue": queue,
            "active_nodes": active_roots,
            "inactive_nodes": inactive_roots,
            "project_count": len(projects),
            "loose_items": loose,
            "rev_note": _rev_note(request, n_sup, rev == "all"),
            "dates_label": hobby_face["dates"],
            "dates_start": _friendly_date(start) if start else None,
            "dates_end": _friendly_date(end) if end else None,
            # #562: the per-hobby "shows physical-piece fields" setting (was a name match).
            "shows_physical_piece": db.hobby_shows_physical_piece(hobby["id"]),
            # #596: the hobby card's own synopsis / flavor (the ABOUT group).
            "hobby_text": hobbies.text_fields(hobby),
            "card_text_limits": card_rules.CARD_TEXT_LIMITS,
        },
    )


@router.get("/brand", response_class=HTMLResponse)
def brand_kit_page(request: Request):
    """Brand kit page (#350) showing all reusable branding assets."""
    return templates.TemplateResponse(request, "brand.html", {})


@router.get("/wallpaper", response_class=HTMLResponse)
def wallpaper_page(request: Request):
    """Wallpaper home (#422) — a large-preview, download-oriented gallery of
    every wallpaper-tagged object (see db.WALLPAPER_TAG_NAMES)."""
    return templates.TemplateResponse(request, "wallpaper.html", {})


@router.get("/gallery/user/{uploader}", response_class=HTMLResponse)
def user_gallery_page(request: Request, uploader: str, rev: str = ""):
    # #563: no 1000-row cap (it made "1000 uploads" of 1,640). The page embeds the whole list and
    # the ItemCards pager (batches of 120) draws it lazily, so the count is the true total.
    rows, n_sup = _split_revisions(policy.filter_visible(db.search(uploaded_by=uploader, limit=10_000_000)),
                                   rev == "all")  # #477, #557
    items = _card_items(rows)
    return templates.TemplateResponse(
        request, "user_gallery.html",
        {"uploader": uploader, "uploader_display": uploader, "items": items,
         "rev_note": _rev_note(request, n_sup, rev == "all")},
    )


@router.get("/object/{slug}", response_class=HTMLResponse)
def object_detail_page(request: Request, slug: str):
    """Generic detail page for any capture_events row, regardless of
    media_type — image, youtube, document, or anything else. Canonical route
    for what used to be the image-only /image/<slug> page (see the redirect
    below); the template branches on item.media_type/is_file to render the
    right preview (uploaded image, embedded YouTube player, or plain text
    content) instead of assuming every row has an uploaded file.
    """
    row = db.get_by_slug(slug)
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    policy.require_view(row)  # #557: the item policy (today: served; #467: restricted needs admin)
    item = _to_object_detail(row)
    full_url = str(request.base_url).rstrip("/") + item["url"] if item["is_file"] else None
    full_object_url = str(request.base_url).rstrip("/") + f"/object/{slug}"
    # #52: relations (core/db.py's add_relation/remove_relation, #16) are
    # type-agnostic — a plain slug-to-slug link with no media_type or
    # is_file check on the backend — so this used to gate the Related panel
    # on item["is_file"] was a leftover from before the object-type registry
    # existed (predating #15) that accidentally hid "Add related" for every
    # content-only row (youtube, document posts), not just non-file types.
    # Always computed now so every object type gets the same panel.
    related = [_to_public(r) for r in policy.filter_visible(db.list_related(slug))]  # #557
    # #137: breadcrumb navigation — read the from param and build the breadcrumb list
    from_param = request.query_params.get("from")
    breadcrumbs = _build_breadcrumbs(from_param, item["display_name"])
    if not from_param and item["projects"]:
        # #515: arriving with no `from`, the trail goes through the item's home project
        # (its most recently updated one), then that card's own home chain above it.
        home = item["projects"][0]
        trail = [{"label": "Home", "href": "/"}]
        for h in reversed(cards.home_chain(home["id"])):
            if h.get("title"):
                trail.append({"label": h["title"],
                              "href": f"/hobby/{h['slug']}" if h["type"] == "hobby" else f"/project/{h['slug']}"})
        trail.append({"label": home["title"], "href": f"/project/{home['slug']}"})
        trail.append({"label": item["display_name"], "href": None})
        breadcrumbs = trail
    # #425: the PHYSICAL PIECE group shows when any field is set or the item is in a hobby whose
    # "shows physical-piece fields" setting is on (#562; was the hobby named Traditional Media).
    physical = {
        "rows": physical_piece.rows(item["type_metadata"]),
        "show": physical_piece.has_any(item["type_metadata"])
                or physical_piece.in_physical_piece_hobby(db, slug, item.get("tags")),
        "medium_suggestions": physical_piece.medium_suggestions(db),
    }
    redact_hold = db.get_redact_hold(slug) if row.get("redacted") else None
    return templates.TemplateResponse(
        request, "object_detail.html",
        {"item": item, "redact_hold": redact_hold, "revisions": revisions.revision_view(slug), "full_url": full_url, "full_object_url": full_object_url, "related": related, "breadcrumbs": breadcrumbs, "file_provenance_options": provenance_options.picker_options("file", item.get("provenance")), "file_provenance_label": provenance_options.label("file", item.get("provenance")), "BRAND_ROLES": BRAND_ROLES, "physical": physical},
    )


@router.get("/image/{slug}")
def image_detail_redirect(slug: str):
    # /image/<slug> was the original imagerepo-era canonical route (image-only).
    # /object/<slug> replaced it so any bookmarked/hotlinked old URLs still resolve.
    return RedirectResponse(f"/object/{slug}", status_code=308)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    return templates.TemplateResponse(request, "account.html", {})


@router.get("/admin", response_class=HTMLResponse, dependencies=requires(roles.ADMIN))
def admin_page(request: Request, embed: int = 0):
    """#295: the admin surface as its own full page. It was a bottom-right
    pop-out (_admin_pane.html) included on every page until it outgrew a
    320px column; every panel on it still talks to the same /api/* routes
    it always did (settings, pending-decisions, redacted, audit-log,
    captions/test, backup, delete-all) -- the page itself carries no data,
    the JS fetches it, so there's nothing to pass in here.

    #345: ?embed=1 renders a chrome-less version (no header, no nav rail,
    no other drawers) so it can be loaded inside the Admin nav-rail drawer's
    iframe without nested chrome. The panels/JS are identical either way."""
    # #562: "Finish setting up this install" banner until the owner name is set (Admin > Install).
    # #467 step 1: "Create the admin account" banner while the install has no user at all.
    return templates.TemplateResponse(request, "admin.html", {"embed": bool(embed),
                                                              "install_setup_needed": install_config.setup_needed(),
                                                              "users_setup_needed": users.setup_needed()})


# #562: in-app guides (served from the app, not the owner's GitHub). slug -> (file, title).
GUIDES_DIR = Path(__file__).resolve().parent.parent / "guides"
GUIDES = {
    "capture-physical-piece": ("capture-physical-piece.md", "Capturing a physical piece"),
}


@router.get("/guides/{guide}", response_class=HTMLResponse)
def guide_page(request: Request, guide: str):
    """#562: a how-to guide shipped with the app (web/guides/*.md), rendered as safe Markdown.
    Only the guides listed in GUIDES exist; anything else is a 404. The hobby page links the
    physical-piece capture guide when its "shows physical-piece fields" setting is on."""
    entry = GUIDES.get(guide)
    if entry is None:
        raise HTTPException(status_code=404, detail="guide not found")
    text = (GUIDES_DIR / entry[0]).read_text(encoding="utf-8")
    return templates.TemplateResponse(request, "guide.html", {
        "title": entry[1], "body": markdown_render.render(text, breaks=False)})


@router.get("/curator", response_class=HTMLResponse)
def curator_dashboard_page(request: Request):
    """Curator Stage 2 dashboard: state of the beast. Displays aggregate
    project health scoring, project status distribution, and gap buckets
    (which checks are failing most frequently). The page loads the dashboard
    data via /api/curator/dashboard."""
    return templates.TemplateResponse(request, "curator.html", {})


@router.get("/captions/review", response_class=HTMLResponse)
def captions_review_page(request: Request):
    """#409: bulk review surface for auto-caption suggestions — the actionable
    destination for the confirm_caption nudge (was dumping to /admin)."""
    return templates.TemplateResponse(request, "captions_review.html", {})
