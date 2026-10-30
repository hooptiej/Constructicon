"""Card faces (8.1, #596): card_face, hobby_card_face, file_face, piles and stacks, the hobby cover.

Part of the core.cards package (#547): import it as `from core import cards` and use
`cards.<name>`; core/cards/__init__.py re-exports every name.
"""

import time

from .. import card_rules, datefmt, db, item_dates, item_title, markdown_render, policy, timeline
from .base import get_card
from .text import face_text, FACE_TEXT_MAX
from .families import card_span
from .links import list_links
from .decisions import status_fields
from .reshape import resolve_home


# --- Card face (8.1, #596) ---------------------------------------------------------------
# Everything a card renderer needs, in one dict, zone by zone (the V2 mockup's Card template,
# drawn by web/templates/_card.html):
#   name bar, dates + pips, art, type line + group codes,
#   text box   relationship lines (`rel_lines` + `rel_more`), the face text (`text`: synopsis, else
#              the write-up lead, else the description; see face_text), `notes` (a hobby's project
#              list and group-code line), then the italic `flavor` under a rule;
#   status box `zone` ("In progress", "Stopped · failed", "Inside · <parent>", "Stacked · <card>");
#   stat box   `stat` ("103 stacked · 1 nested", "3 members", "10 projects", "JPEG");
#   footer     `foot` ("RCA · R/C Adventures", "COL · family card", "RCA · group card") + "n of N".
# No face shows provenance, credit or whereabouts (#596): those live in the details panel only.
# Reuses card_level / resolve_home / list_links rather than recomputing; explain_card is the long
# form for a single card, this is the light form for a whole page of them.

FACE_REL_LINES = 2          # relationship lines on a face, then "+N more"
FACE_PROJECT_LIST_MAX = 160  # characters of a hobby face's project list, then "+N more"
_DOT = " · "


def date_range_label(start, end, active=False, now=None):
    """`Nov 2025 - Jul 2026`; a single moment `Jul 2026`; an active card whose end is in
    the past `Nov 2025 - now` (8.1). Month granularity, Mountain Time (#544)."""
    if start is None:
        return ""
    first = datefmt.month(start)
    last = datefmt.month(end if end is not None else start)
    if active:
        now_label = datefmt.month(now if now is not None else time.time())
        if last != now_label:
            return f"{first} - now"
    return first if first == last else f"{first} - {last}"


