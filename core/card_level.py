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

Group kinds (family, collection) are graded by their MEMBERS (#765, #554, owner decisions
2026-10-18): `shared_checks(card, files)` is the ONE kind-aware entry point both the pips and
core/curator.py call, so the two can't disagree again. For a group card:
  cover    its own cover (picked, or borrowed from a card): that is the art its face shows;
  writeup  its own write-up passes, or >= GROUP_SHARE of its members' write-ups pass;
  dates    its own start and end are both set, or >= GROUP_SHARE of its graded units pass
           check_dates: each member, plus (a collection only) its own loose files as one unit.
A family is graded by its members only; a collection also by the files filed loose on it.
"""

from . import card_rules, db, timeline

PIP_KEYS = ("cover", "dates", "writeup", "owner_words", "stack")
WRITEUP_MIN_CHARS = 200
DATES_SHARE = 0.8
STACK_MIN_TYPES = 3
STACK_MIN_MEMBERS = 3
STACK_MIN_EVENT_FILES = 3
STACK_MIN_GROUP_FILES = 3   # #554: a group's stack pip counts its files OR its members
GROUP_SHARE = 0.8           # #765: the share of a group's members that must pass a check


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


def _member_files(member):
    """A member card's files without its write-up (the `files` check_dates takes)."""
    wu = member.get("writeup_slug")
    return [i for i in db.list_project_items(member["id"]) if i["slug"] != wu]


def _share(passed, total):
    return total > 0 and passed / total >= GROUP_SHARE


def shared_checks(card, files, members=None):
    """{cover, writeup, dates}: each {passed, reason}, the checks the pips and the Curator score
    share (#545), kind-aware (#765). `files` are the card's own items WITHOUT its write-up.
    `members` (db.list_family_members rows) is fetched for a group card when omitted."""
    kind = card.get("kind") or "project"
    cover, writeup, dates = check_cover(card), check_writeup(card), check_dates(card, files)
    if kind not in card_rules.GROUP_KINDS:
        return {"cover": cover, "writeup": writeup, "dates": dates}
    if members is None:
        members = db.list_family_members(card["id"])
    n = len(members)
    word = card_rules.kind_label(kind).lower()

    # cover: the group's own (picked, or borrowed from a card): the art its face shows. Members'
    # covers don't put art on the group's face, so they don't pass it (batch 5 lets a member's
    # cover be borrowed with a checkbox, #554).

    if not writeup["passed"]:
        ok = sum(1 for m in members if check_writeup(m)["passed"])
        passed = _share(ok, n)
        writeup = {**writeup, "passed": passed,
                   "reason": (f"{ok} of {n} members have a write-up of {WRITEUP_MIN_CHARS}+ characters"
                              f" (need {int(GROUP_SHARE * 100)}%)." if n else
                              f"{writeup['reason']} No members to grade.")}

    if not (card.get("start_date_override") is not None and card.get("end_date_override") is not None):
        units = [check_dates(m, _member_files(m))["passed"] for m in members]
        loose = kind == "collection" and bool(files)
        if loose:
            units.append(dates["passed"])
        ok = sum(1 for u in units if u)
        need = f"(need {int(GROUP_SHARE * 100)}%)"
        if not units:
            reason = f"No members to date, and no start/end dates set on the {word}."
        elif not n:  # a collection with loose files only: graded like any card
            reason = dates["reason"]
        else:
            member_ok = ok - (1 if loose and units[-1] else 0)
            reason = f"{member_ok} of {n} members are dated"
            if loose:
                reason += f"; its loose files {'are' if units[-1] else 'are not'} ({dates['reason'].rstrip('.')})"
            reason += f" {need}."
        dates = {**dates, "passed": _share(ok, len(units)), "reason": reason,
                 "units_dated": ok, "units": len(units)}
    return {"cover": cover, "writeup": writeup, "dates": dates}


def card_level(card, items=None):
    """`card` is a projects dict; `items` its file rows (db.list_project_items shape),
    fetched when omitted. Returns {score, checks: {pip: bool}, reasons: {pip: str}}."""
    if items is None:
        items = db.list_project_items(card["id"])
    kind = card.get("kind") or "project"
    writeup_slug = card.get("writeup_slug")
    files = [i for i in items if i["slug"] != writeup_slug]
    checks, reasons = {}, {}

    shared = shared_checks(card, files)
    for key in ("cover", "dates", "writeup"):
        checks[key], reasons[key] = shared[key]["passed"], shared[key]["reason"]

    doc = _writeup_doc(card)
    meta = (doc.get("type_metadata") or {}) if doc else {}

    checks["owner_words"] = bool(doc and meta.get("owner_words"))
    reasons["owner_words"] = ("The write-up is marked as the owner's own words." if checks["owner_words"]
                              else "The write-up isn't marked as the owner's own words.")

    if kind in card_rules.GROUP_KINDS:
        # #554 (owner, 2026-10-18): files OR members, so a file-bucket collection can earn it.
        n = db.count_family_members(card["id"])
        checks["stack"] = n >= STACK_MIN_MEMBERS or len(files) >= STACK_MIN_GROUP_FILES
        reasons["stack"] = (f"{n} member(s) and {len(files)} file(s) "
                            f"(need {STACK_MIN_MEMBERS} members or {STACK_MIN_GROUP_FILES} files).")
    elif kind == "event":
        checks["stack"] = len(files) >= STACK_MIN_EVENT_FILES
        reasons["stack"] = f"{len(files)} file(s) (need {STACK_MIN_EVENT_FILES})."
    else:
        types = {f.get("media_type") for f in files if f.get("media_type")}
        checks["stack"] = len(types) >= STACK_MIN_TYPES
        reasons["stack"] = f"{len(types)} kind(s) of file (need {STACK_MIN_TYPES})."

    return {"score": sum(1 for k in PIP_KEYS if checks[k]), "checks": checks, "reasons": reasons}
