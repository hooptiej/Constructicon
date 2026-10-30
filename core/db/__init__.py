"""SQLite index for Constructicon. One row per upload.

#547 step 1 ("Four-Thousand-Line Club"): this used to be one 6,700-line core/db.py. It is now a
package, moved verbatim, with no behaviour change. Callers do not change: `from core import db`
then `db.<name>` (public or `_private`), and `from core.db import <name>`, reach every name the old
module had. The submodules, in the order they load:

    conn          DB_PATH, get_conn, transaction(), read_session(), the ambient shared connection,
                  and the shared helpers _row_to_dict / _table_exists
    labels        Source labels and channels, HOBBY_STATUSES
    settings      the settings table
    changelog     the change log, row images (ImageLog), invert_image, write_images
    audit         the request audit log and the sensitive-item access log
    revisions     revision chains (#477) and the look-alike image candidates (#759)
    tags          the tag tree and the tag-chip cache (#555)
    search_index  the FTS5 search index (#543) and search()
    items         item rows: insert, read, fields, redaction, trash
    pipeline      caption queue, OCR status, hashes/embeddings, extracted text, caption worklists
    projects      cards: create, read, tree, files on cards, privacy reads, family members
    relations     item-to-item relations and card-to-card links (#408)
    decisions     pending decisions (#240) and the Curator queue state (#519)
    blog          blog entries
    hobbies       hobbies (#360)
    dates         the reads behind approximate item dates (#725), the first upload
    users         users, sessions and per-user MCP tokens
    unpack        Unpack the Crate job rows (#605)
    migrations    the run-once data migrations (#549) and their runner
    schema        SCHEMA, init_db, the every-boot steps (#667, #704, #760), table helpers

Each submodule imports (`from .conn import get_conn`, ...) only from the submodules above it in
this list, so the imports form a chain with no cycle; keep it that way (a function a lower module
needs moves down, it is not imported upward).

Two rules keep the package behaving like the one module it was:

* One copy of every name. Each name is defined in exactly one submodule; the others import that
  same object (one `_ambient`, one cache, one lock). This file copies every submodule's names onto
  the package at import (by walking the submodules, not by listing names, so a name here is never
  mistaken for a use of it by scripts/check_dead_code.py), and refuses to load if two submodules
  bind one name to different objects.
* Assigning to the package reaches the submodules. Tests set `db.DB_PATH = ...` and monkeypatch
  `db.get_conn`, `db._ensure_chip_triggers` and friends, and expect code inside db to see it, as it
  did when that code read its own module globals. `db.<name> = value` therefore also rebinds
  `<name>` in every submodule that held the same object. Patch through `core.db`, never through a
  submodule (`core.db.conn.DB_PATH = ...` reaches only that one file).

scripts/test_db_package_547.py checks both, against the old module's names
(scripts/data/db_names_547.txt).
"""

import sys as _sys
import types as _types

from . import (conn, labels, settings, changelog, audit, revisions, tags, search_index, items,  # noqa: F401
               pipeline, projects, relations, decisions, blog, hobbies, dates, users, unpack, migrations, schema)

_SUBMODULES = (conn, labels, settings, changelog, audit, revisions, tags, search_index, items, pipeline, projects,
               relations, decisions, blog, hobbies, dates, users, unpack, migrations, schema)


def _export():
    owner = {}
    for mod in _SUBMODULES:
        for name, value in vars(mod).items():
            if name.startswith("__") and name.endswith("__"):
                continue
            if name in owner and owner[name][1] is not value:
                raise ImportError(f"core.db: {name!r} is bound to different objects in {owner[name][0]} and "
                                  f"{mod.__name__}; each name must live in one submodule (#547)")
            owner.setdefault(name, (mod.__name__, value))
    clash = sorted(n for n in owner if n in {m.__name__.rsplit(".", 1)[1] for m in _SUBMODULES})
    if clash:
        raise ImportError(f"core.db: submodule name(s) {clash} shadow a db name; rename the submodule (#547)")
    g = globals()
    for name, (_mod, value) in owner.items():
        g[name] = value


class _Package(_types.ModuleType):
    """core.db's module type: `db.X = v` also rebinds X in each submodule that held the old object."""

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
