"""The date an item SHOWS (#725, "Import date is not a birthday"), and the undated items' guesses.

core/timeline.py decides an item's REAL date (hand-set, date made, content_date, a file mtime from
before the install's first upload) or None. This module adds what a page shows for the None case:

  * real date      -> that date ("Mar 4, 2021").
  * undated, on a card whose REAL-dated range exists -> the card's range, approximately:
                      "≈ Nov 2019 - Jul 2021 · from Desk Build". Derived at read time, NEVER stored
                      as content_date, so it follows the card as its range changes; and it never
                      feeds back into the card's own span (spans read real dates only).
  * undated, no range to inherit -> "Undated", and the Curator's `needs_date` need
                      (needs_date_rows below; core/curation_queue.py shows it).

Which card lends its range (owner decision on #725 to pick one and document it): the card the item
was FIRST filed into (the lowest project_items rowid), the same card the file face's "Stacked ·
<card>" box already names (db.first_card_titles), so "from <card>" and the footer agree. Not the
union of all its cards' ranges. A card that has no range (nothing real-dated, no Timeline overrides)
lends nothing, even when a later card of the same item has one.

The range a card lends is its span over its NON-sensitive, non-redacted items only (the same for
every viewer, so an approximate date can't reveal that a hidden item exists), with the card's own
Timeline start/end overrides winning, as on the card itself. The whole map is computed in one query
and cached until anything is written to the database (db.change_fingerprint); never cached across
an open transaction.

Guesses (core/date_guess.py): `guesses_for(row, read_file=...)` gathers the evidence (the file's
own embedded date through its type's embedded_metadata_fn, the card's other real dates) and asks
the pure guesser. `auto_plan()` lists the high-confidence ones to apply; the write is
core/items.py `apply_dates` (one batch, undoable).
"""

import threading

from . import card_rules, datefmt, date_guess, db, item_title, policy, timeline

APPROX = "≈"
UNDATED_LABEL = "Undated"
NO_SPAN_LABEL = "No real dates yet"   # a card's start / end when nothing on it is really dated
_DOT = " · "

_cache = {"key": None, "snap": None}
_cache_lock = threading.Lock()


def _date_row(r):
    """A date_lending_rows / item_date_rows row in the shape core/timeline.py reads."""
    return {"slug": r["slug"], "media_type": r.get("media_type"), "timestamp": r.get("timestamp"),
            "content_date": r.get("content_date"), "display_date_override": r.get("display_date_override"),
            "source_modified_at": r.get("source_modified_at"),
            "type_metadata": {"date_made": r["date_made"]} if r.get("date_made") not in (None, "") else {}}


def _build():
    cards, links = db.date_lending_rows()
    cutoff = timeline.first_upload()
    by_id = {c["id"]: c for c in cards}
    first = {}        # item slug -> (link_order, card id)
    dates = {}        # card id -> [real dates of its eligible, visible-to-everyone items]
    for r in links:
        prev = first.get(r["slug"])
        if prev is None or r["link_order"] < prev[0]:
            first[r["slug"]] = (r["link_order"], r["project_id"])
        card = by_id.get(r["project_id"])
        if card is None or r.get("redacted") or r.get("blank_doc") or r["slug"] == card.get("writeup_slug"):
            continue
        if policy.restriction_reason(r) is not None:
            continue  # sensitive / restricted type: its date must not shape what others see
        d = timeline.resolve_item_date(_date_row(r), cutoff)
        if d is not None:
            dates.setdefault(r["project_id"], []).append(d)
    ranges = {}
    for cid, card in by_id.items():
        start, end = timeline.span_from_dates(card, dates.get(cid, []))
        if start is not None:
            ranges[cid] = (start, end)
    # #772: a family / collection lends the range rolled up over its members (one level: members
    # can't be groups), with its own overrides still winning. The members' ranges are the ones just
    # built, so the same visible-to-everyone rule applies to them.
    members = {}
    for fid, mid in db.family_member_pairs():
        members.setdefault(fid, []).append(mid)
    for fid, mids in members.items():
        card = by_id.get(fid)
        if card is None or (card.get("kind") or "project") not in card_rules.GROUP_KINDS:
            continue
        if card.get("start_date_override") is not None and card.get("end_date_override") is not None:
            continue  # both overrides set: already its range
        rolled = dates.get(fid, []) + timeline.member_span_dates(ranges.get(m) for m in mids if m in ranges)
        start, end = timeline.span_from_dates(card, rolled)
        if start is not None:
            ranges[fid] = (start, end)
    writeups = {c["writeup_slug"] for c in cards if c.get("writeup_slug")}
    writeups |= {r["slug"] for r in links if r.get("blank_doc")}
    return {"cards": by_id, "first": {s: cid for s, (_o, cid) in first.items()},
            "ranges": ranges, "dates": dates, "cutoff": cutoff, "writeups": writeups}


