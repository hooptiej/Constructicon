#!/usr/bin/env python3
"""Self-contained check for the look-alike pass (#759, core/lookalikes.py + core/lookalike_match.py)
and the macOS " 2" copy suffix (#768, core/duplicates.py copy_marker).

Throwaway DB and storage (_testenv). Synthetic perceptual hashes are written straight onto the rows
(db.set_perceptual_hash, the pipeline's own writer), so the grouping is exact and needs no real photos.
It checks: the pure grouping (pigeonhole buckets = brute force, degenerate hashes ignored); byte-identical
and revision-chain pairs excluded and counted; a sensitive photo's group hidden from an editor and
counted; the suggested keeper and its `why` (incl. the " 2" suffix); "Not the same" remembered per pair
and a new member re-asked alone; confirm = #615's fold (cards and tags inherited, the rest trashed, ONE
batch, one undo restores every table and file); every refusal (viewer forbidden, unknown keeper, keeper
not in a group, a named slug outside the group, a conflicting pick, not_lookalikes); the MCP tool (paged
list, confirm dry run, dismiss, a viewer refused); the web routes; and the Curator need appearing and
going away. Also the measurement script on a copy of the throwaway DB.

    python scripts/test_lookalikes_759.py
"""

import io
import os
import random
import sqlite3
import subprocess
import sys
import time

import _testenv  # noqa: E402
TMP = _testenv.isolate("lookalikes-759-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import (actor, cards, curation_queue, db, duplicates, ingest, items, lookalike_match as lm,  # noqa: E402
                  lookalikes, membership, paths, revisions, roles, storage, tags, users)
_testenv.assert_isolated()
from core.errors import AppError  # noqa: E402

