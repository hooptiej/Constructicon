"""The Curator's Apprentice (#354, #344): the pure rules, stdlib only (plus core/timeline.py, also stdlib).

Two kinds of PROPOSAL the Curator makes and a person confirms. Nothing here writes; nothing here imports
the app. core/provenance_rules.py and core/correlations.py (the service ops) and
scripts/measure_apprentice_354_344.py (the read-only measurement on a copy of prod) all call it, the
script with nothing but the repo's code and the standard library. The loaders take a plain sqlite3
connection, so the service and the script read exactly the same rows the same way.

1. FILE PROVENANCE BY GROSS RULE (#354). A small registry (FILE_RULES): each rule names the file
   provenance it proposes, the media types it looks at, and a test on a row. First cut, owner decision
   2026-10-25 (answer (a)): ONE rule, "text-heavy PDFs and manuals -> Reference". More rules are added
   to FILE_RULES as the owner names them. A file whose provenance is already set is never proposed
   (the loader's WHERE clause), whatever any rule says.

   "Text-heavy" uses only what the app already stores: the length of capture_events.extracted_text
   (a PDF's whole text layer, every page, core/object_types/pdf.py text_layer; a scanned PDF with no
   text layer has only page 1's OCR, so it rarely passes) and the file's name. Page counts are not
   stored (pdf.get_properties opens the file each time), so text-per-page is not used: see
   TEXT_HEAVY_MIN_CHARS.

2. PROJECT CORRELATIONS (#344, trimmed): "these projects look related: link them?". A pair of cards is
   scored from four signals, each 0..1, weighted (W_*), summed to 0..1:
     text    cosine of the two cards' embedding centroids (the mean of their files' sentence-transformer
             embeddings, core/similarity.py's all-MiniLM-L6-v2, already stored per item; blank write-ups
             left out), mapped EMB_FLOOR..EMB_FULL -> 0..1;
     tags    tags the two cards' files share, min(1, shared / TAGS_FULL). Not counted: any card's own
             linked tag, hobby tags (sharing a hobby is already a grouping) and tags on more than
             TAG_COMMON_SHARE of all cards (too generic to mean anything);
     time    how much the two real-date spans overlap (core/timeline.py resolve_project_span, real dates
             only), as a share of the shorter span, with TIME_SLACK_DAYS of slack; 0 when either is
             undated;
     shared  files on both cards, min(1, n / SHARED_FULL).
   A pair is proposed at score >= SCORE_THRESHOLD, and only with some evidence beyond time (time alone
   is never enough). Left out: group kinds (family, collection: membership is their relation), pairs
   already linked by ANY link type, a card and its nested parts (any depth), two parts of one parent,
   two members of one family / collection, and pairs the owner answered "Not related".

Every threshold is a module constant, so it can be tuned after the read-only prod measurement.
"""

import array
import math
import re
from collections import Counter

from . import timeline

# --- 1. file provenance rules ------------------------------------------------------------------------

TEXT_HEAVY_MIN_CHARS = 6000
"""A PDF with at least this many characters of extracted text is "text-heavy". Measured against the
idea of a page: a page of plain prose is ~2,500-3,500 characters, so 6,000 is two-plus pages of real
text. Design PDFs, posters, logos exported to PDF, single-page receipts and scanned pages (OCR of page 1
only) stay under it. Tune after the prod measurement."""

MANUAL_HINT_MIN_CHARS = 300
"""A PDF or Word file whose NAME says manual / guide / datasheet / ... needs only this much text (a
short quick-start sheet is still reference material), so a near-empty file named "manual" (a scan
with no text) is not proposed blind."""

MANUAL_HINT_WORDS = frozenset({
    "manual", "manuals", "handbook", "datasheet", "datasheets", "guide", "userguide", "instructions",
    "instruction", "reference", "quickstart", "howto", "spec", "specs", "specification", "specifications",
})
MANUAL_HINT_PAIRS = frozenset({("data", "sheet"), ("quick", "start"), ("how", "to"), ("user", "guide")})

PROPOSALS_SHOWN = 30
"""At most this many files (or pairs) ride in one Curator need; the rest come up as these are answered.
Bulk-confirm confirms the ones shown."""