def snapshot():
    """{cards, first (item slug -> card id), ranges (card id -> (start, end)), dates, cutoff,
    writeups (slugs that are a card's write-up, or an empty written post on a card)}."""
    if db.in_transaction():
        return _build()
    key = (str(db.DB_PATH), db.change_fingerprint())
    with _cache_lock:
        if _cache["key"] == key:
            return _cache["snap"]
    snap = _build()
    with _cache_lock:
        _cache.update(key=key, snap=snap)
    return snap


def lent_range(slug, snap=None):
    """The range an item inherits from its first card: {start, end, card_id, card_slug,
    card_title}, or None (on no card, or its first card has no real-dated range)."""
    snap = snap or snapshot()
    cid = snap["first"].get(slug)
    if cid is None or cid not in snap["ranges"]:
        return None
    card = snap["cards"][cid]
    start, end = snap["ranges"][cid]
    return {"start": start, "end": end, "card_id": cid, "card_slug": card["slug"], "card_title": card["title"]}


def item_date(row, snap=None):
    """What date `row` shows: {epoch (the real date or None), source (timeline.date_source),
    approx (True when inherited), start, end, card_slug, card_title (the lending card, or None),
    sort_epoch (the real date, else the range's start, else None), label}."""
    snap = snap or snapshot()
    real = timeline.resolve_item_date(row, snap["cutoff"])
    info = {"epoch": real, "source": timeline.date_source(row, snap["cutoff"]), "approx": False,
            "start": None, "end": None, "card_slug": None, "card_title": None}
    if real is None:
        lent = lent_range(row["slug"], snap)
        if lent is not None:
            info.update(approx=True, start=lent["start"], end=lent["end"],
                        card_slug=lent["card_slug"], card_title=lent["card_title"])
    info["sort_epoch"] = real if real is not None else info["start"]
    info["label"] = label(info)
    return info


def range_label(start, end):
    """`Nov 2019 - Jul 2021`, or `Jul 2021` for a single month (cards.date_range_label's format)."""
    first, last = datefmt.month(start), datefmt.month(end if end is not None else start)
    return first if first == last else f"{first} - {last}"


def label(info):
    """The date line for an item_date(): the day, "≈ <range> · from <card>", or "Undated"."""
    if info["epoch"] is not None:
        return datefmt.day(info["epoch"])
    if info["approx"]:
        return f"{APPROX} {range_label(info['start'], info['end'])}{_DOT}from {info['card_title']}"
    return UNDATED_LABEL


def sort_key(row, newest_first=False, snap=None):
    """The Date sort's key for one row: undated (nothing real, nothing inherited) LAST in either
    direction; an inherited one sorts at its range's start; ties by upload time, same direction."""
    info = item_date(row, snap)
    d = info["sort_epoch"]
    ts = row.get("timestamp") or 0
    if d is None:
        return (1, -ts if newest_first else ts)
    return (0, -d if newest_first else d, -ts if newest_first else ts)


# --- the undated ---------------------------------------------------------------------------

def undated_rows(include_inherited=True, snap=None):
    """Full rows of every non-redacted item with no real date, oldest upload first. With
    include_inherited=False only those that also inherit nothing (the `needs_date` ones)."""
    snap = snap or snapshot()
    slugs = []
    for r in db.item_date_rows():
        if timeline.resolve_item_date(_date_row(r), snap["cutoff"]) is not None:
            continue
        if not include_inherited and lent_range(r["slug"], snap) is not None:
            continue
        slugs.append((r.get("timestamp") or 0, r["slug"]))
    rows = db.get_by_slugs([s for _ts, s in sorted(slugs)])
    for row in rows:
        row.pop("embedding", None)
    return rows


