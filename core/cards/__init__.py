"""V2 card operations (docs/design/v2-cards.md section 6).

Every function takes `card` as a project id or slug, runs the validators in
core/card_rules, writes inside one transaction (via core/db.py helpers, which
own all SQL), records a change-log row (core/changes), and returns a Result.
All accept `dry_run` (compute and return `changes`, write nothing) and `actor`
('owner-ui' | 'mcp'). The web routes and the MCP tools are thin wrappers over
these; no rule lives anywhere else.

Piece 1 scope: set_status, set_kind, resolve_decision (for the card_* decision
kinds), list_needs_decision (stored decisions only), plus the helpers the
Curator and the pages use to read a card's live status.

Piece 3 adds families/collections (add_to_family, remove_from_family), "part of"
nesting (nest, unnest) and the card_family_members decision resolution.

#547 step 2 ("Four-Thousand-Line Club"): this used to be one 3,000-line core/cards.py. It is now a
package, moved verbatim, with no behaviour change. Callers do not change: `from core import cards`
then `cards.<name>` (public or `_private`), and `from core.cards import <name>` (`Result`,
`_changes_from_log`, ...), reach every name the old module had. The submodules, in the order they load:

    base       the card decision kinds and patch ops, Result, card_decision_slug, get_card, _change_rows,
               _status_warnings, _changes_from_log (what every other submodule shares)
    text       face text (#596): set_text, the write-up lead cache, face_text, text_fields
    ops        one-card setters: set_status, set_kind, set_whereabouts, set_provenance, set_highlight,
               suggest_provenance, whereabouts_fields
    families   group spans (#772), nest / unnest, add_to_family / remove_from_family, family_fields
    links      typed links: link, unlink, retype_link(s), list_links
    hobbies    get_hobby, hobby_flags / hobby_fields, set_hobby_activity, set_group_code
    decisions  a card's live status (open_card_decisions, status_fields), resolve_decision, decision_summary
    needs      every computed need (NEED_*) and list_needs_decision, which merges them with the stored ones
    reshape    piece 6: home (set_home, resolve_home, home_chain), hobby membership, move / copy files,
               split_card, merge_cards, delete_card
    edit       create / update, cover picks (set_cover, lend_problem, ...), set_agent_notes, the blank
               write-up a card leaves behind
    explain    explain_card
    faces      card_face, hobby_card_face, file_face, project_pile, file_stacks, the hobby cover
    batch      bulk, resolve_decisions (the module is not called `bulk`: that name is the function)
    history    undo, list_changes (not called `undo`, for the same reason)

Each submodule imports (`from .base import get_card`, ...) only from the submodules above it in
this list, so the imports form a chain with no cycle; keep it that way (a function a lower module
needs moves down, it is not imported upward).

Two rules keep the package behaving like the one module it was (the same two as core/db, #547 step 1):

* One copy of every name. Each name is defined in exactly one submodule; the others import that
  same object. This file copies every submodule's names onto the package at import (by walking the
  submodules, not by listing names, so a name here is never mistaken for a use of it by
  scripts/check_dead_code.py), and refuses to load if two submodules bind one name to different objects.
* Assigning to the package reaches the submodules. A test that sets `cards.update = slow_update`
  expects the code inside cards (set_cover calls update) to see it, as it did when that code read its
  own module globals. `cards.<name> = value` therefore also rebinds `<name>` in every submodule that
  held the same object. Patch through `core.cards`, never through a submodule
  (`core.cards.edit.update = ...` reaches only that one file).

scripts/test_cards_package_547.py checks both, against the old module's names
(scripts/data/cards_names_547.txt).
"""

import sys as _sys
import types as _types

from . import (base, text, ops, families, links, hobbies, decisions, needs, reshape, edit, explain,  # noqa: F401
               faces, batch, history)

_SUBMODULES = (base, text, ops, families, links, hobbies, decisions, needs, reshape, edit, explain, faces, batch,
               history)


def _export():
    owner = {}
    for mod in _SUBMODULES:
        for name, value in vars(mod).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in owner and owner[name][1] is not value:
                raise ImportError(f"core.cards: {name!r} is bound to different objects in {owner[name][0]} and "
                                  f"{mod.__name__}; each name must live in one submodule (#547)")
            owner.setdefault(name, (mod.__name__, value))
    clash = sorted(n for n in owner if n in {m.__name__.rsplit(".", 1)[1] for m in _SUBMODULES})
    if clash:
        raise ImportError(f"core.cards: submodule name(s) {clash} shadow a cards name; rename the submodule (#547)")
    g = globals()
    for name, (_mod, value) in owner.items():
        g[name] = value


class _Package(_types.ModuleType):
    """core.cards' module type: `cards.X = v` also rebinds X in each submodule that held the old object."""

    _submodules = _SUBMODULES

    def __setattr__(self, name, value):
        missing = object()
        old = self.__dict__.get(name, missing)
        if old is not missing and not (name.startswith("__") and name.endswith("__")):
            for mod in type(self)._submodules:
                if vars(mod).get(name, missing) is old:
                    setattr(mod, name, value)
        super().__setattr__(name, value)


_export()
_sys.modules[__name__].__class__ = _Package
del _sys, _types, _SUBMODULES, _export, _Package
