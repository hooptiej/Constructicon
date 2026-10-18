"""Curation level ("pips", 0-5) for a card (docs/design/v2-cards.md 3.11), and the
ONE definition of "done" for the checks it shares with the Curator score (#545).

Computed per request, never stored. One point each for: a cover, dates, a real
write-up, the owner's own words in it, and a full stack of files.

The Curator score (core/curator.py) grades more things (captions, tags, links, a
timeline gap...), but for the checks both systems make it calls the functions here,
so a card can't pass a check on its pips and fail it on its Curator score:
  check_cover    a cover resolves (picked, or the automatic one);
  check_writeup  the write-up has >= WRITEUP_MIN_CHARS characters (spaces not counted);
  check_dates    >= DATES_SHARE of the card's files have a REAL date
                 (timeline.has_real_date: never the upload time), or both the card's
                 start and end dates are set.
Owner decisions on #545 (2026-10-17): 200 characters and 80% win.
"""

from . import card_rules, db, timeline

PIP_KEYS = ("cover", "dates", "writeup", "owner_words", "stack")
WRITEUP_MIN_CHARS = 200
DATES_SHARE = 0.8
STACK_MIN_TYPES = 3
STACK_MIN_MEMBERS = 3
STACK_MIN_EVENT_FILES = 3


def _writeup_doc(card):
    slug = card.get("writeup_slug")
    return db.get_by_slug(slug) if slug else None


def check_cover(card):
    """{passed, reason}: the card has a cover (picked, or the automatic one)."""
    cover = db.resolve_project_cover_slug(card)
    return {"passed": cover is not None, "reason": "Has a cover." if cover else "No cover picked."}


def check_dates(card, files):
    """{passed, reason, dated, total}. `files` are the card's items WITHOUT its write-up."""
    both = card.get("start_date_override") is not None and card.get("end_date_override") is not None
    dated = [f for f in files if timeline.has_real_date(f)]
    share_ok = bool(files) and len(dated) / len(files) >= DATES_SHARE
    if both:
        reason = "Start and end dates are set."
    elif files:
        reason = f"{len(dated)} of {len(files)} files have real dates (need {int(DATES_SHARE * 100)}%)."
    else:
        reason = "No files to date, and no start/end dates set."
    return {"passed": bool(both or share_ok), "reason": reason, "dated": len(dated), "total": len(files)}


def check_writeup(card, doc=None):
    """{passed, reason, chars, doc}: the write-up has at least WRITEUP_MIN_CHARS characters,
    spaces and line breaks not counted. `doc` is the write-up document, fetched when omitted."""
    if doc is None:
        doc = _writeup_doc(card)
    meta = (doc.get("type_metadata") or {}) if doc else {}
    body = (meta.get("body") or "")
    chars = len("".join(body.split()))
    passed = chars >= WRITEUP_MIN_CHARS
    reason = (f"Write-up has {chars} characters." if doc
              else "No write-up.") + ("" if passed else f" Needs {WRITEUP_MIN_CHARS}.")
    return {"passed": passed, "reason": reason, "chars": chars, "doc": doc}


def card_level(card, items=None):
    """`card` is a projects dict; `items` its file rows (db.list_project_items shape),
    fetched when omitted. Returns {score, checks: {pip: bool}, reasons: {pip: str}}."""
    if items is None:
        items = db.list_project_items(card["id"])
    kind = card.get("kind") or "project"
    writeup_slug = card.get("writeup_slug")
    files = [i for i in items if i["slug"] != writeup_slug]
    checks, reasons = {}, {}

    for key, res in (("cover", check_cover(card)), ("dates", check_dates(card, files))):
        checks[key], reasons[key] = res["passed"], res["reason"]

    wu = check_writeup(card)
    doc = wu["doc"]
    meta = (doc.get("type_metadata") or {}) if doc else {}
    checks["writeup"], reasons["writeup"] = wu["passed"], wu["reason"]

    checks["owner_words"] = bool(doc and meta.get("owner_words"))
    reasons["owner_words"] = ("The write-up is marked as the owner's own words." if checks["owner_words"]
                              else "The write-up isn't marked as the owner's own words.")

    if kind in card_rules.GROUP_KINDS:
        n = db.count_family_members(card["id"])
        checks["stack"] = n >= STACK_MIN_MEMBERS
        reasons["stack"] = f"{n} member(s) (need {STACK_MIN_MEMBERS})."
    elif kind == "event":
        checks["stack"] = len(files) >= STACK_MIN_EVENT_FILES
        reasons["stack"] = f"{len(files)} file(s) (need {STACK_MIN_EVENT_FILES})."
    else:
        types = {f.get("media_type") for f in files if f.get("media_type")}
        checks["stack"] = len(types) >= STACK_MIN_TYPES
        reasons["stack"] = f"{len(types)} kind(s) of file (need {STACK_MIN_TYPES})."

    return {"score": sum(1 for k in PIP_KEYS if checks[k]), "checks": checks, "reasons": reasons}
