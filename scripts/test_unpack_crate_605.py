#!/usr/bin/env python3
"""Unpack the Crate (#605): an archive item becomes a card holding its members. Throwaway DB and
storage (_testenv), synthetic archives built here, no server, no network.

Checks: the archive's type_metadata stats (new uploads and the archive_stats_605 backfill); the
preview (members, sizes, junk skipped and listed, unsupported, nested archive kept, byte-identical
linked, the cards it would make, decoded names, the wrapper folder) writes nothing; the three shapes
(flat collection, flat project, nested project) and the depth limit; zip-slip / absolute paths refused
and links skipped (zip and tar); the bomb caps (members, total bytes, ratio, time); encrypted (whole
and partial); corrupt (zip and a truncated tar.gz); byte-identical members linked, not duplicated;
one undo restores everything (and undoing the undo brings the items back); progress while running and
a partial failure that names the member; the busy and nothing-to-unpack refusals; a sensitive archive's
members come in sensitive, a restricted type comes in locked, an archive on a private card makes a
private card; a viewer is refused (web) and sees the action greyed; the web preview / start / poll; the
MCP type action (preview, start, status) and a parameter-less action refusing params.

Needs: Pillow (in the app image and most dev machines). .7z is tested only when py7zr is installed
(the app image has it; it prints SKIP otherwise). libcairo is stubbed when missing (Windows).

    python scripts/test_unpack_crate_605.py
"""

import io
import os
import re
import secrets
import sqlite3
import sys
import tarfile
import zipfile

import _testenv  # noqa: E402
TMP = _testenv.isolate("unpack-605-")
os.environ["CAPTION_DISABLED"] = "1"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

try:
    import cairosvg  # noqa: F401
except Exception:  # no libcairo here (e.g. Windows): object types import it
    import types
    sys.modules["cairosvg"] = types.ModuleType("cairosvg")

from PIL import Image  # noqa: E402

from core import actor, archive_reader, cards, db, ingest, object_types, paths, policy, sharing, storage, unpack, users  # noqa: E402
from core.errors import AppError  # noqa: E402
_testenv.assert_isolated()

ingest.run_in_thread = lambda fn, *a: None
NOOP = lambda fn, *a: None  # noqa: E731  (no OCR threads in a test)
FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:300]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def refusal(fn, *a, **kw):
    try:
        fn(*a, **kw)
    except AppError as e:
        return e
    return None


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def png(color, size=16):
    buf = io.BytesIO()
    Image.new("RGB", (size, size), color).save(buf, "PNG")
    return buf.getvalue()


def zip_bytes(entries):
    """entries: [(name, data)] or [(ZipInfo, data)]."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in entries:
            z.writestr(name, data)
    return buf.getvalue()


def tar_bytes(entries, mode="w"):
    """entries: [(name, data)] or [(TarInfo, None)] for links."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as t:
        for name, data in entries:
            if isinstance(name, tarfile.TarInfo):
                t.addfile(name)
                continue
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(data), 1262304000  # 2010-01-01
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def upload(name, data, sensitive=False):
    res = ingest.ingest_file(io.BytesIO(data), name, size=len(data), source="tester", run_background=NOOP,
                             sensitive=sensitive)
    assert res.row is not None, res.error
    return res.row["slug"]


def table_counts():
    return {t: q(f"SELECT COUNT(*) AS n FROM {t}")[0]["n"] for t in ("capture_events", "projects", "project_items")}


def convert(slug, shape="collection", depth=None):
    job = unpack.start(slug, shape, depth)
    return unpack.run_job(job["id"], run_background=NOOP)


def on_card(card_id):
    return {r["post_slug"] for r in q("SELECT post_slug FROM project_items WHERE project_id = ?", card_id)}


db.init_db()
users.limiter.reset()
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_TOTAL_MB", None)

# ------------------------------------------------------------------------------- fixtures
EXISTING_PNG = png((10, 200, 30))
existing_slug = upload("already-here.png", EXISTING_PNG)
PEM = b"-----BEGIN PUBLIC KEY-----\nVEVTVC1OT1QtQS1SRUFMLUtFWQ==\n-----END PUBLIC KEY-----\n"
INNER = zip_bytes([("inside/readme.txt", b"inner archive, stays an archive\n")])
VENZA = [
    ("Manual/", b""),
    ("Manual/index.html", b"<html><body><h1>Index</h1><a href='about.html'>about</a></body></html>"),
    ("Manual/about.html", b"<html><body>About</body></html>"),
    ("Manual/Repair%20and%20Diagnosis/engine.html", b"<html><body>Engine</body></html>"),
    ("Manual/Repair%20and%20Diagnosis/Brakes/pads.html", b"<html><body>Pads</body></html>"),
    ("Manual/Repair%20and%20Diagnosis/Brakes/Rear/drum.txt", b"Deep inside the rear brakes.\n"),
    ("Manual/images/diagram.png", png((200, 10, 10))),
    ("Manual/images/copy-of-existing.png", EXISTING_PNG),
    ("Manual/images/twin-a.png", png((1, 2, 3))),
    ("Manual/images/twin-b.png", png((1, 2, 3))),
    ("Manual/notes.xyz", b"an extension nothing handles"),
    ("Manual/inner.zip", INNER),
    ("Manual/server.pem", PEM),
    ("Manual/.DS_Store", b"\x00\x00\x00\x01Bud1"),
    ("Manual/Thumbs.db", b"thumbs"),
    ("Manual/desktop.ini", b"[.ShellClassInfo]"),
    ("__MACOSX/Manual/._index.html", b"resource fork"),
    ("Manual/._about.html", b"resource fork"),
]
venza_zip = zip_bytes(VENZA)

