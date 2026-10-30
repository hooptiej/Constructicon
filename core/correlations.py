"""Project correlations: "these projects look related: link them?" (#344, trimmed; "The Curator's Apprentice").

The scoring is pure (core/apprentice_rules.py propose_pairs: text similarity of the cards' embedding
centroids, shared tags, time overlap, shared files; see its docstring for the weights and the
threshold). This module is the service side.

  * Nothing is linked without a yes. The proposals are ONE Curator need (kind `correlation`, key
    `need:correlation:global`, so Defer works), listing the best apprentice_rules.PROPOSALS_SHOWN pairs;
    "Link all shown" is the bulk-confirm.
  * Confirm adds a `related` link per pair through the existing card op (cards.link, op
    correlation_link), every pair in ONE batch: one undo removes them all.
  * "Not related" is remembered per pair in curator_dismissals (key `need:correlation_pair:<a>:<b>`,
    action dismiss; one imaged change-log row, undoable).
  * Skipped: pairs already linked (any type), group kinds, nesting, shared parents and families (see
    apprentice_rules), and, per actor, any pair with a card they can't see (private cards, #604 step 3:
    left out and only counted). Hobbies are not cards, so they never appear.

  plan(actor=None)                       read-only: {summary, pairs}
  confirm(pairs=None, *, shown, dry_run) editor: link them (`related`)
  dismiss(pairs, *, dry_run)             editor: "not related", remembered per pair
  queue_items()                          the Curator queue's need

Measure first on a read-only copy of prod: scripts/measure_apprentice_354_344.py.
"""

import time

from . import actor as actor_ctx
from . import apprentice_rules as ar
from . import cards, changes, db, items, policy, roles
from .cards import Result
from .errors import AppError, Conflict, InvalidInput

NEED_KIND = "correlation"
NEED_KEY = f"need:{NEED_KIND}:global"
OP_LINK = "correlation_link"
OP_NOT_RELATED = "correlation_not_related"
LABEL = "These projects look related: link them?"


def _scan():
    conn = db.get_conn()
    try:
        data = ar.load_correlation_inputs(conn)
    finally:
        conn.close()
    return ar.propose_pairs(data)


def _plan(who):
    pairs, counts = _scan()
    visible, hidden = [], 0
    with policy.access_scope(who) as acc:
        for p in pairs:
            if acc.empty or (policy.can_view_card(p["a_id"], access=acc) and policy.can_view_card(p["b_id"], access=acc)):
                visible.append({**p, "key": ar.correlation_skip_key(p["a"], p["b"]),
                                "a_url": f"/project/{p['a']}", "b_url": f"/project/{p['b']}"})
            else:
                hidden += 1  # counted, never named: one of the two cards is private to someone else
    return visible, {**counts, "proposed": len(visible), "hidden_pairs": hidden}


def plan(actor=None):
    """Read-only. {summary: {cards_considered, with_embedding, pairs_scored, excluded: {already_linked,
    nested, same_parent, same_family}, answered_not_related, below_threshold, too_few_signals, min_signals,
    text_required, proposed, hidden_pairs,
    threshold, weights}, pairs: [{a, b, a_title, b_title, score, parts: {text, tags, time, shared},
    evidence: {cosine, shared_tags, overlap, shared_files}, why, a_url, b_url}]} best first."""
    who = actor_ctx.resolve(actor)
    pairs, summary = _plan(who)
    summary["weights"] = {"text": ar.W_TEXT, "tags": ar.W_TAGS, "time": ar.W_TIME, "shared": ar.W_SHARED}
    return {"summary": summary,
            "pairs": [{k: v for k, v in p.items() if k not in ("a_id", "b_id", "key")} for p in pairs]}


def _require_editor(who, what):
    if not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise AppError("forbidden", f"{what} needs the editor role (editors and admins), and {who} has "
                       f"{roles.role_of(who)}. Ask an admin to change your role. Nothing was changed.",
                       status=403, details={"need": roles.EDITOR, "have": roles.role_of(who)})


def _pairs_arg(pairs, name="pairs"):
    """[[a, b], ...] or ["a:b", ...] -> sorted unique (a, b) tuples. 400 on anything else."""
    out = []
    for p in pairs or []:
        if isinstance(p, str):
            a, sep, b = p.partition(":")
            if not sep:
                a, sep, b = p.partition(",")
        elif isinstance(p, (list, tuple)) and len(p) == 2:
            a, b = p
        else:
            a = b = None
        if not a or not b or not isinstance(a, str) or not isinstance(b, str) or a == b:
            raise InvalidInput(f"Each of {name} is two different card slugs, [a, b] or \"a:b\"; got {p!r}.")
        out.append(ar.pair(a.strip(), b.strip()))
    return list(dict.fromkeys(out))


def _resolve(named, who):
    """Card slugs or ids -> slugs, refusing an unknown or hidden card (cards.get_card: not_found)."""
    out = []
    with actor_ctx.acting_as(who):
        for a, b in named:
            out.append(ar.pair(cards.get_card(a)["slug"], cards.get_card(b)["slug"]))
    return list(dict.fromkeys(out))