def name_words(name):
    """The words of a file name: camelCase and every non-letter split, lower-cased, extension dropped.
    "UserManual_v2.pdf" -> ["user", "manual", "v", "2"]."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name or "")
    return [w.lower() for w in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+", stem)]


def manual_hint(*names):
    """The hint word(s) found in any of the names ("manual", "data sheet"), or None."""
    for name in names:
        words = name_words(name)
        for w in words:
            if w in MANUAL_HINT_WORDS:
                return w
        for a, b in zip(words, words[1:]):
            if (a, b) in MANUAL_HINT_PAIRS:
                return f"{a} {b}"
    return None


def _chars(row):
    v = row.get("text_chars")
    if v is None:
        v = len((row.get("extracted_text") or "").strip())
    return int(v or 0)


def _text_heavy_reference(row):
    """The first rule: (reason, evidence) when `row` is a text-heavy PDF or a manual, else None."""
    mt = row.get("media_type")
    chars = _chars(row)
    hint = manual_hint(row.get("filename"), row.get("display_name"))
    if mt == "pdf" and chars >= TEXT_HEAVY_MIN_CHARS:
        why = f"a PDF with {chars:,} characters of text (text-heavy: {TEXT_HEAVY_MIN_CHARS:,} or more)"
        if hint:
            why += f", and its name says “{hint}”"
        return why, {"chars": chars, "hint": hint, "test": "text_heavy"}
    if mt in ("pdf", "word") and hint and chars >= MANUAL_HINT_MIN_CHARS:
        kind = "PDF" if mt == "pdf" else "Word document"
        return (f"a {kind} whose name says “{hint}”, with {chars:,} characters of text",
                {"chars": chars, "hint": hint, "test": "manual_name"})
    return None


class FileRule:
    """One gross rule: `test(row)` -> (reason, evidence) or None. `media_types` limits the rows loaded."""

    def __init__(self, key, provenance, label, question, media_types, test, explain):
        self.key, self.provenance, self.label, self.question = key, provenance, label, question
        self.media_types, self.test, self.explain = tuple(media_types), test, explain

    def public(self):
        return {"key": self.key, "provenance": self.provenance, "label": self.label, "question": self.question,
                "media_types": list(self.media_types), "explain": self.explain}


FILE_RULES = [
    FileRule("text_heavy_reference", "reference", "Text-heavy PDFs and manuals",
             "These look like reference material: mark them Reference?",
             ("pdf", "word"), _text_heavy_reference,
             f"A PDF with {TEXT_HEAVY_MIN_CHARS:,}+ characters of text, or a PDF / Word file whose name says manual, "
             f"guide, datasheet, handbook, instructions, reference or spec (with {MANUAL_HINT_MIN_CHARS}+ characters)."),
]
RULES_BY_KEY = {r.key: r for r in FILE_RULES}

PROVENANCE_SKIP_PREFIX = "need:provenance_rule_skip:"


def provenance_skip_key(rule_key, slug):
    """curator_dismissals key for "not this one" on a (rule, file)."""
    return f"{PROVENANCE_SKIP_PREFIX}{rule_key}:{slug}"


def _columns(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def load_provenance_candidates(conn, rule):
    """Rows `rule` looks at: its media types, provenance NOT set, not redacted, with a stored file.
    Text length comes from SQL (length(extracted_text)), never the text itself."""
    cols = _columns(conn, "capture_events")
    pick = [c for c in ("slug", "media_type", "filename", "display_name", "provenance", "timestamp", "file_size")
            if c in cols]
    marks = ",".join("?" * len(rule.media_types))
    sql = (f"SELECT {', '.join(pick)}, length(trim(extracted_text)) AS text_chars FROM capture_events "
           f"WHERE media_type IN ({marks}) AND (provenance IS NULL OR provenance = '') "
           "AND COALESCE(redacted, 0) = 0 AND stored_filename IS NOT NULL AND stored_filename != '' ORDER BY id")
    cur = conn.execute(sql, rule.media_types)
    names = [d[0] for d in cur.description]
    return [dict(zip(names, r)) for r in cur.fetchall()]


def dismissed_keys(conn, prefix):
    try:
        return {r[0] for r in conn.execute("SELECT nudge_key FROM curator_dismissals WHERE action = 'dismiss' "
                                           "AND substr(nudge_key, 1, ?) = ?", (len(prefix), prefix))}
    except Exception as e:  # noqa: BLE001 -- a DB without the table (an old copy): nothing answered yet
        if "no such table" in str(e):
            return set()
        raise


def propose_files(conn, rule):
    """(proposals, counts) for one rule: proposals [{slug, media_type, filename, display_name, chars,
    reason, evidence}] best evidence first; counts {considered, matched, answered_not_this}."""
    skipped = dismissed_keys(conn, f"{PROVENANCE_SKIP_PREFIX}{rule.key}:")
    rows = load_provenance_candidates(conn, rule)
    out, answered = [], 0
    for r in rows:
        hit = rule.test(r)
        if hit is None:
            continue
        if provenance_skip_key(rule.key, r["slug"]) in skipped:
            answered += 1
            continue
        reason, ev = hit
        out.append({**r, "chars": ev.get("chars"), "reason": reason, "evidence": ev})
    out.sort(key=lambda p: (0 if (p["evidence"].get("hint") and p["evidence"]["test"] == "text_heavy") else 1,
                            -(p.get("chars") or 0), p["slug"]))
    return out, {"considered": len(rows), "matched": len(out) + answered, "answered_not_this": answered}


# --- 2. project correlations -------------------------------------------------------------------------

W_TEXT, W_TAGS, W_TIME, W_SHARED = 0.50, 0.20, 0.15, 0.15
EMB_FLOOR = 0.35   # centroid cosine at or below this counts as no text signal
EMB_FULL = 0.85    # ... at or above this, full signal
TAGS_FULL = 3      # this many shared (meaningful) tags = full tag signal
TAG_COMMON_SHARE = 0.25  # a tag on more than this share of cards is ignored (only with 8+ cards)
TAG_COMMON_MIN_CARDS = 8
TIME_SLACK_DAYS = 30
SHARED_FULL = 2    # this many files on both cards = full signal
SCORE_THRESHOLD = 0.45
CORRELATION_KINDS = ("project", "thing", "action", "event")
CORRELATION_SKIP_PREFIX = "need:correlation_pair:"


def pair(a, b):
    return (a, b) if a <= b else (b, a)


def correlation_skip_key(a, b):
    a, b = pair(a, b)
    return f"{CORRELATION_SKIP_PREFIX}{a}:{b}"


_ITEM_COLS = ("slug", "media_type", "embedding", "display_date_override", "content_date", "source_modified_at",
              "type_metadata", "timestamp", "redacted")


def load_correlation_inputs(conn):
    """Everything the correlation pass reads, as plain Python: cards, their items, tags, links, families."""
    pcols = _columns(conn, "projects")
    cur = conn.execute("SELECT * FROM projects ORDER BY id")
    names = [d[0] for d in cur.description]
    cards = [dict(zip(names, r)) for r in cur.fetchall()]
    for c in cards:
        c.pop("agent_notes", None)
        c.setdefault("kind", "project")
    icols = [c for c in _ITEM_COLS if c in _columns(conn, "capture_events")]
    items_by_card = {}
    for r in conn.execute(f"SELECT pi.project_id, {', '.join('ce.' + c for c in icols)} FROM project_items pi "
                          "JOIN capture_events ce ON ce.slug = pi.post_slug"):
        d = dict(zip(["project_id"] + icols, r))
        if d.get("redacted"):
            continue
        items_by_card.setdefault(d["project_id"], []).append(d)
    tags_of = {}
    for slug, tag_id in conn.execute("SELECT post_slug, tag_id FROM post_tags"):
        tags_of.setdefault(slug, set()).add(tag_id)
    tcols = _columns(conn, "blog_tags")
    tag_names, hobby_tags = {}, set()
    for r in conn.execute("SELECT id, name" + (", COALESCE(is_hobby, 0)" if "is_hobby" in tcols else ", 0")
                          + " FROM blog_tags"):
        tag_names[r[0]] = r[1]
        if r[2]:
            hobby_tags.add(r[0])
    links = set()
    for a, b in conn.execute("SELECT slug_a, slug_b FROM project_relations"):
        links.add(pair(a, b))
    families = []
    try:
        families = [(f, m) for f, m in conn.execute("SELECT family_id, member_id FROM family_members")]
    except Exception as e:  # noqa: BLE001 -- an old copy without the table: no families
        if "no such table" not in str(e):
            raise
    first = conn.execute("SELECT MIN(timestamp) FROM capture_events").fetchone()[0]
    return {"cards": cards, "items_by_card": items_by_card, "tags_of": tags_of, "tag_names": tag_names,
            "hobby_tags": hobby_tags, "links": links, "families": families, "first_upload": first,
            "has_parent": "parent_id" in pcols, "dismissed": dismissed_keys(conn, CORRELATION_SKIP_PREFIX)}


def _vector(blob):
    if not blob:
        return None
    v = array.array("f")
    try:
        v.frombytes(bytes(blob))
    except ValueError:  # silent-ok: a blob that isn't float32 is no embedding; the card just has less text signal
        return None
    return v


def _centroid(vectors):
    vs = [v for v in vectors if v is not None]
    if not vs:
        return None
    n = len(vs[0])
    vs = [v for v in vs if len(v) == n]
    sums = [0.0] * n
    for v in vs:
        for i, x in enumerate(v):
            sums[i] += x
    norm = math.sqrt(sum(x * x for x in sums))
    return [x / norm for x in sums] if norm else None


def card_features(data):
    """{card id: {card, vec, tags, span, files}} for the cards the pass considers."""
    cards = {c["id"]: c for c in data["cards"]}
    linked_tags = {c.get("tag_id") for c in data["cards"] if c.get("tag_id") is not None}
    feats = {}
    for cid, c in cards.items():
        if (c.get("kind") or "project") not in CORRELATION_KINDS:
            continue
        items = data["items_by_card"].get(cid, [])
        content = [i for i in items if not timeline.is_blank_writeup_document(i)]
        tags = set()
        for i in content:
            tags |= data["tags_of"].get(i["slug"], set())
        tags -= linked_tags
        tags -= data["hobby_tags"]
        feats[cid] = {
            "card": c,
            "vec": _centroid([_vector(i.get("embedding")) for i in content]),
            "tags": tags,
            "span": timeline.resolve_project_span(c, items, data["first_upload"]),
            "files": {i["slug"] for i in content if i["slug"] != c.get("writeup_slug")},
        }
    # Tags too common to mean anything: on more than TAG_COMMON_SHARE of the considered cards.
    if len(feats) >= TAG_COMMON_MIN_CARDS:
        counts = Counter(t for f in feats.values() for t in f["tags"])
        common = {t for t, n in counts.items() if n > TAG_COMMON_SHARE * len(feats)}
        for f in feats.values():
            f["tags"] -= common
    return feats


def _ancestors(card_id, cards, limit=24):
    out, cur = [], cards.get(card_id)
    while cur is not None and cur.get("parent_id") is not None and len(out) < limit:
        out.append(cur["parent_id"])
        cur = cards.get(cur["parent_id"])
    return out


def excluded_reason(a, b, data, cards, fam_of):
    """Why a pair of cards is never proposed (already related some other way), or None."""
    if pair(a["slug"], b["slug"]) in data["links"]:
        return "already_linked"
    anc_a, anc_b = _ancestors(a["id"], cards), _ancestors(b["id"], cards)
    if b["id"] in anc_a or a["id"] in anc_b:
        return "nested"
    if a.get("parent_id") is not None and a.get("parent_id") == b.get("parent_id"):
        return "same_parent"
    if fam_of.get(a["id"], set()) & fam_of.get(b["id"], set()):
        return "same_family"
    return None


def _dot(u, v):
    return sum(x * y for x, y in zip(u, v))


def time_overlap(sa, sb, slack_days=TIME_SLACK_DAYS):
    """0..1: the overlap of two (start, end) real-date spans as a share of the shorter one (+ slack)."""
    if not sa or not sb or sa[0] is None or sb[0] is None:
        return None
    a0, a1 = sa[0], sa[1] if sa[1] is not None else sa[0]
    b0, b1 = sb[0], sb[1] if sb[1] is not None else sb[0]
    slack = slack_days * 86400
    inter = min(a1, b1) + slack - max(a0, b0)
    if inter <= 0:
        return 0.0
    shorter = min(a1 - a0, b1 - b0) + slack
    return max(0.0, min(1.0, inter / shorter))


def _cosines(feats, ids):
    """{(id_a, id_b): cosine} for every pair with two centroids. numpy when the app image has it (one
    matrix product), else plain Python; the centroids are unit vectors, so both are the dot product."""
    have = [i for i in ids if feats[i]["vec"] is not None]
    try:
        import numpy as np  # optional speed-up only; the standard library path gives the same numbers
    except ImportError:  # silent-ok: the pure-Python loop below computes the same cosines
        np = None
    out = {}
    if np is not None and have:
        m = np.asarray([feats[i]["vec"] for i in have], dtype=np.float64)
        sims = m @ m.T
        for x, ia in enumerate(have):
            for y in range(x + 1, len(have)):
                out[(ia, have[y])] = float(sims[x, y])
        return out
    for x, ia in enumerate(have):
        for ib in have[x + 1:]:
            out[(ia, ib)] = _dot(feats[ia]["vec"], feats[ib]["vec"])
    return out


def score_pair(fa, fb, tag_names=None, cos=...):
    """{score, parts: {text, tags, time, shared}, evidence: {cosine, shared_tags, overlap, shared_files}}.
    `cos` = the centroids' cosine when already computed (None = no text signal)."""
    if cos is ...:
        cos = _dot(fa["vec"], fb["vec"]) if fa["vec"] is not None and fb["vec"] is not None else None
    text = 0.0 if cos is None else max(0.0, min(1.0, (cos - EMB_FLOOR) / (EMB_FULL - EMB_FLOOR)))
    shared_tags = fa["tags"] & fb["tags"]
    tags = min(1.0, len(shared_tags) / TAGS_FULL)
    ov = time_overlap(fa["span"], fb["span"])
    time_part = ov or 0.0
    shared_files = fa["files"] & fb["files"]
    shared = min(1.0, len(shared_files) / SHARED_FULL)
    score = W_TEXT * text + W_TAGS * tags + W_TIME * time_part + W_SHARED * shared
    names = sorted((tag_names or {}).get(t, str(t)) for t in shared_tags)
    return {"score": round(score, 4),
            "parts": {"text": round(text, 4), "tags": round(tags, 4), "time": round(time_part, 4),
                      "shared": round(shared, 4)},
            "evidence": {"cosine": None if cos is None else round(cos, 4), "shared_tags": names,
                         "overlap": None if ov is None else round(ov, 4), "shared_files": sorted(shared_files)}}


