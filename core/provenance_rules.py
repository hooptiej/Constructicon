"""File provenance by gross rule, proposed then confirmed (#354, "The Curator's Apprentice").

The rules themselves are pure (core/apprentice_rules.py FILE_RULES: first cut, ONE rule, "text-heavy PDFs
and manuals -> Reference"). This module is the service side: the plan as an actor sees it, the confirm
and the "not this one", and the Curator need.

  * Nothing is ever written without a yes. A proposal is a Curator need (one need per rule, kind
    `provenance_rule`, key `need:provenance_rule:<rule>`, so Defer works on it), listing up to
    apprentice_rules.PROPOSALS_SHOWN files; "Mark all shown" is the bulk-confirm.
  * A file whose provenance is already set is never proposed (the rule's loader), and confirm re-checks
    it: a file that got a provenance meanwhile is skipped and named, never overwritten.
  * "Not this one" is remembered per (rule, file) in curator_dismissals (key
    `need:provenance_rule_skip:<rule>:<slug>`, action dismiss; one imaged change-log row, undoable).
  * Confirm writes the provenance of every confirmed file as ONE imaged change-log row (op
    provenance_rule_apply), one batch: one undo takes them all back.
  * A file the actor may not see is left out of their plan and only counted; naming one in a write is
    refused with #709's 403 saying why (policy.viewable_rows).

  plan(actor=None, rule=None)                  read-only: {summary, rules: [{rule, proposals, ...}]}
  confirm(slugs=None, *, rule, shown, dry_run) editor: write the rule's provenance
  dismiss(slugs, *, rule, dry_run)             editor: "not this one", remembered
  queue_items()                                the Curator queue's needs (one per rule with proposals)

Measure first on a read-only copy of prod: scripts/measure_apprentice_354_344.py.
"""

import time

from . import actor as actor_ctx
from . import apprentice_rules as ar
from . import changes, db, item_title, items, policy, provenance_options, roles
from .cards import Result
from .errors import AppError, Conflict, InvalidInput

NEED_KIND = "provenance_rule"
OP_APPLY = "provenance_rule_apply"
OP_NOT_THIS = "provenance_rule_not_this"
DEFAULT_RULE = ar.FILE_RULES[0].key


def _rule(key):
    key = key or DEFAULT_RULE
    rule = ar.RULES_BY_KEY.get(key)
    if rule is None:
        raise InvalidInput(f"Unknown rule {key!r}. Rules: {', '.join(ar.RULES_BY_KEY)}.", code="bad_rule",
                           details={"rules": list(ar.RULES_BY_KEY)})
    return rule


def _target_problem(rule):
    """Why the rule's provenance can't be written now (the option is missing or retired), or None."""
    opt = provenance_options.get_option("file", rule.provenance)
    if opt is None:
        return f"the file provenance {rule.provenance!r} no longer exists (Admin > Provenance options)"
    if opt.get("retired"):
        return f"the file provenance {opt.get('label') or rule.provenance!r} is retired (Admin > Provenance options)"
    return None


def _label(rule):
    opt = provenance_options.get_option("file", rule.provenance) or {}
    return opt.get("label") or rule.provenance.title()


def _shape(p):
    return {"slug": p["slug"], "title": item_title.title_of(p), "filename": p.get("filename"),
            "media_type": p.get("media_type"), "chars": p.get("chars"), "reason": p["reason"],
            "evidence": p["evidence"], "thumb_url": f"/f/{p['slug']}/thumb", "url": f"/object/{p['slug']}"}


def _rule_plan(rule, who):
    conn = db.get_conn()
    try:
        proposals, counts = ar.propose_files(conn, rule)
    finally:
        conn.close()
    visible, hidden = [], 0
    rows = {r["slug"]: r for r in (db.get_by_slug(p["slug"]) for p in proposals) if r}
    for p in proposals:
        row = rows.get(p["slug"])
        if row is None:
            continue
        if not policy.can_view(row, who):
            hidden += 1  # counted, never named
            continue
        visible.append(_shape({**row, **p}))
    problem = _target_problem(rule)
    return {"rule": rule.public(), "provenance_label": _label(rule), "proposals": visible,
            "blocked": problem,
            "summary": {**counts, "proposed": len(visible), "hidden": hidden}}