def confirm(pairs=None, *, shown=None, dry_run=False, actor=None, batch_id=None):
    """Links each confirmed pair `related` (cards.link, op correlation_link), ALL in one batch: one undo
    unlinks them all. `pairs` = explicit picks, each a current proposal (else 409 not_proposed, nothing
    written); `shown` = the pairs on screen (the Curator's buttons): those still proposed are linked, the
    rest skipped and named; neither = every current proposal the actor can see. Editor. Refusals: 403
    forbidden, 404 not_found (unknown or hidden card), 400 bad_request, 409 not_proposed, 409
    nothing_to_confirm. data: {linked: [[a, b]...], skipped: [{pair, reason}]}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Linking suggested related projects")
    named, seen = _pairs_arg(pairs), _pairs_arg(shown, "shown")
    if named and seen:
        raise InvalidInput("Give pairs (the ones to link) or shown (the ones on screen), not both.")
    named, seen = _resolve(named, who), _resolve(seen, who)
    batch_id = batch_id or changes.new_batch_id()
    rows, warnings = [], []
    with db.transaction(dry_run=dry_run):
        current, _summary = _plan(who)
        open_now = {(p["a"], p["b"]): p for p in current}
        skipped = []
        if named:
            stray = [p for p in named if p not in open_now]
            if stray:
                raise Conflict("Nothing was changed: " + "; ".join(f"{a} and {b}" for a, b in stray)
                               + (" is not a current suggestion" if len(stray) == 1 else " are not current suggestions")
                               + " (already linked, answered “not related”, or no longer alike). List them again.",
                               code="not_proposed", details={"not_proposed": [list(p) for p in stray]})
            todo = named
        elif seen:
            todo = [p for p in seen if p in open_now]
            skipped = [{"pair": list(p), "reason": "no longer suggested (linked or answered meanwhile)"}
                       for p in seen if p not in open_now]
        else:
            todo = list(open_now)
        if not todo:
            raise Conflict("Nothing was changed: none of these pairs is still waiting for an answer.",
                           code="nothing_to_confirm", details={"skipped": skipped})
        for a, b in todo:
            p = open_now[(a, b)]
            res = cards.link(a, b, "related", note=f"Suggested by the Curator (score {p['score']:.2f})",
                             actor=who, batch_id=batch_id, _op=OP_LINK)
            rows += res.changes
            warnings += res.warnings
    return Result(True, rows, warnings + [f"skipped {x['pair'][0]} / {x['pair'][1]}: {x['reason']}" for x in skipped],
                  batch_id, dry_run, {"linked": [list(p) for p in todo], "skipped": skipped})


def dismiss(pairs, *, dry_run=False, actor=None, batch_id=None):
    """"Not related": remembers each named pair that is a current suggestion, so it is never suggested
    again. ONE imaged row (op correlation_not_related), undoable. Editor. Refusals: 403 forbidden, 404
    not_found, 400 bad_request (no pairs), 409 not_proposed (none of them is a current suggestion).
    data: {pairs: [[a, b]...]}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Answering a related-projects suggestion")
    named = _pairs_arg(pairs)
    if not named:
        raise InvalidInput("Name at least one pair of cards (pairs=[[a, b]]) to say they are not related.")
    named = _resolve(named, who)
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        current, _summary = _plan(who)
        open_now = {(p["a"], p["b"]) for p in current}
        todo = [p for p in named if p in open_now]
        if not todo:
            raise Conflict("Nothing was changed: none of these pairs is a current suggestion (already answered or "
                           "linked). List them again.", code="not_proposed",
                           details={"not_proposed": [list(p) for p in named]})
        with db.ImageLog(OP_NOT_RELATED, who, batch_id, sorted({s for p in todo for s in p})) as log:
            for a, b in todo:
                key = {"nudge_key": ar.correlation_skip_key(a, b)}
                if log.get("curator_dismissals", key) is None:
                    log.insert("curator_dismissals", key, {"action": db.CURATOR_DISMISS, "snooze_until": None,
                                                           "created_at": time.time()})
            muts = list(log.muts)
    return Result(True, items._flat(OP_NOT_RELATED, muts), [], batch_id, dry_run, {"pairs": [list(p) for p in todo]})


# --- the Curator queue -------------------------------------------------------------------

def queue_items():
    """The one correlation need (built as `system`; for_actor drops the pairs with a card the asker
    can't see through `member_pairs`, and the need when none are left)."""
    pairs, summary = _plan(actor_ctx.resolve(None))
    if not pairs:
        return []
    shown = pairs[:ar.PROPOSALS_SHOWN]
    detail = ("Scored from how alike their files read, shared tags, overlapping dates and shared files "
              f"(suggested at {ar.SCORE_THRESHOLD:.2f} of 1 or more, with at least {ar.MIN_SIGNALS} of those signals, "
              "alike text one of them). Linking adds a “related” link; "
              "one undo takes a whole answer back.")
    if len(pairs) > len(shown):
        detail += f" Showing {len(shown)} of {len(pairs)}; the rest come up as you answer these."
    return [{
        "type": "need", "key": NEED_KEY, "id": None, "kind": NEED_KIND, "label": LABEL, "detail": detail,
        "suggested": None, "confidence": None, "group": None, "href": "/curator", "fix_label": "Open",
        "deferred": False, "dismissible": False, "total": len(pairs),
        "member_pairs": [{k: v for k, v in p.items() if k not in ("a_id", "b_id")} for p in shown],
    }]