def _count(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


def _face_relations(kind, links, parent):
    """The text box's relationship lines (#596): an action that is part of a card and has no
    applies_to link of its own reads "Applies to: <parent>."; then every OUTGOING directed typed
    link, "<Label>: <title>." ("Built for: X.", "Applies to: X."). Returns (lines, more)."""
    lines = []
    out = [lk for lk in links if lk["direction"] == "out" and lk["type"] in card_rules.DIRECTED_LINK_TYPES]
    if kind == "action" and parent and not any(lk["type"] == "applies_to" for lk in out):
        lines.append(f"{card_rules.link_label('applies_to')}: {parent['title']}.")
    lines += [f"{lk['label']}: {lk['title']}." for lk in out]
    return lines[:FACE_REL_LINES], max(0, len(lines) - FACE_REL_LINES)


def _face_zone(status, parent):
    """The status box: "Inside · <parent>" for a nested card, else the stage label
    ("Stopped · failed" with its reason)."""
    if parent:
        return f"Inside{_DOT}{parent['title']}"
    label = status["stage_label"] or ""
    if status["stage"] == "stopped" and status["stop_reason_label"]:
        label += f"{_DOT}{status['stop_reason_label'].lower()}"
    return label


def _face_stat(kind, card_id, n_files):
    """The stat box: a group's member count; otherwise the files stacked under the card and the
    cards nested in it. "" when there is nothing to count."""
    if kind in card_rules.GROUP_KINDS:
        return _count(db.count_family_members(card_id), "member")
    nested = len(db.list_child_projects(card_id))
    parts = ([f"{n_files} stacked"] if n_files else []) + ([f"{nested} nested"] if nested else [])
    return _DOT.join(parts)


def _face_foot(kind, hobbies, home):
    """The footer's left side: "<CODE> · <hobby>" for the card's home hobby (else its first),
    "+N" when it has more; "<CODE> · family card" / "collection card" for a group."""
    primary = next((h for h in hobbies if home.get("type") == "hobby" and h["id"] == home.get("id")),
                   hobbies[0] if hobbies else None)
    code = primary["code"] if primary else ""
    if kind in card_rules.GROUP_KINDS:
        return _DOT.join(x for x in (code, f"{kind} card") if x)
    if primary is None:
        return ""
    foot = _DOT.join(x for x in (code, primary["name"]) if x)
    return foot + (f" +{len(hobbies) - 1}" if len(hobbies) > 1 else "")


def card_face(card, items=None):
    """The card face data for one project/card row (a `db.get_project` dict): zone content
    for web/templates/_card.html (see the section comment above). `cover_slug` is the resolved
    cover (the caller turns it into a URL). Hobby faces: hobby_card_face; file faces: file_face."""
    from .. import card_level  # lazy, as in explain_card
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    kind = row.get("kind") or "project"
    start, end = card_span(row, items)
    status = status_fields(row)
    level = card_level.card_level(row, items)
    home = resolve_home(row["id"])
    fams = db.list_families_for_member(row["id"])
    parent = db.get_project(row["parent_id"]) if row.get("parent_id") else None
    hobbies = []
    for hr in db.list_hobby_rows(row["id"]):
        h = db.get_hobby(hr["hobby_tag_id"])
        if h:
            hobbies.append({"id": h["id"], "slug": h["slug"], "name": h["name"],
                            "code": h.get("group_code") or "",
                            "active": (h.get("hobby_status") or "active") == "active"})
    rel_lines, rel_more = _face_relations(kind, list_links(row["id"]), parent)
    text, text_source = face_text(row)

    type_line = status["kind_label"]
    if home.get("title"):
        type_line += f" - {home['title']}"
    elif kind in card_rules.GROUP_KINDS:
        type_line += " - group"

    n_files = len([i for i in items if i["slug"] != row.get("writeup_slug")])

    order = None
    if len(fams) == 1:
        rows = db.list_family_rows(fams[0]["id"], as_member=False)
        orders = [r["sort_order"] for r in rows]
        if len(rows) > 1 and len(set(orders)) == len(orders):
            ranked = sorted(rows, key=lambda r: r["sort_order"])
            order = {"n": [r["member_id"] for r in ranked].index(row["id"]) + 1, "of": len(rows)}

    return {
        "slug": row["slug"], "title": row["title"], "description": row.get("description") or "",
        **status,
        "highlight": bool(row.get("highlight")),
        # #770: grey art = "I don't have it" (whereabouts gone); Stopped only adds a small tag.
        "gone": card_rules.whereabouts_gone(kind, row.get("whereabouts")),
        "stopped_tag": row.get("stage") == "stopped",
        "effective_start": start, "effective_end": end, "created_at": row["created_at"],
        "dates": date_range_label(start, end, active=row.get("activity") == "active"),
        "level": level["score"], "level_checks": level["checks"],
        "show_level": True,
        "cover_slug": db.resolve_project_cover_slug(row),
        "type_line": type_line,
        "hobbies": hobbies,
        "codes": [h["code"] for h in hobbies if h["code"]],
        "rel_lines": rel_lines, "rel_more": rel_more,
        "text": text, "text_source": text_source, "notes": [],
        "flavor": row.get("flavor") or "",
        "zone": _face_zone(status, parent),
        "stat": _face_stat(kind, row["id"], n_files),
        "foot": _face_foot(kind, hobbies, home),
        "order": order,
        "family_ids": [f["id"] for f in fams],
        "card_id": row["id"],
    }


def file_face(*, slug, title, dates, type_line, cover_url, href, codes=(), highlight=False, stacked=None,
              text="", stat=""):
    """An asset (file) card face (8.1, #596), in card_face's shape: kind "asset", no pips, the
    file's own date, type line = its type label. Status box "Stacked · <card>" when the file is on
    a card; footer "<its first group code> · <card>"; text box = the file's own description; stat
    = what the caller can say cheaply (the file extension). No provenance (#596).
    web/static/js/cards.js builds the same face client-side for the item grids;
    scripts/check_item_cards.py renders both from this function and compares them."""
    code = codes[0] if codes else ""
    return {
        "slug": slug, "kind": "asset", "title": title, "dates": dates,  # no kind_label: the icon's title is the type
        "type_line": type_line, "cover_url": cover_url, "href": href,
        "codes": list(codes), "highlight": bool(highlight), "show_level": False,
        "rel_lines": [], "rel_more": 0, "text": markdown_render.clamp(text, FACE_TEXT_MAX), "notes": [],
        "flavor": "", "zone": f"Stacked{_DOT}{stacked}" if stacked else "", "stat": stat,
        "foot": _DOT.join(x for x in (code, stacked) if x), "order": None,
    }


STACK_FAN_MAX = 7   # cards shown when a pile is fanned out (then "+N")


def _day_label(ts):
    return datefmt.day(ts)


def _by_real_date(rows):
    """[(real date or None, row)], earliest first, undated last (#725; stable, so they keep their
    list order)."""
    cutoff = timeline.first_upload()
    dated = [(timeline.resolve_item_date(r, cutoff), r) for r in rows]
    return sorted(dated, key=lambda p: timeline.date_sort_key(p[0]))


def _pile_span(dated):
    """A pile's `Mon YYYY - Mon YYYY` from its REAL dates; "Undated" when it has none (#725)."""
    real = [ts for ts, _r in dated if ts is not None]
    return date_range_label(real[0], real[-1]) if real else item_dates.UNDATED_LABEL


def _asset_card(r, ts, spec, card_row, thumb_fn):
    """One file's face (file_face) for a pile or a fan on `card_row`'s page. `ts` is its real date
    or None (#725): an undated file's date line is what core/item_dates.py says it shows."""
    title = item_title.title_of(r)
    text = next((t for t in (r.get("content_description"), r.get("description")) if t and t != title), "")
    name = r.get("filename") or ""
    ext = name.rpartition(".")[2] if "." in name else ""
    dates = _day_label(ts) if ts is not None else item_dates.item_date(r)["label"]
    return file_face(slug=r["slug"], title=title, dates=dates, type_line=spec.label,
                     cover_url=thumb_fn(r) if thumb_fn else None,
                     href=f"/object/{r['slug']}?from=project:{card_row['slug']}",
                     stacked=card_row["title"], text=text, stat=ext.upper())


def project_pile(card, items=None, thumb_fn=None):
    """One mixed-type pile of a card's files (the hobby page, #525): the same asset cards and
    the same flat-top / tilted-under / count-badge shape as a detail-page pile, but across
    every file type. Earliest first, like file_stacks. Returns None for a card with no files.
    `count` is every file in the card's grid (write-up included), matching file_stacks."""
    from .. import object_types  # lazy, same reason as the other lazy imports in this module
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    if not items:
        return None
    dated = _by_real_date(items)
    shown = dated[:STACK_FAN_MAX]
    cards = [_asset_card(r, ts, object_types.get_object_type(r.get("media_type") or "unknown"), row, thumb_fn)
             for ts, r in shown]
    return {"count": len(items), "span": _pile_span(dated),
            "cards": cards, "more": max(0, len(items) - len(cards))}


def _project_list_line(titles):
    """ "Projects: A, B, C, +N more." for a hobby face, clamped to FACE_PROJECT_LIST_MAX."""
    shown = []
    for t in titles:
        if shown and len(", ".join(shown + [t])) > FACE_PROJECT_LIST_MAX:
            break
        shown.append(t)
    more = len(titles) - len(shown)
    return "Projects: " + ", ".join(shown) + (f", +{more} more." if more else ".")


def hobby_cover_usable(slug):
    """#686: may `slug` be shown as a hobby's cover right now? The item exists, isn't redacted,
    the current actor may see it (core/policy.py) and its type has a picture (a thumbnail
    strategy). The web layer passes a stricter check (its thumb-route rules) to the resolver."""
    from .. import object_types, policy  # lazy: keep cards' import graph as it was
    row = db.get_by_slug(slug) if slug else None
    if row is None or row.get("redacted") or not policy.can_view(row):
        return False
    return object_types.get_object_type(row.get("media_type")).thumbnail_source != object_types.ThumbnailSource.NONE


def recent_first(projects):
    """#686: "most recent" member project = the latest `projects.updated_at` (bumped by every Save,
    file added, cover or status change of the card), then the latest `created_at`, then the highest
    id, so the order is total and stable."""
    return sorted(projects, key=lambda p: (p.get("updated_at") or 0, p.get("created_at") or 0, p.get("id") or 0),
                  reverse=True)


def resolve_hobby_cover(hobby_row, projects=None, usable=None):
    """#686: the cover a hobby shows, and where it came from. In order:
      1. its chosen cover (hobby_settings.cover_slug, hobbies.set_cover), when `usable`;
      2. automatic: the cover of its highlighted member project (projects.highlight; several
         highlighted = the most recent first);
      3. automatic: the cover of its most recent member project that has one (_recent_first);
      4. none (the card shows its placeholder).
    A project's cover is its resolved one (db.resolve_project_cover_slug: its own, or the child
    card's it borrows). A chosen cover that was deleted, trashed, redacted or hidden from this
    actor is not shown (no broken image): the automatic one is, and `picked_unavailable` says so;
    the choice itself is kept, so undoing the delete brings it back. Read-only.
    `usable(slug) -> bool` defaults to hobby_cover_usable. Returns {slug, source (card_rules.
    HOBBY_COVER_* or None), picked (bool), picked_slug, picked_unavailable, project (slug or None),
    project_title}."""
    usable = usable or hobby_cover_usable
    settings = db.get_hobby_settings(hobby_row["id"])
    picked = settings.get("cover_slug") or None
    picked_card = settings.get("cover_project_id")
    out = {"slug": None, "source": None, "picked": False, "picked_slug": picked, "picked_unavailable": False,
           "project": None, "project_title": None, "picked_card_id": picked_card}
    if picked:
        if usable(picked):
            out.update(slug=picked, source=card_rules.HOBBY_COVER_PICKED, picked=True)
            return out
        out["picked_unavailable"] = True
    if projects is None:
        projects = policy.filter_visible_cards(db.list_projects_for_hobby(hobby_row["id"]))  # #604 step 3
    if picked_card and not picked:
        # #554: a member card ticked as the cover lends its RESOLVED cover, so the hobby follows it.
        # Gone, out of the hobby, hidden from this actor or coverless = the automatic one, flagged.
        lender = next((p for p in projects if p["id"] == picked_card), None)
        slug = db.resolve_project_cover_slug(lender) if lender else None
        if slug and usable(slug):
            out.update(slug=slug, source=card_rules.HOBBY_COVER_CARD, picked=True,
                       project=lender["slug"], project_title=lender["title"])
            return out
        out["picked_unavailable"] = True
    ranked = recent_first(projects)
    highlighted = [p for p in ranked if p.get("highlight")]
    rest = [p for p in ranked if not p.get("highlight")]
    for source, group in ((card_rules.HOBBY_COVER_HIGHLIGHT, highlighted), (card_rules.HOBBY_COVER_RECENT, rest)):
        for p in group:
            slug = db.resolve_project_cover_slug(p)
            if slug and usable(slug):
                out.update(slug=slug, source=source, project=p["slug"], project_title=p["title"])
                return out
    return out


def hobby_cover_fields(cover):
    """#686: a resolve_hobby_cover result in the flat shape APIs and tools return (the caller adds
    `cover_url`): cover_slug, cover_picked, cover_source, cover_source_label, cover_project,
    picked_cover_slug, picked_cover_unavailable."""
    return {
        "cover_slug": cover["slug"],
        "cover_picked": cover["picked"],
        "cover_source": cover["source"],
        "cover_source_label": card_rules.HOBBY_COVER_SOURCE_LABELS.get(cover["source"]),
        "cover_project": cover["project"],
        "picked_cover_slug": cover["picked_slug"],
        "picked_cover_card_id": cover.get("picked_card_id"),  # #554: the member card it borrows from
        "picked_cover_unavailable": cover["picked_unavailable"],
    }


def hobby_card_face(hobby_row, projects, items_by_project, needs_input=False, cover=None):
    """The card face for a hobby (#525, #596), in the same dict shape `card_face` returns so
    web/templates/_card.html draws it (kind 'hobby': the green frame, the mountain icon). The
    text box is the hobby's own synopsis (hobby_settings, hobbies.set_text), then its member
    projects by name ("+N more") and "Every card in this hobby carries its group code, CODE.";
    status box "Hobby · active|inactive", stat "N projects", footer "<CODE> · group card". The
    date line is the computed span of the member projects' real dates; the cover is
    resolve_hobby_cover's (#686: chosen, else highlighted project's, else the most recent
    project's; pass `cover` when the caller already resolved it, e.g. with the web layer's
    thumbnail check). The caller turns `cover_slug` into a URL. `items_by_project` maps project
    id -> db.list_project_items rows."""
    status = hobby_row.get("hobby_status") or "active"
    status_label = card_rules.HOBBY_ACTIVITY_LABELS.get(status, status)
    spans = [card_span(p, items_by_project.get(p["id"], [])) for p in projects]
    starts = [s for s, _ in spans if s is not None]
    ends = [e for _, e in spans if e is not None]
    start = min(starts) if starts else None
    end = max(ends) if ends else None
    code = hobby_row.get("group_code") or ""
    settings = db.get_hobby_settings(hobby_row["id"])
    if cover is None:
        cover = resolve_hobby_cover(hobby_row, projects)
    label = card_rules.HOBBY_LABEL
    notes = [_project_list_line([p["title"] for p in projects])] if projects else []
    if code:
        notes.append(f"Every card in this {label.lower()} carries its group code, {code}.")
    return {
        "slug": hobby_row["slug"], "kind": "hobby", "kind_label": label,
        "href": "#stacks",
        "title": hobby_row["name"], "description": "",
        "activity": status, "stage": None, "stage_label": status_label, "stop_reason_label": None,
        "needs_input": bool(needs_input),
        "highlight": False,
        "effective_start": start, "effective_end": end, "created_at": hobby_row.get("created_at") or 0,
        "dates": date_range_label(start, end, active=status == "active"),
        "level": 0, "show_level": False,
        "cover_slug": cover["slug"],
        "cover": cover,
        "type_line": f"{label}{_DOT}{status_label}",
        "codes": [code] if code else [],
        "rel_lines": [], "rel_more": 0,
        "text": markdown_render.clamp(settings.get("synopsis") or "", FACE_TEXT_MAX),
        "text_source": "synopsis" if settings.get("synopsis") else None,
        "notes": notes, "flavor": settings.get("flavor") or "",
        "zone": f"{label}{_DOT}{status_label.lower()}",
        "stat": _count(len(projects), "project"),
        "foot": _DOT.join(x for x in (code, "group card") if x),
        "order": None,
    }


def file_stacks(card, items=None, thumb_fn=None):
    """Piles for the detail page (8.3): one per `media_type` of the card's files, biggest
    first. Each pile: `media_type`, `label` (registry label), `count`, `span` (the
    `Mon YYYY - Mon YYYY` date span of the files' resolved dates), `cards` (up to
    STACK_FAN_MAX file faces, earliest first), and `more` (files beyond the fan).
    `thumb_fn(row)` gives a row's thumbnail URL or None (the page decides, since thumbnails
    depend on storage). Counts here are every file in the card's grid, write-up included."""
    from .. import object_types  # lazy, same reason as the other lazy imports in this module
    row = get_card(card) if not isinstance(card, dict) else card
    if items is None:
        items = db.list_project_items(row["id"])
    by_type = {}
    for it in items:
        by_type.setdefault(it.get("media_type") or "unknown", []).append(it)
    piles = []
    for mt, rows in by_type.items():
        spec = object_types.get_object_type(mt)
        dated = _by_real_date(rows)
        shown = dated[:STACK_FAN_MAX]
        cards = [_asset_card(r, ts, spec, row, thumb_fn) for ts, r in shown]
        piles.append({
            "media_type": mt, "label": spec.label, "count": len(rows),
            "span": _pile_span(dated),
            "cards": cards, "more": max(0, len(rows) - len(cards)),
        })
    piles.sort(key=lambda p: (-p["count"], p["label"]))
    return piles