ingest.run_in_thread = lambda fn, *a: None

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def code_of(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e.code
    return None


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def raw(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.execute(sql, args)
    c.commit()
    c.close()


def png(color, size=(12, 8)):
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


BASE = 0x9F3C5A7E1B2D4C68  # a 64-bit hash with plenty of set and clear bits


def h(flip_bits=()):
    v = BASE
    for b in flip_bits:
        v ^= 1 << b
    return f"{v:016x}"


# One far-apart base hash per fixture group (pairwise >= 24 bits), so groups never touch each other.
_spread = random.Random(7590)
GB = {"A": BASE}
while len(GB) < 6:
    cand = _spread.getrandbits(64)
    if 20 <= bin(cand).count("1") <= 44 and all(bin(cand ^ v).count("1") >= 24 for v in GB.values()):
        GB["BCDES"[len(GB) - 1]] = cand


def gh(group, flip_bits=()):
    v = GB[group]
    for b in flip_bits:
        v ^= 1 << b
    return f"{v:016x}"


def mk(slug, data, filename, phash, ts, media_type="image"):
    sf = f"{slug}.png"
    (paths.storage_dir() / sf).write_bytes(data)
    storage.thumb_path_for(slug).write_bytes(b"thumb-" + slug.encode())
    db.insert_upload(slug, filename, sf, "tester", media_type=media_type, file_size=len(data))
    raw("UPDATE capture_events SET timestamp = ? WHERE slug = ?", ts, slug)
    if phash is not None:
        db.set_perceptual_hash(slug, phash)
    return slug


TABLES = ("capture_events", "project_items", "post_tags", "blog_tags", "capture_event_relations",
          "projects", "item_revisions", "curator_dismissals", "trash")


def snapshot():
    out = {}
    for t in TABLES:
        rows = q(f"SELECT * FROM {t}")
        for r in rows:
            r.pop("embedding", None)
        out[t] = sorted(repr(sorted(r.items())) for r in rows)
    return out


def files():
    return sorted(p.name for p in paths.storage_dir().iterdir() if p.is_file())


def groups_by_slug(plan_):
    return {s: g for g in plan_["groups"] for s in g["slugs"]}


# ---- 1. the pure grouping --------------------------------------------------------------------
print("--- 1. lookalike_match: buckets are exact, degenerate hashes ignored ---")
rng = random.Random(759)
ints = {}
for i in range(300):
    v = rng.getrandbits(64) if i % 3 == 0 else ints[f"s{i - 1}"] ^ (1 << rng.randrange(64)) ^ (1 << rng.randrange(64))
    ints[f"s{i}"] = v
fast = lm.candidate_pairs(ints, 6)
slow = {}
names = sorted(ints)
for i, a in enumerate(names):
    for b in names[i + 1:]:
        d = lm.distance(ints[a], ints[b])
        if d <= 6:
            slow[(a, b)] = d
check("candidate_pairs (pigeonhole buckets) equals the brute-force scan", fast == slow, (len(fast), len(slow)))
check("there were pairs to find", len(slow) >= 90, len(slow))
check("a degenerate hash (all zeros, one lone bit) is no hash",
      lm.hash_int("0000000000000000") is None and lm.hash_int("8000000000000000") is None
      and lm.hash_int(h()) is not None and lm.hash_int("abc") is None)
check("similarity re-exports the same degenerate check (one definition)",
      __import__("core.similarity", fromlist=["x"]).is_degenerate_hash is lm.is_degenerate_hash)
check("MAX_DISTANCE is a conservative named constant (<= similarity's 8)", 0 < lm.MAX_DISTANCE <= 8)
built = lm.build({"a": h(), "b": h([1]), "c": h([1, 2]), "z": "0000000000000000"}, dismissed={("a", "b")})
check("build: a dismissed pair leaves its group, the rest still groups",
      [g["members"] for g in built["groups"]] == [["a", "b", "c"]] and built["groups"][0]["answered"] == [("a", "b")]
      and built["degenerate"] == 1, built)

# ---- 2. #768: the macOS " 2" copy suffix ---------------------------------------------------------
print("--- 2. #768: 'name 2.ext' is a copy marker only beside 'name.ext' ---")
for name, group, want in [
        ("lildragclean1 2.eps", ["lildragclean1.eps"], duplicates.MAC_SUFFIX_RULE),
        ("lildragclean1 2.eps", ["LILDRAGCLEAN1.EPS"], duplicates.MAC_SUFFIX_RULE),
        ("photo 12.jpg", ["photo.jpg", "other.jpg"], duplicates.MAC_SUFFIX_RULE),
        ("IMG 2.jpg", [], None),                       # alone: an ordinary name
        ("IMG 2.jpg", ["IMG 3.jpg"], None),
        ("IMG 2.jpg", ["IMG.png"], None),               # another extension is another name
        ("photo 1.jpg", ["photo.jpg"], None),           # Finder starts at 2
        ("photo 100.jpg", ["photo.jpg"], None),         # 2..99 only
        ("lildragclean1.eps", ["lildragclean1 2.eps"], None),
        ("AlbertusMT (1).otf", [], "a '(n)' copy number")]:
    got = duplicates.copy_marker(name, [name] + group)
    check(f"copy_marker({name!r} beside {group}) = {want!r}", got == want, got)
check("has_copy_marker keeps its one-argument form (no group = no macOS rule)",
      duplicates.has_copy_marker("IMG 2.jpg") is False and duplicates.has_copy_marker("a (1).png") is True)

db.init_db()
NOW = time.time()
DAY = 86400
FIRST = NOW - 400 * DAY

with actor.acting_as(actor.ACTOR_SCRIPT):
    users.create_user("la_view", "la-view-password-1", roles.VIEWER)
    users.create_user("la_ed", "la-ed-password-123", roles.EDITOR)

with actor.acting_as(actor.ACTOR_MCP):
    # #768 on the byte-identical fold: the " 2" copy uploaded FIRST still loses, and the why says so.
    EPS = png((40, 40, 40))
    mk("eps_two", EPS, "lildragclean1 2.eps", None, FIRST, media_type="eps")
    mk("eps_one", EPS, "lildragclean1.eps", None, FIRST + 1, media_type="eps")
    dplan = duplicates.plan()
    eg = next((g for g in dplan["groups"] if g["keep"]["slug"] in ("eps_one", "eps_two")), None)
    check("#768: duplicates keeps lildragclean1.eps over 'lildragclean1 2.eps' uploaded first",
          eg is not None and eg["keep"]["slug"] == "eps_one", eg)
    check("#768: the plan's why names the rule and both files",
          eg is not None and "kept lildragclean1.eps: the other name carries the macOS ' 2' copy suffix" in eg["why"]
          and "lildragclean1 2.eps" in eg["why"], eg and eg["why"])
    items.delete(["eps_two", "eps_one"])  # out of the way of the look-alike checks

    # Look-alike group A: same photo, different bytes; "photo 2.png" uploaded first, plain name wins (#768).
    mk("a_two", png((200, 10, 10)), "photo 2.png", h([3]), FIRST + 10)
    mk("a_one", png((201, 10, 10), (24, 16)), "photo.png", h(), FIRST + 20)
    # B: byte-identical pair (same bytes, same hash): #615's job, excluded.
    B = png((10, 200, 10))
    mk("b_one", B, "green.png", gh("B"), FIRST + 30)
    mk("b_two", B, "green copy.png", gh("B"), FIRST + 31)
    # C: a revision chain with near-identical hashes: versions, never look-alikes.
    mk("c_v1", png((10, 10, 200)), "manual v1.png", gh("C"), FIRST + 40)
    mk("c_v2", png((10, 10, 201)), "manual v2.png", gh("C", [9, 30]), FIRST + 41)
    revisions.mark_superseded("c_v1", "c_v2")
    # D: far from everything.
    mk("d_far", png((1, 2, 3)), "far.png", gh("D"), FIRST + 50)
    # E: a pair, later answered "Not the same", then a newcomer close to both.
    mk("e_one", png((5, 5, 5)), "dog.png", gh("E"), FIRST + 60)
    mk("e_two", png((6, 6, 6)), "dog other.png", gh("E", [0, 1]), FIRST + 61)
    # S: a sensitive photo in a group (hidden from the editor, counted).
    mk("s_open", png((7, 7, 7)), "open.png", gh("S"), FIRST + 70)
    mk("s_secret", png((8, 8, 8)), "secret.png", gh("S", [0]), FIRST + 71)
    items.set_sensitive(["s_secret"], True)
    # A video with a close hash: images only.
    mk("v_clip", png((9, 9, 9)), "clip.mp4", h([4]), FIRST + 80, media_type="video")
    # A redacted look-alike: never a candidate.
    mk("r_gone", png((9, 9, 10)), "photo redacted.png", h([5]), FIRST + 90)
    raw("UPDATE capture_events SET redacted = 1 WHERE slug = 'r_gone'")
    # A's second copy is on a card and tagged: the keeper inherits them.
    cards.create("Hooptie car")
    CAR = next(p for p in db.list_projects() if p["title"] == "Hooptie car")
    cards.create("Desk build")
    DESK = next(p for p in db.list_projects() if p["title"] == "Desk build")
    # Each copy is on one card and has one tag, so cards and metadata tie and the name decides (#768).
    membership.add_files(CAR["id"], ["a_two"], **membership.UI_EFFECTS)
    membership.add_files(DESK["id"], ["a_one"], **membership.UI_EFFECTS)
    tags.attach("a_two", ["Maliboom"])
    tags.attach("a_one", ["Car"])

# ---- 3. grouping on the real DB ------------------------------------------------------------------
print("--- 3. candidate groups: images only, byte-identical and chains out, hidden counted ---")
with actor.acting_as(actor.ACTOR_MCP):
    P = lookalikes.plan()
G = groups_by_slug(P)
check("A is a group of exactly a_one + a_two", sorted(G.get("a_one", {}).get("slugs", [])) == ["a_one", "a_two"], P["groups"])
check("byte-identical B is not a look-alike group, and is counted",
      "b_one" not in G and P["summary"]["excluded_groups"]["byte_identical"] == 1, P["summary"])
check("revision chain C is not a look-alike group, and is counted",
      "c_v1" not in G and P["summary"]["excluded_groups"]["revision_chain"] == 1, P["summary"])
check("far D, the video and the redacted copy are in no group",
      not ({"d_far", "v_clip", "r_gone"} & set(G)), sorted(G))
check("the admin sees the sensitive photo's group", "s_secret" in G)
ga = G["a_one"]
check("A's suggested keeper is photo.png (the other carries the macOS ' 2' suffix)", ga["keep"] == "a_one", ga)
check("A's why names the rule", "macOS ' 2' copy suffix" in ga["why"] and "kept photo.png" in ga["why"], ga["why"])
m1 = next(m for m in ga["members"] if m["slug"] == "a_one")
check("a member carries filename, dimensions, file size, dates and cards",
      m1["filename"] == "photo.png" and (m1["width"], m1["height"]) == (24, 16) and m1["file_size"] > 0
      and m1["uploaded"] and "date" in m1
      and next(m for m in ga["members"] if m["slug"] == "a_two")["cards"] == ["Hooptie car"]
      and next(m for m in ga["members"] if m["slug"] == "a_two")["copy_marker"] == duplicates.MAC_SUFFIX_RULE, ga["members"])
check("pairs carry their distance", ga["pairs"] == [{"a": "a_one", "b": "a_two", "distance": 1}], ga["pairs"])
with actor.acting_as("user:la_ed"):
    PE = lookalikes.plan()
check("an editor doesn't see the group holding a sensitive photo, only its count",
      "s_secret" not in groups_by_slug(PE) and "s_open" not in groups_by_slug(PE)
      and PE["summary"]["hidden_groups"] == 1, PE["summary"])

# ---- 4. the Curator need ----------------------------------------------------------------------
print("--- 4. the Curator need appears ---")


def queue_lookalikes(who=actor.ACTOR_MCP):
    curation_queue._cache.update(fp=None, q=None)
    with actor.acting_as(who):
        qq = curation_queue.cached_queue()
    return [i for g in qq["groups"] if g["id"] == "lookalikes" for i in g["items"]]


QI = queue_lookalikes()
check("one need per group in the 'lookalikes' group, with the asked label",
      len(QI) == len(P["groups"]) and all(i["kind"] == "lookalike" and i["label"] == lookalikes.LABEL for i in QI),
      [(i["kind"], i["label"]) for i in QI])
qa = next(i for i in QI if "a_one" in i["item_slugs"])
check("the need suggests the keeper with its why", qa["suggested"]["picks"] == ["a_one"] and "macOS" in qa["suggested"]["reason"])
check("the editor's queue hides the sensitive group (for_actor, item_slugs)",
      not any("s_secret" in i["item_slugs"] for i in queue_lookalikes("user:la_ed")))
check("valid_key accepts the need's key (Defer works on it)", curation_queue.valid_key(qa["key"]), qa["key"])

# ---- 5. refusals ----------------------------------------------------------------------------
print("--- 5. refusals ---")
check("a viewer may not confirm (forbidden)", code_of(lookalikes.confirm, "a_one", actor="user:la_view") == "forbidden")
check("a viewer may not dismiss (forbidden)", code_of(lookalikes.dismiss, ["a_one", "a_two"], actor="user:la_view") == "forbidden")
check("unknown keeper: not_found", code_of(lookalikes.confirm, "no_such_slug", actor=actor.ACTOR_MCP) == "not_found")
check("keeper in no group: not_in_group", code_of(lookalikes.confirm, "d_far", actor=actor.ACTOR_MCP) == "not_in_group")
check("a named slug outside the keeper's group: not_in_group",
      code_of(lookalikes.confirm, "a_one", ["e_one"], actor=actor.ACTOR_MCP) == "not_in_group")
check("an editor confirming a group with a photo they can't see: forbidden",
      code_of(lookalikes.confirm, "s_open", ["s_secret"], actor="user:la_ed") == "forbidden")
check("dismiss with one photo: bad_request", code_of(lookalikes.dismiss, ["a_one"], actor=actor.ACTOR_MCP) == "bad_request")
check("dismiss photos that aren't look-alikes: not_lookalikes",
      code_of(lookalikes.dismiss, ["a_one", "d_far"], actor=actor.ACTOR_MCP) == "not_lookalikes")

# ---- 6. "Not the same": remembered, newcomer re-asked alone ---------------------------------------
print("--- 6. Not the same is remembered per pair ---")
before = snapshot()
r = lookalikes.dismiss(["e_one", "e_two"], actor="user:la_ed", dry_run=True)
check("dismiss dry run writes nothing", r.dry_run and snapshot() == before)
r = lookalikes.dismiss(["e_one", "e_two"], actor="user:la_ed")
check("dismiss answers its batch and pair", r.batch_id and r.data["pairs"] == [["e_one", "e_two"]], r.to_dict())
check("the pair is stored under its key", q("SELECT action FROM curator_dismissals WHERE nudge_key = ?",
                                            lookalikes.pair_key("e_two", "e_one")) == [{"action": "dismiss"}])
with actor.acting_as(actor.ACTOR_MCP):
    check("the answered group is gone", "e_one" not in groups_by_slug(lookalikes.plan()))
check("the answered group's need is gone", not any("e_one" in i["item_slugs"] for i in queue_lookalikes()))
check("dismissing it again: not_lookalikes", code_of(lookalikes.dismiss, ["e_one", "e_two"], actor=actor.ACTOR_MCP) == "not_lookalikes")
with actor.acting_as(actor.ACTOR_MCP):
    mk("e_new", png((4, 4, 4)), "dog again.png", gh("E", [0]), FIRST + 65)  # close to both
    ge = groups_by_slug(lookalikes.plan()).get("e_new")
check("a new member brings the group back, asking only about the newcomer",
      ge is not None and ge["new_members"] == ["e_new"] and ge["answered"] == [["e_one", "e_two"]], ge)
check("its need says so", any("e_new" in i["item_slugs"] and "already said" in i["detail"] for i in queue_lookalikes()))
with actor.acting_as(actor.ACTOR_MCP):
    r = lookalikes.confirm("e_new", dry_run=True)
check("confirming keep=newcomer folds one of the two and leaves the other out (answered against it)",
      r.dry_run and len(r.data["trashed"]) == 1 and len(r.data["left_out"]) == 1
      and "not the same photo" in r.data["left_out"][0]["reason"], r.to_dict())
with actor.acting_as(actor.ACTOR_MCP):
    r = lookalikes.confirm("e_new", shown=["e_new", "e_one", "e_two"], dry_run=True)
check("the Curator's 'shown' form leaves the answered one out instead of refusing",
      r.data["trashed"] == ["e_one"] and [x["slug"] for x in r.data["left_out"]] == ["e_two"], r.data)
with actor.acting_as(actor.ACTOR_MCP):
    r = lookalikes.confirm("e_new", shown=["e_new", "e_two"], dry_run=True)
check("'shown' limits the fold to what was on screen", r.data["trashed"] == ["e_two"], r.data)
check("slugs and shown together: bad_request",
      code_of(lookalikes.confirm, "e_new", ["e_one"], shown=["e_two"], actor=actor.ACTOR_MCP) == "bad_request")
check("naming both old photos explicitly is refused (lookalike_conflict)",
      code_of(lookalikes.confirm, "e_new", ["e_one", "e_two"], actor=actor.ACTOR_MCP) == "lookalike_conflict")
undone = cards.undo(lookalikes.dismiss(["e_new", "e_one", "e_two"], actor=actor.ACTOR_MCP).batch_id, actor=actor.ACTOR_MCP)
check("an undone 'Not the same' puts the question back", "e_new" in groups_by_slug(lookalikes.plan(actor=actor.ACTOR_MCP)))

# ---- 7. confirm = #615's fold, one batch, one undo ---------------------------------------------------
print("--- 7. Same photo, keep this one ---")
before, before_files = snapshot(), files()
r = lookalikes.confirm("a_one", actor="user:la_ed", dry_run=True)
check("confirm dry run: plan shown, nothing written, no file moved",
      r.dry_run and r.data["trashed"] == ["a_two"] and snapshot() == before and files() == before_files, r.to_dict())
r = lookalikes.confirm("a_one", ["a_two"], actor="user:la_ed")
check("confirm folds: a_two gone, a_one kept", db.get_by_slug("a_two") is None and db.get_by_slug("a_one") is not None)
check("the keeper inherited the card and the tag",
      sorted(p["title"] for p in db.list_projects_for_post("a_one")) == ["Desk build", "Hooptie car"]
      and "Maliboom" in {t["name"] for t in db.list_tags_for_post("a_one")})
check("the copy is in the trash (7-day undo)", q("SELECT slug FROM trash WHERE slug = 'a_two'") != [] and r.data["expires_at"])
ops = q("SELECT DISTINCT batch_id FROM audit_log WHERE batch_id = ?", r.batch_id)
check("ONE batch, holding the fold row and the delete",
      len(ops) == 1 and {x["op"] for x in q("SELECT op FROM audit_log WHERE batch_id = ?", r.batch_id)} >= {lookalikes.OP_FOLD},
      q("SELECT op FROM audit_log WHERE batch_id = ?", r.batch_id))
check("the group's need is gone after the fold", not any("a_one" in i["item_slugs"] for i in queue_lookalikes()))
cards.undo(r.batch_id, actor=actor.ACTOR_MCP)
check("one undo restores every table exactly", snapshot() == before,
      [t for t in TABLES if snapshot()[t] != before[t]])
check("one undo restores every file", files() == before_files, sorted(set(files()) ^ set(before_files)))
check("and the question is back", "a_two" in groups_by_slug(lookalikes.plan(actor=actor.ACTOR_MCP)))

# ---- 8. MCP ---------------------------------------------------------------------------------
print("--- 8. MCP constructicon_lookalikes ---")
from mcp_server import auth as mcp_auth  # noqa: E402
from mcp_server import server as S  # noqa: E402


def as_user(name, fn, *a, **kw):
    token = mcp_auth.IDENTITY.set({"actor": f"user:{name}", "user_id": None, "username": name})
    try:
        return fn(*a, **kw)
    finally:
        mcp_auth.IDENTITY.reset(token)


def err(out):
    return out.get("error", {}).get("code") if isinstance(out, dict) and out.get("ok") is False else None


check("ACTION_ROLES: list viewer, confirm/dismiss editor",
      S.role_for("constructicon_lookalikes", "list") == roles.VIEWER
      and S.role_for("constructicon_lookalikes", "confirm") == roles.EDITOR
      and S.role_for("constructicon_lookalikes", "dismiss") == roles.EDITOR)
total = len(lookalikes.plan(actor=actor.ACTOR_MCP)["groups"])
p1 = S.constructicon_lookalikes(limit=1)
check("list is paged: {items, next_cursor, more, limit} + summary",
      len(p1["items"]) == 1 and p1["more"] is True and p1["next_cursor"] and p1["limit"] == 1 and "summary" in p1, p1)
seen, cur = [g["id"] for g in p1["items"]], p1["next_cursor"]
while cur:
    pg = S.constructicon_lookalikes(limit=1, cursor=cur)
    seen += [g["id"] for g in pg["items"]]
    cur = pg["next_cursor"]
check("paging walks every group once", len(seen) == total == len(set(seen)), (seen, total))
check("an item gives members and the keeper's why",
      all(k in p1["items"][0] for k in ("members", "keep", "why", "pairs")) and
      all(k in p1["items"][0]["members"][0] for k in ("slug", "filename", "width", "height", "file_size", "date", "cards")))
check("a foreign cursor is refused", err(S.constructicon_lookalikes(cursor="eyJ2IjoidW5maWxlZCIsIm8iOjB9")) == "cursor_view_mismatch")
check("list refuses keep= (does not apply)", err(S.constructicon_lookalikes(keep="a_one")) == "bad_request")
out = S.constructicon_lookalikes(action="confirm", keep="a_one", dry_run=True)
check("confirm dry run over MCP", out.get("ok") and out["dry_run"] and out["trashed"] == ["a_two"] and out["batch_id"], out)
check("confirm with an unknown keeper: not_found", err(S.constructicon_lookalikes(action="confirm", keep="nope")) == "not_found")
check("confirm with a keeper in no group: not_in_group",
      err(S.constructicon_lookalikes(action="confirm", keep="d_far")) == "not_in_group")
check("confirm naming a slug outside the group: not_in_group",
      err(S.constructicon_lookalikes(action="confirm", keep="a_one", slugs=["s_open"])) == "not_in_group")
check("a viewer's confirm is refused (forbidden)",
      err(as_user("la_view", S.constructicon_lookalikes, action="confirm", keep="a_one")) == "forbidden")
check("a viewer may list", "items" in as_user("la_view", S.constructicon_lookalikes))
out = S.constructicon_lookalikes(action="dismiss", slugs=["s_open", "s_secret"])
check("dismiss over MCP", out.get("ok") and out["pairs"] == [["s_open", "s_secret"]] and out["batch_id"], out)
cards.undo(out["batch_id"], actor=actor.ACTOR_MCP)

# ---- 9. web routes --------------------------------------------------------------------------
print("--- 9. web routes ---")
from web import app as webapp  # noqa: E402
client = _testenv.client(webapp.app)
rr = client.get("/api/curator/lookalikes")
check("GET /api/curator/lookalikes", rr.status_code == 200 and "a_one" in groups_by_slug(rr.json()), rr.text[:300])
rr = client.post("/api/curator/lookalikes/confirm", data={"keep": "a_one", "slugs": ["a_one", "a_two"], "dry_run": "true"})
check("POST confirm (dry run)", rr.status_code == 200 and rr.json()["trashed"] == ["a_two"], rr.text[:300])
rr = client.post("/api/curator/lookalikes/confirm", data={"keep": "d_far"})
check("POST confirm refusal: 409 not_in_group in the shared shape",
      rr.status_code == 409 and rr.json()["error"]["code"] == "not_in_group" and rr.json()["detail"], rr.text[:300])
rr = client.post("/api/curator/lookalikes/dismiss", data={"slugs": ["a_one"]})
check("POST dismiss refusal: 400 with a message", rr.status_code == 400 and rr.json()["error"]["message"], rr.text[:300])
curation_queue._cache.update(fp=None, q=None)
html = client.get("/api/curator/queue/html?group=lookalikes&section=open").text
check("the Curator fragment shows the copies side by side with both buttons",
      "cq-lookalike" in html and "Same photo, keep this one" in html and "Not the same" in html
      and 'data-cq-keep="a_one"' in html and "/f/a_two/thumb" in html, html[:400])
shell = client.get("/api/curator/queue/html").text
check("the queue shell lists the 'Look-alike photos' group", "Look-alike photos" in shell)

# ---- 10. the measurement script ----------------------------------------------------------------
print("--- 10. scripts/measure_lookalikes_759.py on a read-only copy ---")
copy = os.path.join(str(TMP), "copy.db")
src = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
dst = sqlite3.connect(copy)
src.backup(dst)
src.close()
dst.close()
before_bytes = open(copy, "rb").read()
proc = subprocess.run([sys.executable, os.path.join(HERE, "measure_lookalikes_759.py"), copy,
                       "--storage", str(paths.storage_dir())], capture_output=True, text=True, timeout=120)
outp = proc.stdout
check("the script exits 0", proc.returncode == 0, proc.stderr[-500:])
check("it prints the counts, histogram, exclusions and a line per group",
      "images considered" in outp and "pair distance histogram" in outp and "byte-identical 1" in outp
      and "revision chain 1" in outp and "a_one photo.png" in outp, outp[-1200:])
check("it wrote nothing to the copy", open(copy, "rb").read() == before_bytes)
check("a missing database exits 2", subprocess.run([sys.executable, os.path.join(HERE, "measure_lookalikes_759.py"),
                                                     os.path.join(str(TMP), "nope.db")], capture_output=True).returncode == 2)

print()
if FAILS:
    print(f"{len(FAILS)} FAILED:")
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("all look-alike checks passed")