def has_evidence(s):
    """Time overlap alone is never a reason to link two cards."""
    p = s["parts"]
    return p["text"] > 0 or p["tags"] > 0 or p["shared"] > 0


def why_text(s, a_title, b_title):
    """One plain sentence: what made these two look related."""
    bits = []
    ev = s["evidence"]
    if s["parts"]["text"] > 0:
        bits.append(f"their files read alike (text similarity {ev['cosine']:.2f})")
    if ev["shared_tags"]:
        bits.append("they share the tag" + ("s " if len(ev["shared_tags"]) > 1 else " ")
                    + ", ".join(ev["shared_tags"][:5]))
    if ev["shared_files"]:
        n = len(ev["shared_files"])
        bits.append(f"{n} file{'s are' if n != 1 else ' is'} on both")
    if s["parts"]["time"] > 0:
        bits.append("their dates overlap")
    return f"{a_title} and {b_title}: " + "; ".join(bits) + f" (score {s['score']:.2f} of 1)."


def propose_pairs(data, threshold=SCORE_THRESHOLD, keep_below=False):
    """(pairs, counts). pairs: [{a, b, a_id, b_id, a_title, b_title, score, parts, evidence, why}] best first
    (score >= threshold with evidence, not excluded, not answered). counts: {cards_considered,
    with_embedding, pairs_scored, excluded: {reason: n}, answered_not_related, below_threshold, proposed}.
    keep_below=True also returns the scores under the threshold (the measurement's histogram)."""
    cards = {c["id"]: c for c in data["cards"]}
    feats = card_features(data)
    fam_of = {}
    for f, m in data["families"]:
        fam_of.setdefault(m, set()).add(f)
    ids = sorted(feats)
    cosines = _cosines(feats, ids)
    excluded, answered, below, scored, out, under = Counter(), 0, 0, 0, [], []
    for i, ia in enumerate(ids):
        for ib in ids[i + 1:]:
            fa, fb = feats[ia], feats[ib]
            a, b = fa["card"], fb["card"]
            why_not = excluded_reason(a, b, data, cards, fam_of)
            if why_not:
                excluded[why_not] += 1
                continue
            scored += 1
            s = score_pair(fa, fb, data["tag_names"], cos=cosines.get((ia, ib)))
            if s["score"] < threshold or not has_evidence(s):
                below += 1
                if keep_below:
                    under.append(s["score"])
                continue
            if correlation_skip_key(a["slug"], b["slug"]) in data["dismissed"]:
                answered += 1
                continue
            x, y = (a, b) if a["slug"] <= b["slug"] else (b, a)
            out.append({"a": x["slug"], "b": y["slug"], "a_id": x["id"], "b_id": y["id"],
                        "a_title": x["title"], "b_title": y["title"], **s,
                        "why": why_text(s, x["title"], y["title"])})
    out.sort(key=lambda p: (-p["score"], p["a"], p["b"]))
    counts = {"cards_considered": len(feats), "with_embedding": sum(1 for f in feats.values() if f["vec"] is not None),
              "pairs_scored": scored, "excluded": dict(excluded), "answered_not_related": answered,
              "below_threshold": below, "proposed": len(out), "threshold": threshold}
    if keep_below:
        counts["below_scores"] = under
    return out, counts
