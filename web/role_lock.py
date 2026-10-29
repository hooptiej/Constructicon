"""Role-locked controls: the ONE way a page shows a feature the user's role can't use (#779).

Owner rule (2026-10-18): "a permission never makes a feature invisible." A control the user's role
can't use is still drawn, greyed (CSS `.role-locked`: greyscale, never translucent), labelled with
the role it needs, and a click explains instead of doing anything (static/js/role-lock.js, loaded on
every page). Hiding is only for private CONTENT (items and cards the user may not see), never for a
feature. The routes' own role checks stay the real guard; this only decides what is SHOWN.

Jinja globals (web/common.py):
  role_locked(need)              True when the current actor is below `need` (draw it locked)
  role_lock_attrs(need, label)   the attributes a locked control carries: aria-disabled, the
                                 `data-locked-role` / `data-locked-message` role-lock.js reads, and a
                                 title "Label (editors and admins)". Put `role-locked` in its class.
  role_lock_message(label, need) the sentence a click shows
  role_need_phrase(need)         "admins only" / "editors and admins"
  role_lock_class(need)          " role-locked" or "" (inside a class attribute)
  role_lock_if(need, label)      role_lock_attrs(...) when locked, else nothing

Typical use in a template (one control, both states):
  <button type="button" class="dp-btn{{ role_lock_class('editor') }}"{{ role_lock_if('editor', 'Delete') }}>Delete</button>
A locked control must not carry the id / class a page script wires its action to (or that script
must skip `.role-locked`); role-lock.js also stops the click in the capture phase, before any other
listener.

JS-built controls (the file grid's bulk bar) get the same strings from the server (a page config
carries `locked: {need, message}`) and call `RoleLock.decorate(el, need, message)`.
"""

from markupsafe import Markup, escape

from core import actor as actor_ctx, roles

NEED_PHRASE = {roles.ADMIN: "admins only", roles.EDITOR: "editors and admins", roles.VIEWER: "anyone signed in"}


def current_role():
    return roles.role_of(actor_ctx.current_actor())


def locked(need):
    """True when the current actor's role is below `need`."""
    return not roles.at_least(current_role(), need)


def need_phrase(need):
    return NEED_PHRASE.get(need, f"{need}s and above")


def article(word):
    return "an" if word[:1].lower() in "aeiou" else "a"


def message(label, need, have=None, note=None):
    """What a click on a locked control says: what it is, the role it needs, the user's own role and
    who to ask (`note`, optional: one more sentence, e.g. who else may). The nav rail's locked tabs
    (#779, PR #776) say exactly this too."""
    have = current_role() if have is None else have
    head = f"{label} needs the {need} role ({need_phrase(need)})." + (f" {note}" if note else "")
    if have == roles.PUBLIC:
        return f"{head} You're not signed in: sign in with an account that has it."
    return f"{head} Your account is {article(have)} {have}. Ask an admin to change your role if you need it."


def attrs(need, label, note=None, title=True):
    """The attributes of a locked control (a leading space, Markup-safe). title=False for a control
    that already carries its own title attribute (no duplicate attribute)."""
    msg = message(label, need, note=note)
    out = f' aria-disabled="true" data-locked-role="{escape(need)}" data-locked-message="{escape(msg)}"'
    if title:
        out += f' title="{escape(label)} ({escape(need_phrase(need))})"'
    return Markup(out)


def lock_class(need):
    """' role-locked' when the actor is below `need`, else '' (for a class attribute)."""
    return " role-locked" if locked(need) else ""


def lock_if(need, label, note=None, title=True):
    """`attrs(need, label)` when the actor is below `need`, else nothing: one control, both states."""
    return attrs(need, label, note, title) if locked(need) else Markup("")


def config(need, label):
    """For a JS-built control: {need, message, title}, or None when the actor holds the role."""
    if not locked(need):
        return None
    return {"need": need, "message": message(label, need), "title": f"{label} ({need_phrase(need)})"}