print("--- the archive's type_metadata (the {} fix) ---")
a1 = upload("2013 Toyota Venza%20Manual.zip", venza_zip)
stats = (db.get_by_slug(a1)["type_metadata"] or {}).get("archive_stats") or {}
check("a new zip upload stores archive_stats (entries, folders, uncompressed, by_type, max_depth)",
      stats.get("entries") == 17 and stats.get("folders") == 1 and stats.get("uncompressed", 0) > 0
      and stats.get("by_type", {}).get(".html") == 6 and stats.get("max_depth") == 4, stats)
props = object_types.get_object_type("archive").properties_fn(db.get_by_slug(a1))
check("the properties panel shows Inside and Deepest folder from the stored stats",
      "Inside" in props and ".html" in props["Inside"] and props.get("Deepest folder") == "4 levels down", props)
old_zip_path = os.path.join(os.environ["CONSTRUCTICON_STORAGE_DIR"], "oldzipXYZ.zip")
with open(old_zip_path, "wb") as f:
    f.write(venza_zip)
db.insert_upload("oldzipXYZ", "old upload.zip", "oldzipXYZ.zip", "tester", media_type="archive", file_size=len(venza_zip))
check("an archive stored before #605 has an empty type_metadata (the reported bug)",
      db.get_by_slug("oldzipXYZ")["type_metadata"] == {})
with db.transaction():
    db._mig_archive_stats_605()
back = (db.get_by_slug("oldzipXYZ")["type_metadata"] or {}).get("archive_stats") or {}
check("the archive_stats_605 backfill fills it", back.get("entries") == 17 and back.get("by_type"), back)
with db.transaction():
    db._mig_archive_stats_605()
check("the backfill is idempotent (fill-only; the stats are unchanged)",
      (db.get_by_slug("oldzipXYZ")["type_metadata"] or {}).get("archive_stats") == back)
tz = upload("bundle.tar.xz", tar_bytes([("a/x.txt", b"x"), ("a/y.txt", b"yy")], mode="w:xz"))
check(".tar.xz is an archive now, with stats", db.get_by_slug(tz)["media_type"] == "archive"
      and (db.get_by_slug(tz)["type_metadata"].get("archive_stats") or {}).get("entries") == 2)
tb = upload("bundle.tar.bz2", tar_bytes([("x.txt", b"x")], mode="w:bz2"))
check(".tar.bz2 is an archive now", db.get_by_slug(tb)["media_type"] == "archive")
spec = object_types.get_object_type("archive")
check("the archive type declares the unpack action (takes params, its own dialog)",
      any(a.key == "unpack" and a.takes_params and a.ui == "unpack" for a in spec.actions))

print("--- preview: nothing written ---")
before = table_counts()
changes_before = q("SELECT COUNT(*) AS n FROM audit_log WHERE op IS NOT NULL")[0]["n"]
p = unpack.preview(a1, "nested", 2)
check("preview writes nothing (items, cards, memberships, change log)",
      table_counts() == before and q("SELECT COUNT(*) AS n FROM audit_log WHERE op IS NOT NULL")[0]["n"] == changes_before)
check("preview says nothing was written", p["nothing_written"] and "Nothing has been written yet" in p["message"], p["message"])
check("the card is named after the archive, extension dropped and %20 decoded",
      p["card_title"] == "2013 Toyota Venza Manual", p["card_title"])
check("the single top folder is the card itself (wrapper)", p["wrapper_folder"] == "Manual", p["wrapper_folder"])
skipped = {s["path"]: s["reason"] for s in p["skipped"]}
check("junk is skipped AND listed with a reason (.DS_Store, Thumbs.db, desktop.ini, __MACOSX, ._*)",
      all(any(k.endswith(j) for k in skipped) for j in (".DS_Store", "Thumbs.db", "desktop.ini", "._index.html", "._about.html"))
      and all(v.startswith("junk:") for k, v in skipped.items() if k.endswith((".DS_Store", "Thumbs.db", "desktop.ini"))),
      skipped)
check("an unsupported member is skipped with the upload's own words",
      skipped.get("Manual/notes.xyz") == "Unsupported file type: .xyz", skipped.get("Manual/notes.xyz"))
members = {m["path"]: m for m in p["members"]}
check("decoded paths for display ('Repair and Diagnosis')", "Manual/Repair and Diagnosis/engine.html" in members,
      list(members)[:5])
check("a byte-identical member links to the existing item",
      members["Manual/images/copy-of-existing.png"]["action"] == "link"
      and members["Manual/images/copy-of-existing.png"]["existing_slug"] == existing_slug, members["Manual/images/copy-of-existing.png"])