def sibling_dates(row, snap=None):
    """(the item's first card's title or None, the real dates of the card's other eligible items)."""
    snap = snap or snapshot()
    cid = snap["first"].get(row["slug"])
    if cid is None:
        return None, []
    return snap["cards"][cid]["title"], list(snap["dates"].get(cid, []))


def guesses_for(row, read_file=False, snap=None, name_card=True):
    """The ranked date guesses for one row. `read_file` also reads the file's own embedded date
    through its type's embedded_metadata_fn (a parser child process per file: the backfill and the
    MCP's apply, not a page load). `name_card=False` leaves the card's title out of the evidence."""
    from . import embedded_metadata  # lazy: it imports the type registry
    file_date = None
    if read_file:
        file_date = embedded_metadata.plausible_content_date(embedded_metadata.extract(row).get("content_date"))
    card_title, dates = sibling_dates(row, snap)
    if not name_card:
        card_title = None
    return date_guess.guesses(row, file_metadata_date=file_date, sibling_dates=dates, card_title=card_title)


def source_record(guess, accepted_by="auto"):
    """What type_metadata.content_date_source records for an applied guess."""
    return {"source": guess.source, "confidence": guess.confidence, "evidence": guess.evidence[:200],
            "guessed": True, "accepted": accepted_by}


def auto_plan(slugs=None, read_files=True):
    """[(row, Guess)] for every undated item (or just `slugs`, when they are undated) that has a
    high-confidence guess. Read-only; core/items.py apply_dates writes it."""
    snap = snapshot()
    rows = undated_rows(snap=snap)
    if slugs is not None:
        wanted = set(slugs)
        rows = [r for r in rows if r["slug"] in wanted]
    plan = []
    for row in rows:
        g = date_guess.best_auto(guesses_for(row, read_file=read_files, snap=snap))
        if g is not None:
            plan.append((row, g))
    return plan


def needs_date(row, info=None, snap=None):
    """Does an undated item belong in the Curator? Only when it has no real date, borrows no card
    range, and is not a card's write-up (a write-up documents the card; its own date is not the
    content's, which is also why spans skip it)."""
    snap = snap or snapshot()
    info = info or item_date(row, snap)
    return info["epoch"] is None and not info["approx"] and row["slug"] not in snap["writeups"]


def needs_date_rows():
    """The Curator's `needs_date` items (see needs_date). Each {row, card (the first card's {slug,
    title} or None), guess (the best guess from what needs no file read, or None)}. The file-read
    guesses were applied by the backfill / upload already."""
    snap = snapshot()
    out = []
    for row in undated_rows(include_inherited=False, snap=snap):
        if row["slug"] in snap["writeups"]:
            continue
        cid = snap["first"].get(row["slug"])
        card = snap["cards"].get(cid) if cid is not None else None
        out.append({"row": row, "card": {"slug": card["slug"], "title": card["title"]} if card else None,
                    "guess": date_guess.best(guesses_for(row, read_file=False, snap=snap))})
    return out


def listing_entry(row, read_file=False, snap=None, access=None):
    """One undated item as the MCP lists it, for the current actor. A card the actor may not see
    (#604: private, itself or above) is never named: not in `shows`, `inherits` or a guess's
    evidence. The caller filters the rows through policy.filter_visible first, so this is the second
    lock (a visible item's cards are all visible today); `access` is policy.card_access()."""
    snap = snap or snapshot()
    info = item_date(row, snap)
    cid = snap["first"].get(row["slug"])
    name_card = cid is None or policy.can_view_card(cid, access=access)
    if not name_card and info["approx"]:
        info = {**info, "card_slug": None, "card_title": None,
                "label": f"{APPROX} {range_label(info['start'], info['end'])}"}
    return {
        "slug": row["slug"], "name": item_title.title_of(row), "type": row.get("media_type") or "image",
        "filename": row.get("filename"), "imported": datefmt.day(row.get("timestamp")),
        "shows": info["label"],
        "inherits": ({"from_card": info["card_slug"], "card_title": info["card_title"],
                      "start": timeline.epoch_to_local(info["start"]).date().isoformat(),
                      "end": timeline.epoch_to_local(info["end"]).date().isoformat()} if info["approx"] else None),
        "needs_date": needs_date(row, info, snap),
        "guesses": [g.as_dict() for g in guesses_for(row, read_file=read_file, snap=snap, name_card=name_card)],
    }