def plan(actor=None, rule=None):
    """Read-only. {summary: {proposed, hidden, rules}, rules: [{rule: {key, provenance, label, question,
    media_types, explain}, provenance_label, blocked (why it can't apply now, or None), proposals: [{slug,
    title, filename, media_type, chars, reason, evidence, thumb_url, url}], summary: {considered, matched,
    answered_not_this, proposed, hidden}}]}. A file the actor can't see is left out and counted."""
    who = actor_ctx.resolve(actor)
    rules = [_rule(rule)] if rule else ar.FILE_RULES
    out = [_rule_plan(r, who) for r in rules]
    return {"summary": {"proposed": sum(r["summary"]["proposed"] for r in out),
                        "hidden": sum(r["summary"]["hidden"] for r in out), "rules": len(out)},
            "rules": out}


def _require_editor(who, what):
    if not roles.at_least(roles.role_of(who), roles.EDITOR):
        raise AppError("forbidden", f"{what} needs the editor role (editors and admins), and {who} has "
                       f"{roles.role_of(who)}. Ask an admin to change your role. Nothing was changed.",
                       status=403, details={"need": roles.EDITOR, "have": roles.role_of(who)})


def _names(slugs):
    return list(dict.fromkeys(s for s in (slugs or []) if s and isinstance(s, str)))