check("two identical members: the second links to the first",
      members["Manual/images/twin-b.png"]["action"] == "link_member"
      and members["Manual/images/twin-b.png"]["same_as"] == "Manual/images/twin-a.png")
check("a nested archive is kept as an archive (noted)",
      any("kept as an archive" in n for n in members["Manual/inner.zip"]["notes"]))
check("a restricted type is noted as coming in locked", any("locked" in n for n in members["Manual/server.pem"]["notes"]))
titles = {c["path"]: c for c in p["cards"]}
check("nested depth 2: cards for 'Repair and Diagnosis', '.../Brakes' and 'images'; Rear folds into Brakes",
      set(titles) == {None, "Repair and Diagnosis", "Repair and Diagnosis/Brakes", "images"}
      and members["Manual/Repair and Diagnosis/Brakes/Rear/drum.txt"]["card"] == "Repair and Diagnosis/Brakes", titles)
check("preview totals add up (members + skipped = files)",
      p["totals"]["files"] == len(p["members"]) + len(p["skipped"]) and p["totals"]["to_ingest"] == 9
      and p["totals"]["to_link"] == 2, p["totals"])
check("preview breaks the members down by type (junk left out)", p["by_type"].get(".html") == 4
      and ".db" not in p["by_type"], p["by_type"])
p1 = unpack.preview(a1, "nested", 1)
m1 = {m["path"]: m for m in p1["members"]}
check("depth 1: only the top folders become cards; deeper files fold into them",
      {c["path"] for c in p1["cards"]} == {None, "Repair and Diagnosis", "images"}
      and m1["Manual/Repair and Diagnosis/Brakes/Rear/drum.txt"]["card"] == "Repair and Diagnosis")
e = refusal(unpack.preview, a1, "nested", 3)
check("depth 3 is refused (bad_shape, says 1 or 2)", e is not None and e.code == "bad_shape" and "1 or 2" in e.message,
      e and e.message)
e = refusal(unpack.preview, a1, "browsable-site")
check("an unknown shape is refused (bad_shape)", e is not None and e.code == "bad_shape")
e = refusal(unpack.preview, existing_slug)
check("a non-archive is refused (not_an_archive)", e is not None and e.code == "not_an_archive", e and e.code)

print("--- flat collection ---")
snap_slugs = {r["slug"] for r in q("SELECT slug FROM capture_events")}
snap_cards = {r["id"] for r in q("SELECT id FROM projects")}
snap_pi = {(r["project_id"], r["post_slug"]) for r in q("SELECT project_id, post_slug FROM project_items")}
job = convert(a1, "collection")
r = job["result"]
check("the job finishes done", job["status"] == "done" and job["finished"], (job["status"], job.get("error")))
card = db.get_project(r["card"]["id"])
check("a collection named after the archive", card["kind"] == "collection" and card["title"] == "2013 Toyota Venza Manual")
held = on_card(card["id"])
check("the original archive stays on the new card", a1 in held)
check("the existing identical item is linked onto the card, not stored twice",
      existing_slug in held and not any(storage.path_for(db.get_by_slug(s)["stored_filename"]).read_bytes() == EXISTING_PNG
                                        for s in {c["slug"] for c in job["result"]["created"]}))
created = {c["path"]: c["slug"] for c in r["created"]}
check("9 new items, 2 linked, 6 skipped", (len(r["created"]), len(r["linked"]), len(r["skipped"])) == (9, 2, 6), r["counts"])
check("every created and linked member is on the card",
      all(s in held for s in created.values()) and all(l["slug"] in held for l in r["linked"]))
twin_a = created["Manual/images/twin-a.png"]
check("the in-archive twin links to its first copy", any(l["path"] == "Manual/images/twin-b.png" and l["slug"] == twin_a
                                                          for l in r["linked"]))
eng = db.get_by_slug(created["Manual/Repair and Diagnosis/engine.html"])
check("a member keeps its decoded path and the archive in type_metadata.extracted_from",
      eng["type_metadata"]["extracted_from"]["path"] == "Manual/Repair and Diagnosis/engine.html"
      and eng["type_metadata"]["extracted_from"]["slug"] == a1, eng["type_metadata"].get("extracted_from"))
check("Source label: extracted from <archive>", "extracted from 2013 Toyota Venza%20Manual.zip" in (eng.get("tech") or ""),
      eng.get("tech"))
check("filed by the conversion only (no auto-match, no other card)",
      all(len(db.list_projects_for_post(s)) == 1 for s in created.values()))
inner = db.get_by_slug(created["Manual/inner.zip"])
check("the nested archive is an archive item, not unpacked", inner["media_type"] == "archive"
      and not any("readme" in p for p in created))
pem = db.get_by_slug(created["Manual/server.pem"])
check("a restricted type comes out restricted (sensitive)", policy.is_restricted(pem), pem["media_type"])
drum = db.get_by_slug(created["Manual/Repair and Diagnosis/Brakes/Rear/drum.txt"])
check("no tar/zip date from 1980: none is stored; a real member mtime becomes source_modified_at",
      drum.get("source_modified_at") is not None)
