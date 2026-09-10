"""Name-based auto-tag / auto-project on upload — issue #240.

Matches an upload's original filename (and, for a folder-drop upload, the
dropped folder's name) against the names of *existing* tags
(`blog_tags`) and the titles of *existing* projects. Deliberately the same
permissive spirit as core/ocr.py's `_match_client_tags` — case-insensitive,
word-boundary-ish — just pointed at the tag tree / project list instead of
the imagerepo-era client alias list. No model involved; that's #239's
separate mechanism.

What happens with a match:

- **Tag matches** apply immediately, no confirmation — false positives are
  cheap to remove via the existing tag UI (the same reasoning OCR's client
  auto-tagging already relies on).
- **Exactly one project match** auto-adds the upload to that project
  (web/app.py's `_attach_to_project`, same as picking it in the drawer).
- **More than one project match** is NOT guessed at. It's queued as a
  pending decision (`db.add_pending_decision`, kind "project_match") that
  lists every candidate; the owner resolves it from the admin pane with
  checkboxes (zero, one, or several may apply). Projects the row is
  *already* in (e.g. the one explicitly picked in the upload drawer) are
  excluded from the candidate set first, so "I chose P and the name also
  hits Q" is a clean single-candidate auto-add of Q, not a spurious
  question about P.

Matching detail: a name is split into alphanumeric tokens and matched as
those tokens in order, separated by any run of non-alphanumerics, bounded
on both sides by a non-alphanumeric (or the string edge). So the project
"Clod-a-pede" matches "clod_a_pede_01.jpg", "ClodAPede" does NOT match
(different tokenization), and the tag "RC" would match "rc_car" but not
"arc" — except that names shorter than MIN_NAME_LEN are skipped entirely,
since two-letter tags match far too much by accident. `\b` alone isn't
used because it treats "_" as a word character, and underscores are the
most common separator in real filenames.
"""

import re

from . import db

MIN_NAME_LEN = 3  # same threshold ocr.py uses for client nicknames — shorter names are noise
KIND_PROJECT_MATCH = "project_match"


def _name_pattern(name):
    tokens = [t for t in re.split(r"[^a-z0-9]+", (name or "").lower()) if t]
    if not tokens or len("".join(tokens)) < MIN_NAME_LEN:
        return None
    body = r"[^a-z0-9]+".join(re.escape(t) for t in tokens)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?![a-z0-9])")


def _flatten_tags(nodes):
    flat = []
    for node in nodes:
        flat.append(node)
        flat.extend(_flatten_tags(node.get("children") or []))
    return flat


def match_text(text):
    """Returns (matched_tags, matched_projects) — lists of the raw
    blog_tags / projects dicts whose name/title occurs in `text`."""
    text = (text or "").lower()
    if not text.strip():
        return [], []
    tags, seen = [], set()
    for tag in _flatten_tags(db.list_tag_tree()):
        pattern = _name_pattern(tag["name"])
        if pattern and tag["id"] not in seen and pattern.search(text):
            tags.append(tag)
            seen.add(tag["id"])
    projects = []
    for project in db.list_projects():
        pattern = _name_pattern(project["title"])
        if pattern and pattern.search(text):
            projects.append(project)
    return tags, projects


def apply_to_upload(slug, texts, exclude_project_ids=()):
    """Runs the #240 behavior for one freshly-uploaded row. `texts` are the
    strings to match against (filename stem, folder name, ...). Applies tag
    matches itself; for projects it only *decides* — the caller attaches
    (web/app.py owns `_attach_to_project`, which also handles the linked
    tag and cover side effects) — so this module stays free of web-layer
    imports. Returns a summary dict:
        {"text", "tags": [names], "project": project-dict-or-None,
         "pending_id": int-or-None, "candidates": [project dicts]}
    """
    text = " ".join(t for t in texts if t)
    tags, projects = match_text(text)
    result = {"text": text, "tags": [], "project": None, "pending_id": None, "candidates": []}
    if tags:
        names = [t["name"] for t in tags]
        # Both halves of the tag system (see core/db.py's sync_real_tags_for_post
        # docstring): the free-text JSON column the edit form shows, and the
        # real post_tags link the tree browsing/pills read.
        db.add_tags(slug, names)
        db.attach_tags(slug, [t["id"] for t in tags])
        result["tags"] = names
    excluded = set(exclude_project_ids)
    candidates = [p for p in projects if p["id"] not in excluded]
    if len(candidates) == 1:
        result["project"] = candidates[0]
    elif len(candidates) > 1:
        result["candidates"] = candidates
        result["pending_id"] = db.add_pending_decision(
            KIND_PROJECT_MATCH,
            slug,
            {"candidate_project_ids": [p["id"] for p in candidates], "matched_text": text},
        )
    return result
