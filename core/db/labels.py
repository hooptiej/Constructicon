"""Source labels and channels (capture_events.tech / .channel), and the hobby status vocabulary.

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""

import re

from .. import card_rules


# Provenance (capture_events.provenance, #341): the options live in the provenance_options
# table since #529 (core/provenance_options.py seeds and owns them).

# --- Brand asset roles (capture_events.brand_role) — #350 ---
# brand_role is freeform text (custom values allowed). The suggested list the picker offers is
# editable in Admin since #556: the vocab_terms table, owned by core/vocab.py (vocab 'brand_role').

# --- Hobby statuses (blog_tags.hobby_status) — #360 ---
# Controlled vocab for hobby activity. Only meaningful when is_hobby=1.
# V2 cards 3.3: a two-value MANUAL switch. 'dormant'/'abandoned' are deprecated
# aliases of 'inactive' (card_rules.HOBBY_DEPRECATED_ALIASES), accepted for one release.
HOBBY_STATUSES = list(card_rules.HOBBY_ACTIVITIES)

# --- Source (capture_events.tech) ---
# `tech` used to record which technician uploaded a screenshot in imagerepo
# (the original project), when it was a real multi-tech tool gated behind auth.
# Auth is gone (Phase 1) and this is a single-owner site now, so the column has
# been repurposed as a "Source" label: who or what actually added the row, and
# how. Every value written into `tech` should be one of these four shapes
# (SOURCE_MIGRATED is a template — fill in `<source>`). The owner's two come from
# the install's own config (#562, core/install_config.py owner_label): on the
# owner's installs exactly "Hooptie J (me) — manual upload" as before; on a fresh
# install "Owner — manual upload" until an owner is set. #560: `tech` is only the free-text
# Source NOTE now: WHO is `uploaded_by_user_id` (from the actor, #604) and HOW is `channel` (below);
# nothing decides identity, ownership or a gallery from it.
def source_manual_upload():
    from .. import install_config  # lazy: install_config imports this module
    return f"{install_config.owner_label()} — manual upload"


def source_automated_upload():
    from .. import install_config
    return f"{install_config.owner_label()} — automated upload"


SOURCE_CLAUDE = "Claude"
SOURCE_AUTHORED = "Claude — authored"  # Not written by anything yet — reserved for a
# future script that generates original content (e.g. a write-up) rather than
# migrating existing content from somewhere else. See SOURCE_MIGRATED for that case.


def source_migrated_from(source):
    """"Claude — migrated from <source>" — for automated migration scripts
    that bring in existing content from elsewhere (e.g. source="hooptiej.github.io"
    for scripts/archive/backfill_from_hooptiej_site.py)."""
    return f"Claude — migrated from {source}"


# #560: the CHANNEL (capture_events.channel) is how an item arrived; the Source label (`tech`) above
# stays the free-text note. A label in source_migrated_from's shape names where migrated content came
# from, so it sets the channel "migration:<source>"; anything else keeps the context's channel. Only
# ever descriptive: no identity, permission or ownership is decided from a channel or a label.
_MIGRATED_LABEL = re.compile(r"^Claude [—-] migrated from (.+)$", re.S)
CHANNEL_MAX = 120


def migration_source(label):
    """"<source>" for a "Claude — migrated from <source>" label, else None."""
    m = _MIGRATED_LABEL.match((label or "").strip())
    return " ".join(m.group(1).split())[:CHANNEL_MAX - 10] if m else None


def channel_for(label, base_channel):
    """The channel to record for an item with Source label `label` made through `base_channel`."""
    src = migration_source(label)
    return f"migration:{src}" if src else base_channel


CHANNEL_LABELS = {"web": "the web app", "mcp": "the MCP", "script": "a script", "system": "the app itself",
                  "migration": "a migration", "unknown": "not recorded"}


def channel_label(channel):
    """A channel as a person reads it: "the web app", "the MCP", "a migration from hooptiej.com"."""
    if not channel:
        return "not recorded"
    if channel.startswith("migration:"):
        return f"a migration from {channel[len('migration:'):]}"
    return CHANNEL_LABELS.get(channel, channel)


# Known "who" prefixes a Source string can start with — used to bucket the
# compact gallery views (home page panes, /gallery/user/<x>) under a short
# grouping key instead of showing/URL-encoding the full sentence-length
# Source string in a narrow layout. The full string is still stored as-is in
# `tech` and always shown in full on the object detail page.
def source_groups():
    """The owner's label (install config, #562) first, then "Claude". The neutral fallback
    ("Owner") is kept as a group too once a real label is set, so uploads made before the
    owner was configured still group."""
    from .. import install_config
    groups = [install_config.owner_label(), SOURCE_CLAUDE]
    if install_config.FALLBACK_OWNER not in groups:
        groups.append(install_config.FALLBACK_OWNER)
    return groups


def source_group(tech):
    """Short grouping key for a Source string, e.g. "<owner label> — manual
    upload" groups under "<owner label>". Falls back to the value unchanged
    if it doesn't start with a known prefix (covers legacy rows from before
    this repurposing)."""
    if not tech:
        return tech
    for group in source_groups():
        if tech == group or tech.startswith(group + " —") or tech.startswith(group + " -"):
            return group
    return tech