check("junk was never created", not any(re.search(r"(DS_Store|Thumbs\.db|desktop\.ini|\._)", s) for s in created))
diag = db.get_by_slug(created["Manual/images/diagram.png"])
check("an image member got its thumbnail through the normal pipeline", storage.thumb_path_for(diag["slug"]).exists())
check("the first picture became the card's cover", card["cover_slug"] in (created["Manual/images/diagram.png"], existing_slug,
                                                                            twin_a), card["cover_slug"])
check("the job's progress ended at the total", job["progress"]["done"] == job["progress"]["total"] == 11, job["progress"])
check("the item page shape carries extracted_from (ORIGIN row)",
      __import__("web.shapes", fromlist=["x"])._extracted_from(eng)["slug"] == a1)

print("--- one undo ---")
u = cards.undo(job["batch_id"])
check("undo answers with the created items it trashed", sorted(u.data["created_items_trashed"]) == sorted(created.values()),
      u.data.get("created_items_trashed"))
check("undo leaves exactly the items there were before (archive and existing item kept)",
      {r["slug"] for r in q("SELECT slug FROM capture_events")} == snap_slugs)
check("undo removes the card(s)", {r["id"] for r in q("SELECT id FROM projects")} == snap_cards)
check("undo restores every membership exactly", {(r["project_id"], r["post_slug"]) for r in q(
    "SELECT project_id, post_slug FROM project_items")} == snap_pi)
trash_rows = {r["slug"]: r for r in q("SELECT * FROM trash WHERE batch_id = ?", u.batch_id)}
check("the created items are in the trash (7-day hold), their files moved out of storage",
      set(trash_rows) == set(created.values()) and all(r["expires_at"] and r["reason"] == "delete" for r in trash_rows.values())
      and all(not storage.path_for(r["stored_filename"]).exists()
              and (paths.trash_dir(u.batch_id) / r["stored_filename"]).exists() for r in trash_rows.values()), trash_rows.keys())
uu = cards.undo(u.batch_id)
check("undoing the undo brings the card and its items back",
      all(db.get_by_slug(s) is not None for s in created.values()) and db.get_project(card["id"]) is not None
      and created["Manual/index.html"] in on_card(card["id"]))
cards.undo(uu.batch_id)
check("and undo again takes them away again", all(db.get_by_slug(s) is None for s in created.values())
      and db.get_project(card["id"]) is None)

print("--- flat project, nested project ---")
a2 = upload("venza-two.zip", venza_zip)
j2 = convert(a2, "project")
c2 = db.get_project(j2["result"]["card"]["id"])
check("flat project: a project, no nested cards", c2["kind"] == "project" and j2["result"]["cards_made"] == 1
      and not q("SELECT 1 FROM projects WHERE parent_id = ?", c2["id"]))
cards.undo(j2["batch_id"])
a3 = upload("venza-three.zip", venza_zip)
j3 = convert(a3, "nested", 2)
root = db.get_project(j3["result"]["card"]["id"])
kids = {r["title"]: r for r in q("SELECT * FROM projects WHERE parent_id = ?", root["id"])}
check("nested: folder cards under the root", set(kids) == {"Repair and Diagnosis", "images"}, set(kids))
grand = q("SELECT * FROM projects WHERE parent_id = ?", kids["Repair and Diagnosis"]["id"])
check("nested depth 2: 'Brakes' nested under 'Repair and Diagnosis'", [g["title"] for g in grand] == ["Brakes"])
c3 = {c["path"]: c["slug"] for c in j3["result"]["created"]}
check("depth limit: the Rear/ file is on the Brakes card", c3["Manual/Repair and Diagnosis/Brakes/Rear/drum.txt"] in on_card(grand[0]["id"]))
check("folder cards have no linked tag (an 'images' folder never joins an 'images' tag)",
      kids["images"]["tag_id"] is None and root["tag_id"] is not None)
e = refusal(cards.set_kind, root["id"], "collection")
check("turning that nested project into a collection is refused with the reason", e is not None and e.message, e and e.code)
cards.undo(j3["batch_id"])
check("one undo removes all the nested cards too", not q("SELECT 1 FROM projects WHERE id = ? OR parent_id = ?", root["id"], root["id"]))

print("--- hostile paths and links ---")
for label, name in (("zip-slip ..", "../evil.txt"), ("absolute path", "/etc/abs.txt"), ("drive path", "C:/win.txt")):
    bad = upload(f"bad-{secrets.token_hex(3)}.zip", zip_bytes([("ok.txt", b"ok"), (name, b"evil")]))
    e = refusal(unpack.preview, bad)
    check(f"{label} refuses the whole archive (archive_hostile_path), naming the member",
          e is not None and e.code == "archive_hostile_path" and "Nothing was extracted" in e.message
          and (e.details or {}).get("member"), e and (e.code, e.message))
link = zipfile.ZipInfo("shortcut")
link.create_system = 3
link.external_attr = (0o120777 << 16)
zl = upload("links.zip", zip_bytes([("real.txt", b"real"), (link, b"/etc/passwd")]))
pl = unpack.preview(zl)
check("a zip symlink is skipped and listed, never followed",
      any(s["path"] == "shortcut" and "link" in s["reason"] for s in pl["skipped"]), pl["skipped"])