def confirm(slugs=None, *, rule=None, shown=None, dry_run=False, actor=None, batch_id=None):
    """Writes the rule's provenance (Reference for the first rule) on the confirmed files, ONE imaged row
    (op provenance_rule_apply), one batch, one undo. `slugs` = explicit picks: each must be a current
    proposal of the rule, else 409 not_proposed and nothing is written. `shown` = the files on screen
    (the Curator's buttons): those still proposed are written, the rest are skipped and named (e.g. one
    got a provenance meanwhile). Neither = every current proposal the actor can see (bulk). Editor.
    Refusals: 403 forbidden (role; a named file the actor can't see), 404 not_found (unknown slug),
    400 bad_request / bad_rule, 409 not_proposed, 409 rule_blocked (the target option is retired),
    409 nothing_to_confirm. data: {rule, provenance, applied: [slugs], skipped: [{slug, reason}]}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Confirming a provenance proposal")
    r = _rule(rule)
    named, seen = _names(slugs), _names(shown)
    if named and seen:
        raise InvalidInput("Give slugs (the files to confirm) or shown (the files on screen), not both.")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        if named or seen:
            policy.viewable_rows(named + seen, actor=who)  # 404 unknown, 403 hidden, saying why (#709)
        planned = _rule_plan(r, who)
        if planned["blocked"]:
            raise Conflict(f"Nothing was changed: {planned['blocked']}. Bring it back, or answer these by hand.",
                           code="rule_blocked")
        open_now = {p["slug"] for p in planned["proposals"]}
        if named:
            stray = [s for s in named if s not in open_now]
            if stray:
                raise Conflict(f"Nothing was changed: {', '.join(stray)} {'is' if len(stray) == 1 else 'are'} not a "
                               f"current “{r.label}” proposal (its provenance may be set already, or you "
                               "said not this one). List the proposals again.", code="not_proposed",
                               details={"not_proposed": stray})
            todo, skipped = named, []
        elif seen:
            todo = [s for s in seen if s in open_now]
            skipped = [{"slug": s, "reason": "no longer proposed (its provenance was set, or you said not this one)"}
                       for s in seen if s not in open_now]
        else:
            todo, skipped = [p["slug"] for p in planned["proposals"]], []
        if not todo:
            raise Conflict("Nothing was changed: none of these files is still waiting for this answer.",
                           code="nothing_to_confirm", details={"skipped": skipped})
        value = provenance_options.validate("file", r.provenance)
        with db.ImageLog(OP_APPLY, who, batch_id, todo) as log:
            for slug in todo:
                cur = log.get("capture_events", {"slug": slug})
                if cur is None or cur.get("provenance"):
                    skipped.append({"slug": slug, "reason": "its provenance was set meanwhile; left as it is"})
                    continue
                log.update("capture_events", {"slug": slug}, {"provenance": value})
            muts = list(log.muts)
    applied = [m["key"]["slug"] for m in muts]
    return Result(True, items._flat(OP_APPLY, muts), [f"skipped {x['slug']}: {x['reason']}" for x in skipped],
                  batch_id, dry_run, {"rule": r.key, "provenance": value, "applied": applied, "skipped": skipped})


def dismiss(slugs, *, rule=None, dry_run=False, actor=None, batch_id=None):
    """"Not this one": remembers each named file that is a current proposal of the rule, so the rule never
    proposes it again (curator_dismissals, key need:provenance_rule_skip:<rule>:<slug>). ONE imaged row
    (op provenance_rule_not_this), undoable. Editor. Refusals: 403 forbidden, 404 not_found, 400
    bad_request (no files), 409 not_proposed (none of them is a current proposal). data: {rule, slugs}."""
    who = actor_ctx.resolve(actor)
    _require_editor(who, "Answering a provenance proposal")
    r = _rule(rule)
    named = _names(slugs)
    if not named:
        raise InvalidInput("Name at least one file (slugs=[...]) to say it is not this.")
    batch_id = batch_id or changes.new_batch_id()
    with db.transaction(dry_run=dry_run):
        policy.viewable_rows(named, actor=who)
        open_now = {p["slug"] for p in _rule_plan(r, who)["proposals"]}
        todo = [s for s in named if s in open_now]
        if not todo:
            raise Conflict("Nothing was changed: none of these files is a current proposal (already answered, or "
                           "its provenance is set). List the proposals again.", code="not_proposed",
                           details={"not_proposed": named})
        with db.ImageLog(OP_NOT_THIS, who, batch_id, todo) as log:
            for slug in todo:
                key = {"nudge_key": ar.provenance_skip_key(r.key, slug)}
                if log.get("curator_dismissals", key) is None:
                    log.insert("curator_dismissals", key, {"action": db.CURATOR_DISMISS, "snooze_until": None,
                                                           "created_at": time.time()})
            muts = list(log.muts)
    return Result(True, items._flat(OP_NOT_THIS, muts), [], batch_id, dry_run, {"rule": r.key, "slugs": todo})


# --- the Curator queue -------------------------------------------------------------------

def need_key(rule_key):
    return f"need:{NEED_KIND}:{rule_key}"


def queue_items():
    """One Curator need per rule with proposals (built as `system`; for_actor drops the files the asker
    can't see through `members`, and the whole need when none are left)."""
    out = []
    for rp in plan(actor_ctx.resolve(None))["rules"]:
        props = rp["proposals"]
        if not props:
            continue
        shown = props[:ar.PROPOSALS_SHOWN]
        label = rp["provenance_label"]
        detail = (f"Rule: {rp['rule']['explain']} Nothing changes until you say yes; one undo takes a whole "
                  f"answer back.")
        if len(props) > len(shown):
            detail += f" Showing {len(shown)} of {len(props)}; the rest come up as you answer these."
        if rp["blocked"]:
            detail += f" Can't apply right now: {rp['blocked']}."
        out.append({
            "type": "need", "key": need_key(rp["rule"]["key"]), "id": None, "kind": NEED_KIND,
            "label": rp["rule"]["question"], "detail": detail,
            "suggested": None, "confidence": None, "group": None, "href": "/curator", "fix_label": "Open",
            "deferred": False, "dismissible": False,
            "proposal": {"rule": rp["rule"]["key"], "provenance": rp["rule"]["provenance"], "provenance_label": label,
                         "blocked": rp["blocked"], "total": len(props)},
            "members": shown,
        })
    return out