sym = tarfile.TarInfo("evil-link")
sym.type, sym.linkname = tarfile.SYMTYPE, "/etc/passwd"
hard = tarfile.TarInfo("hard-link")
hard.type, hard.linkname = tarfile.LNKTYPE, "real.txt"
tl = upload("links.tar", tar_bytes([("real.txt", b"real"), (sym, None), (hard, None)]))
pt = unpack.preview(tl)
check("tar symlinks and hardlinks are skipped and listed",
      {s["path"] for s in pt["skipped"] if "link" in s["reason"]} == {"evil-link", "hard-link"}, pt["skipped"])
ts = upload("slip.tar", tar_bytes([("../../etc/x", b"x")]))
e = refusal(unpack.preview, ts)
check("tar zip-slip is refused", e is not None and e.code == "archive_hostile_path")

print("--- bomb caps ---")
os.environ["CONSTRUCTICON_UNPACK_MAX_MEMBERS"] = "5"
e = refusal(unpack.preview, a1)
check("member cap: archive_too_many, naming the cap", e is not None and e.code == "archive_too_many" and "5" in e.message,
      e and e.message)
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_MEMBERS")
big = upload("big.zip", zip_bytes([("noise.bin.txt", os.urandom(1024) * 1)] + [(f"r{i}.txt", os.urandom(300 * 1024)) for i in range(5)]))
os.environ["CONSTRUCTICON_UNPACK_MAX_TOTAL_MB"] = "1"
e = refusal(unpack.preview, big)
check("total cap: archive_too_big, with the numbers", e is not None and e.code == "archive_too_big" and "MB" in e.message,
      e and e.message)
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_TOTAL_MB")
os.environ["CONSTRUCTICON_UNPACK_MAX_TOTAL_MB"] = "50"
bomb = upload("bomb.zip", zip_bytes([("zeros.txt", b"\0" * (8 * 1024 * 1024))]))
e = refusal(unpack.preview, bomb)
check("ratio cap: archive_ratio (8 MB of zeros from a few KB)", e is not None and e.code == "archive_ratio"
      and "times its size" in e.message, e and (e.code, e.message))
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_TOTAL_MB")
os.environ["CONSTRUCTICON_UNPACK_MAX_SECONDS"] = "0.000001"
e = refusal(unpack.preview, a1)
check("time cap: archive_too_slow", e is not None and e.code == "archive_too_slow", e and (e.code, e.message))
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_SECONDS")
caps = unpack.parse_limits.unpack_caps()
check("the defaults: total = 4x the max upload size (100 MB), 5,000 members, 200x, 300 s",
      caps.as_dict()["max_total_mb"] == 100 and caps.max_members == 5000 and caps.max_ratio == 200
      and caps.max_seconds == 300, caps.as_dict())
os.environ["CONSTRUCTICON_MAX_UPLOAD_MB"] = "60"
check("the total follows the upload size: 60 MB uploads -> 240 MB unpacked",
      unpack.parse_limits.unpack_caps().as_dict()["max_total_mb"] == 240)
os.environ["CONSTRUCTICON_UNPACK_MAX_TOTAL_MB"] = "30"
check("CONSTRUCTICON_UNPACK_MAX_TOTAL_MB overrides it", unpack.parse_limits.unpack_caps().as_dict()["max_total_mb"] == 30)
os.environ.pop("CONSTRUCTICON_UNPACK_MAX_TOTAL_MB")
os.environ.pop("CONSTRUCTICON_MAX_UPLOAD_MB")

print("--- encrypted and corrupt ---")


CENTRAL = b"PK" + bytes([1, 2])  # a zip central-directory record's signature


def encrypt_flags(data, names):
    """Python can't write an encrypted zip; set the 'encrypted' flag bit on the named members' local and
    central headers instead (the reader decides from that bit and never reads such a member)."""
    buf = bytearray(data)
    for info in zipfile.ZipFile(io.BytesIO(data)).infolist():
        if info.filename in names:
            buf[info.header_offset + 6] |= 0x1
    i = buf.find(CENTRAL)
    while i != -1:
        n = int.from_bytes(buf[i + 28:i + 30], "little")
        if bytes(buf[i + 46:i + 46 + n]).decode() in names:
            buf[i + 8] |= 0x1
        i = buf.find(CENTRAL, i + 4)
    return bytes(buf)


enc = upload("locked.zip", encrypt_flags(zip_bytes([("a.txt", b"x"), ("b.txt", b"y")]), {"a.txt", "b.txt"}))
e = refusal(unpack.preview, enc)
check("an all-encrypted zip: 'This archive is password-protected; nothing was extracted.'",
      e is not None and e.code == "archive_encrypted" and e.message == "This archive is password-protected; nothing was extracted.",
      e and e.message)
half = upload("half.zip", encrypt_flags(zip_bytes([("open.txt", b"open"), ("secret.txt", b"y")]), {"secret.txt"}))
ph = unpack.preview(half)
check("a partly encrypted zip: the encrypted member is skipped as 'encrypted, not extracted'",
      any(s["path"] == "secret.txt" and s["reason"].startswith("encrypted, not extracted") for s in ph["skipped"]), ph["skipped"])
cor = upload("damaged.zip", zip_bytes([("a.txt", b"a" * 5000)]))
with open(os.path.join(os.environ["CONSTRUCTICON_STORAGE_DIR"], db.get_by_slug(cor)["stored_filename"]), "wb") as f:
    f.write(b"PK\x03\x04 this is not really a zip anymore" * 10)
e = refusal(unpack.preview, cor)
check("a corrupt zip: archive_corrupt with the reason", e is not None and e.code == "archive_corrupt"
      and "damaged" in e.message and "Nothing was extracted" in e.message, e and (e.code, e.message))
good_tgz = tar_bytes([(f"f{i}.txt", os.urandom(20000)) for i in range(5)], mode="w:gz")
trunc = upload("cut.tar.gz", good_tgz)
with open(os.path.join(os.environ["CONSTRUCTICON_STORAGE_DIR"], db.get_by_slug(trunc)["stored_filename"]), "wb") as f:
    f.write(good_tgz[: len(good_tgz) // 2])
e = refusal(unpack.preview, trunc)
check("a truncated tar.gz: archive_corrupt", e is not None and e.code == "archive_corrupt", e and (e.code, e.message))

print("--- .7z ---")
if archive_reader.py7zr is None:
    print("SKIP .7z conversion: py7zr isn't installed here (the app image has it)")
else:
    buf = io.BytesIO()
    with archive_reader.py7zr.SevenZipFile(buf, "w") as z:
        z.writestr(b"seven zip text\n", "docs/seven.txt")
        z.writestr(png((5, 5, 250)), "docs/pic.png")
    s7 = upload("crate.7z", buf.getvalue())
    p7 = unpack.preview(s7)
    check(".7z preview lists its members", {m["path"] for m in p7["members"]} == {"docs/seven.txt", "docs/pic.png"}, p7["members"])
    j7 = convert(s7)
    check(".7z converts", j7["status"] == "done" and j7["result"]["counts"]["created"] == 2, j7.get("error") or j7["result"])
    cards.undo(j7["batch_id"])

print("--- progress and a partial failure ---")
a4 = upload("venza-four.zip", venza_zip)
job4 = unpack.start(a4, "collection")
seen = []
real_ingest = ingest.ingest_file


def watching_ingest(f, filename, **kw):
    seen.append(dict(db.get_unpack_job(job4["id"])["progress"]))
    if filename == "about.html":
        raise OSError("disk hiccup while storing about.html")
    return real_ingest(f, filename, **kw)


ingest.ingest_file = watching_ingest
unpack.PROGRESS_EVERY_SECONDS = 0
try:
    done4 = unpack.run_job(job4["id"], run_background=NOOP)
finally:
    ingest.ingest_file = real_ingest
check("progress is written while it runs (phase members, done climbing, the current member named)",
      len(seen) >= 5 and all(s.get("phase") == "members" for s in seen) and seen[-1]["done"] > seen[0]["done"]
      and all(s.get("current") for s in seen), seen[:3])
check("a member failure makes the job partial, not failed", done4["status"] == "partial", done4["status"])
fails = done4["result"]["failed"]
check("the failed member is named with its reason", len(fails) == 1 and fails[0]["path"] == "Manual/about.html"
      and "disk hiccup" in fails[0]["reason"], fails)
check("the message says what was and wasn't done, and that undo covers it",
      "1 failed" in done4["message"] and "about.html" in done4["message"] and "Undo" in done4["message"], done4["message"])
made4 = [c["slug"] for c in done4["result"]["created"]]
check("everything else was made", len(made4) == 8)
cards.undo(done4["batch_id"])
check("undo after a partial failure removes what was made", all(db.get_by_slug(s) is None for s in made4))

print("--- busy, nothing to unpack, interrupted ---")
a5 = upload("venza-five.zip", venza_zip)
q5 = unpack.start(a5)
e = refusal(unpack.start, a5)
check("a second start while one is queued: unpack_busy (409)", e is not None and e.code == "unpack_busy" and e.status == 409)
db._update_unpack_job(q5["id"], status="running")
unpack.recover_interrupted()
st5 = unpack.job_status(q5["id"])
check("a job cut off by a restart is marked failed, saying so", st5["status"] == "failed" and "restarted" in st5["message"])
junk_only = upload("junk.zip", zip_bytes([(".DS_Store", b"x"), ("__MACOSX/._a", b"y")]))
jj = convert(junk_only)
check("nothing to unpack: the job fails with the reason, and makes no card",
      jj["status"] == "failed" and "Nothing to unpack" in jj["message"] and not q("SELECT 1 FROM audit_log WHERE batch_id = ?", jj["batch_id"]),
      jj["message"])

print("--- sensitive and private archives ---")
sens = upload("secret-crate.zip", zip_bytes([("a.txt", b"sensitive a"), ("b.txt", b"sensitive b")]), sensitive=True)
js = convert(sens)
check("a sensitive archive's members come in sensitive",
      all(db.get_by_slug(c["slug"])["sensitive"] for c in js["result"]["created"]) and js["result"]["created"])
cards.undo(js["batch_id"])
priv_card = cards.create("Private stash").data["card"]
sharing.set_private("card", priv_card["id"], True)
pa = upload("private-crate.zip", zip_bytes([("p.txt", b"private member")]))
from core import membership  # noqa: E402
membership.add_files(priv_card["id"], [pa], link_tag=True, auto_cover=False)
pv = unpack.preview(pa)
check("preview warns the new card will be private", pv["private"] and "private too" in pv["message"])
jp = convert(pa)
newc = db.get_project(jp["result"]["card"]["id"])
check("an archive on a private card makes a private card (its members stay hidden from outsiders)",
      newc["private"] == 1 and jp["result"]["private"])
cards.undo(jp["batch_id"])

print("--- web: roles, preview, start, poll ---")
from starlette.testclient import TestClient  # noqa: E402
from web import app as webapp  # noqa: E402
BASE = "http://testhost.local"
SAME = {"Origin": BASE}
PW = {}
with actor.acting_as(actor.ACTOR_SCRIPT):
    for name, role in (("up_viewer", "viewer"), ("up_editor", "editor")):
        PW[name] = "up-" + secrets.token_urlsafe(16)
        users.create_user(name, PW[name], role)
C, H = {}, {}
for name in PW:
    c = TestClient(webapp.app, base_url=BASE, follow_redirects=False)
    c.post("/api/auth/login", json={"username": name, "password": PW[name]}, headers=SAME)
    m = re.search(r'<meta name="csrf-token" content="([^"]+)"', c.get("/").text)
    C[name], H[name] = c, {**SAME, "X-CSRF-Token": m.group(1) if m else ""}
aw = upload("web-crate.zip", zip_bytes([("w/one.txt", b"one"), ("w/two.txt", b"two")]))
page = C["up_viewer"].get(f"/object/{aw}").text
btn = re.search(r'<button[^>]*data-action="unpack"[^>]*>', page)
check("a viewer sees the Unpack action greyed with the role it needs (#779)",
      btn is not None and "role-locked" in btn.group(0) and "data-locked-message" in btn.group(0), btn and btn.group(0))
check("the item page loads unpack-crate.js for an archive", "/static/js/unpack-crate.js" in page)
rv = C["up_viewer"].post(f"/api/image/{aw}/action/unpack", data={"mode": "preview"}, headers=H["up_viewer"])
check("a viewer is refused (403) with the role in the message", rv.status_code == 403 and "editor" in rv.text, rv.text[:200])
re_ = C["up_editor"].post(f"/api/image/{aw}/action/unpack", data={"mode": "preview"}, headers=H["up_editor"])
check("an editor previews over the web", re_.status_code == 200 and re_.json()["preview"]["totals"]["to_ingest"] == 2, re_.text[:300])
rb = C["up_editor"].post(f"/api/image/{aw}/action/unpack", data={"mode": "preview", "shape": "nested", "depth": "9"},
                         headers=H["up_editor"])
check("a bad depth over the web is a 400 with the reason in the shared shape",
      rb.status_code == 400 and rb.json()["error"]["code"] == "bad_shape", rb.text[:200])
rs = C["up_editor"].post(f"/api/image/{aw}/action/unpack", data={"mode": "start"}, headers=H["up_editor"])
check("an editor starts it (a queued job)", rs.status_code == 200 and rs.json()["job"]["status"] == "queued", rs.text[:300])
wj = rs.json()["job"]
unpack.run_job(wj["id"], run_background=NOOP)
poll = C["up_viewer"].get(f"/api/unpack/jobs/{wj['id']}")
check("the page's poll answers the finished job (a viewer may read it)",
      poll.status_code == 200 and poll.json()["job"]["status"] == "done", poll.text[:300])
check("the change log names the editor", all(r["actor"] == "user:up_editor"
                                              for r in q("SELECT actor FROM audit_log WHERE batch_id = ?", wj["batch_id"])))
check("an unknown job is 404", C["up_viewer"].get("/api/unpack/jobs/999999").status_code == 404)
ED_ID = db.get_user(username="up_editor")["id"]


def member_rows(job):
    return [db.get_by_slug(c["slug"]) for c in unpack.job_status(job["id"])["result"]["created"]]


wm = member_rows(wj)
check("starter 1, the web: members arrived via the web, uploaded by the editor, no key credit",
      wm and all(m["channel"] == "web" and m["uploaded_by_user_id"] == ED_ID and m.get("uploaded_via_token_id") is None
                 for m in wm), [(m["channel"], m["uploaded_by_user_id"], m.get("uploaded_via_token_id")) for m in wm])
check("... and the web job stored channel web, no key", (db.get_unpack_job(wj["id"])["channel"],
                                                           db.get_unpack_job(wj["id"])["via_token_id"]) == ("web", None))
ru = C["up_editor"].post(f"/api/changes/{wj['batch_id']}/undo", headers=H["up_editor"])
check("the editor undoes it over the web", ru.status_code == 200, ru.text[:200])

print("--- MCP ---")
from mcp_server import server as mcp  # noqa: E402
am = upload("mcp-crate.zip", zip_bytes([("m/a.txt", b"mcp a"), ("m/b.txt", b"mcp b"), ("m/.DS_Store", b"j")]))
r = mcp.constructicon_run_type_action(slug=am, action="unpack", params={"mode": "preview"})
check("MCP preview", r.get("ok") and r["preview"]["totals"]["to_ingest"] == 2 and r["preview"]["totals"]["skipped"] == 1, str(r)[:300])
r = mcp.constructicon_run_type_action(slug=am, action="unpack", params={"mode": "start", "shape": "project"})
check("MCP start queues a job (the web worker runs it)", r.get("ok") and r["job"]["status"] == "queued", str(r)[:300])
mj = r["job"]["id"]
unpack.run_job(mj, run_background=NOOP)
r = mcp.constructicon_run_type_action(slug=am, action="unpack", params={"mode": "status", "job": mj})
check("MCP status reports the result", r.get("ok") and r["job"]["status"] == "done" and r["job"]["result"]["counts"]["created"] == 2,
      str(r)[:300])
mm = member_rows(r["job"])
check("starter 2, the install token over MCP: members arrived via the MCP, admin-owned, no key credit",
      mm and all(m["channel"] == "mcp" and m["uploaded_by_user_id"] is None and m.get("uploaded_via_token_id") is None
                 for m in mm), [(m["channel"], m["uploaded_by_user_id"], m.get("uploaded_via_token_id")) for m in mm])
from core import mcp_tokens  # noqa: E402
from mcp_server import auth as mcp_auth  # noqa: E402
with actor.acting_as("user:up_editor"):
    key = mcp_tokens.create("windows claude")
ak = upload("key-crate.zip", zip_bytes([("k/one.txt", b"key one"), ("k/two.txt", b"key two")]))
tok = mcp_auth.IDENTITY.set({"actor": "user:up_editor", "user_id": ED_ID, "username": "up_editor",
                             "token_id": key["id"], "token_name": "windows claude"})
try:
    rk = mcp.constructicon_run_type_action(slug=ak, action="unpack", params={"mode": "start"})
finally:
    mcp_auth.IDENTITY.reset(tok)
check("starter 3, a personal MCP key: the job is queued as that user", rk.get("ok") and rk["job"]["status"] == "queued", str(rk)[:300])
kj = db.get_unpack_job(rk["job"]["id"])
check("... it stores channel mcp and the key's id", (kj["actor"], kj["channel"], kj["via_token_id"])
      == ("user:up_editor", "mcp", key["id"]), (kj["actor"], kj["channel"], kj["via_token_id"]))
unpack.run_job(kj["id"], run_background=NOOP)  # the web worker: no MCP identity in this context
km = member_rows(kj)
check("... the worker replays it: members arrived via the MCP, uploaded by the editor, credited to the key",
      len(km) == 2 and all(m["channel"] == "mcp" and m["uploaded_by_user_id"] == ED_ID
                           and m.get("uploaded_via_token_id") == key["id"] for m in km),
      [(m["channel"], m["uploaded_by_user_id"], m.get("uploaded_via_token_id")) for m in km])
credit = mcp_tokens.credit(ED_ID, km[0]["uploaded_via_token_id"])
check("... shown as \"<user>'s <key name>\"", credit and credit["text"].endswith("windows claude") and not credit["revoked"], credit)
kcard = db.get_project(unpack.job_status(kj["id"])["result"]["card"]["id"])
check("... the card it made is credited to the key too", kcard.get("created_via_token_id") == key["id"]
      and kcard.get("created_by_user_id") == ED_ID, (kcard.get("created_by_user_id"), kcard.get("created_via_token_id")))
check("... and so is every change-log row of the batch",
      all(r["via_token_id"] == key["id"] for r in q("SELECT via_token_id FROM audit_log WHERE batch_id = ?", kj["batch_id"])))
check("the replay doesn't leak: afterwards this context has no key and its own channel",
      actor.token_id_for() is None and actor.current_channel() == actor.channel_for_actor(actor.current_actor()))
cards.undo(kj["batch_id"])
r = mcp.constructicon_run_type_action(slug=am, action="unpack", params={"mode": "explode"})
check("MCP: an unknown mode is a clear refusal", r.get("ok") is False and "preview, start or status" in r["error"]["message"], str(r)[:200])
mb = unpack.job_status(mj)["batch_id"]
r = mcp.constructicon_undo(audit_id=mb)
check("MCP undo of the whole conversion (one batch)", r.get("ok") is not False and r.get("created_items_trashed")
      and not q("SELECT 1 FROM projects WHERE id = ?", unpack.job_status(mj)["result"]["card"]["id"]), str(r)[:300])
plain = object_types.TypeAction("plain", "Plain", lambda row: {"ran": True})
e = refusal(plain.run, {"slug": "x"}, {"surprise": "1"})
check("an action that takes no params refuses them (never silently dropped)", e is not None and e.code == "bad_request"
      and "surprise" in e.message)
check("... and runs without them", plain.run({"slug": "x"}, {}) == {"ran": True})

print()
if FAILS:
    print(f"FAILED: {', '.join(FAILS)}")
else:
    print("ALL PASS")
