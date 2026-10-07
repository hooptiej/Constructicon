#!/usr/bin/env python3
"""Install config (#562): a fresh install has nothing owner-specific; an old one sees no change.
Throwaway DBs, the real FastAPI app through TestClient, no server, no network:

    python scripts/test_install_config.py

1. Fresh install (empty DB, real init_db): nothing seeded (install_config, hobby_settings); the
   owner-archive migrations (v2c_3 AlienWhoop, v2c_4 AW canopy) are gone; uploads get the neutral
   "Owner -- manual/automated upload" label; the home page says "Owner"; publishing refuses with
   no_publish_target and /api/export/targets is {}; the export has a neutral title and footer.
2. Cards titled "AlienWhoop" / "AW canopy" queue no question, across a "restart" (init_db again).
3. No hobby shows the physical-piece fields until its setting is switched on, not even one named
   "Traditional Media", across a restart.
4. The admin page shows the setup banner; editing install config through the route updates every
   label (uploads, home, export, publish targets) and one undo puts it all back.
5. Validation: unknown key, bad target, wrong content type.
6. An install that predates #562 (a DB with content): the one-time seed reproduces today's
   hard-wired values byte for byte, keeps a saved pages_publish_targets value, and flags the
   existing "Traditional Media" hobby; a second run changes nothing.
Exits 1 if any check fails.
"""

import json
import os
import sqlite3
import sys
import tempfile
import types
from pathlib import Path

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("installcfg-", "fresh.db")
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)
sys.modules.setdefault("cairosvg", types.ModuleType("cairosvg"))  # native cairo is not needed here

from starlette.testclient import TestClient  # noqa: E402

from core import cards, db, hobbies, install_config, paths, site_export, storage  # noqa: E402
_testenv.assert_isolated()  # now as core actually resolved the paths
from web import app as webapp  # noqa: E402



FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(db.DB_PATH)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def restart():
    """What a container restart does to the DB: init_db (schema + pending migrations)."""
    install_config.clear_cache()
    db.init_db()


client = _testenv.client(webapp.app)
OWNERISH = ("hooptie", "alienwhoop", "traditional media")

# ---- 1. fresh install -----------------------------------------------------------------------
with client:  # runs the real startup hook (init_db, the stale sweep, ...) on the empty DB
    pass
check("fresh: install_config is empty", q("SELECT COUNT(*) FROM install_config")[0][0] == 0)
check("fresh: hobby_settings is empty", q("SELECT COUNT(*) FROM hobby_settings")[0][0] == 0)
names = [n for n, _fn in db.MIGRATIONS]
check("fresh: no v2c_3 / v2c_4 migration exists", not any(n.startswith(("v2c_3", "v2c_4")) for n in names), names)
check("fresh: card_migration has no AlienWhoop / canopy code",
      not hasattr(__import__("core.card_migration", fromlist=["x"]), "run_v2c_3")
      and not hasattr(__import__("core.card_migration", fromlist=["x"]), "run_v2c_4"))
check("fresh: the #562 migrations ran (and seeded nothing)",
      {"install_config_seed_562", "hobby_physical_piece_562"} <= {r[0] for r in q("SELECT name FROM schema_migrations")})
check("fresh: no IT-client seed rows at boot", q("SELECT COUNT(*) FROM clients")[0][0] == 0)
dump = json.dumps([q("SELECT * FROM install_config"), q("SELECT * FROM app_settings"),
                   q("SELECT name FROM blog_tags"), q("SELECT title FROM projects")]).lower()
check("fresh: nothing owner-specific stored", not any(w in dump for w in OWNERISH), dump[:200])

check("fresh: setup needed", install_config.setup_needed())
check("fresh: neutral labels", db.source_manual_upload() == "Owner — manual upload"
      and db.source_automated_upload() == "Owner — automated upload", db.source_manual_upload())

r = client.post("/api/upload", files={"file": ("note.txt", b"hello", "text/plain")})
check("fresh: web upload ok", r.status_code == 200, (r.status_code, r.text[:200]))
r2 = client.post("/api/upload", files={"file": ("note2.txt", b"hello again", "text/plain")},
                 headers={"X-Constructicon-Client": "desktop-app"})
techs = [t for (t,) in q("SELECT tech FROM capture_events ORDER BY id")]
check("fresh: uploads stamped with the neutral label",
      techs == ["Owner — manual upload", "Owner — automated upload"], techs)
check("fresh: the gallery groups them under 'Owner'", db.source_group(techs[0]) == "Owner")

home = client.get("/").text
check("fresh: home shows 'Owner' and 'O'", '<span class="gallery-drawer-name">Owner</span>' in home
      and '<div class="gallery-drawer-avatar">O</div>' in home)
check("fresh: no owner name anywhere on home", "hooptie" not in home.lower())

check("fresh: no publish targets", client.get("/api/export/targets").json() == {})
pub = client.post("/api/export/publish", json={"target": "live"})
check("fresh: publish refuses with no_publish_target", pub.status_code == 400
      and pub.json()["error"]["code"] == "no_publish_target" and "Admin" in pub.json()["error"]["message"],
      (pub.status_code, pub.text[:200]))

out = Path(TMP) / "export-fresh"
site_export.build_site({"project_slugs": [], "blog_entry_slugs": []}, out_dir=out)
idx = (out / "index.html").read_text(encoding="utf-8")
check("fresh: export title is neutral", "<title>Constructicon</title>" in idx, idx[:300])
check("fresh: export footer has no copyright holder", "<p>Generated by Constructicon.</p>" in idx
      and "hooptie" not in idx.lower())

# #576: a custom out_dir touches nothing else (no exports/current, no pruning); the default build
# is the one that refreshes `current` and keeps only the last two timestamped builds.
exp = paths.exports_dir()
check("#576: a custom out_dir build leaves exports/ alone (empty current/ from the app mount, no timestamped build)",
      [p.name for p in exp.iterdir()] == ["current"] and not any((exp / "current").iterdir()),
      [p.name for p in exp.iterdir()])
old_builds = [exp / "20200101_000001", exp / "20200101_000002", exp / "20200101_000003"]
for d in old_builds:
    d.mkdir()
(exp / "current").mkdir(exist_ok=True)
(exp / "current" / "marker.txt").write_text("keep me")
site_export.build_site({"project_slugs": [], "blog_entry_slugs": []}, out_dir=Path(TMP) / "export-custom2")
check("#576: a custom out_dir build doesn't replace current or prune old builds",
      (exp / "current" / "marker.txt").exists() and all(d.exists() for d in old_builds))
site_export.build_site({"project_slugs": [], "blog_entry_slugs": []})
check("#576: the default build refreshes current and prunes to the last two builds",
      not (exp / "current" / "marker.txt").exists() and (exp / "current" / "index.html").exists()
      and not old_builds[0].exists() and not old_builds[1].exists() and old_builds[2].exists())

admin = client.get("/admin").text
check("fresh: admin shows the setup banner", 'id="install-setup-banner"' in admin
      and "Finish setting up this install" in admin)
check("fresh: admin has the Install section", 'id="admin-install-section"' in admin)
cfg = client.get("/api/install-config").json()
check("fresh: GET /api/install-config", cfg["setup_needed"] is True and cfg["values"]["owner_name"] == ""
      and cfg["values"]["publish_targets"] == {} and {f["key"] for f in cfg["fields"]} == set(install_config.KEYS))
check("fresh: no secrets in the install config view", "token" not in json.dumps(cfg["values"]).lower())

# ---- 2. owner-archive questions never appear --------------------------------------------------
fam = cards.create("AlienWhoop").data["card"]
kid = cards.create("TinyWhoop").data["card"]
cards.nest(kid["id"], fam["id"])
cards.create("AW canopy project")
cards.create('AlienWhoop F7 "The Queen"')
restart()
restart()
qs = q("SELECT kind, post_slug FROM pending_decisions WHERE kind IN ('card_family_members', 'card_built_for')")
check("cards titled AlienWhoop / AW canopy queue no question across restarts", qs == [], qs)

# ---- 3. physical-piece fields are opt-in per hobby -----------------------------------------
tm = hobbies.create("Traditional Media").data["hobby"]
restart()
check("a hobby named 'Traditional Media' is not flagged on a fresh install",
      db.physical_piece_hobbies() == [] and not hobbies.shows_physical_piece(tm["id"]))
check("its page shows the setting off and no guide link",
      "Not shown" in client.get(f"/hobby/{tm['slug']}").text
      and "/guides/capture-physical-piece" not in client.get(f"/hobby/{tm['slug']}").text)
res = hobbies.set_physical_piece(tm["id"], True)
check("switching it on is one undoable change", res.batch_id and hobbies.shows_physical_piece(tm["id"]))
cards.undo(res.batch_id)
check("undo switches it off", not hobbies.shows_physical_piece(tm["id"]))

# ---- 4. editing the install config updates every label, one undo puts it back ----------------
body = {"owner_name": "Jane Q", "owner_label": "Jane Q (me)", "site_title": "jane.example",
        "copyright_holder": "jqueue", "publish_targets": {"live": {"repo": "janeq/janeq.github.io", "branch": "main"}}}
r = client.post("/api/install-config", json=body)
check("save install config", r.status_code == 200 and r.json()["batch_id"], (r.status_code, r.text[:300]))
batch = r.json()["batch_id"]
check("labels follow the config", db.source_manual_upload() == "Jane Q (me) — manual upload"
      and install_config.owner_initials() == "JQ" and not install_config.setup_needed())
check("the old neutral uploads still group (under 'Owner')", db.source_group(techs[0]) == "Owner")
client.post("/api/upload", files={"file": ("note3.txt", b"third", "text/plain")})
check("a new upload gets the owner's label",
      q("SELECT tech FROM capture_events ORDER BY id DESC LIMIT 1")[0][0] == "Jane Q (me) — manual upload")
home = client.get("/").text
check("home shows the owner", '<span class="gallery-drawer-name">Jane Q</span>' in home
      and '<div class="gallery-drawer-avatar">JQ</div>' in home)
check("the banner is gone", 'id="install-setup-banner"' not in client.get("/admin").text)
check("targets come from the config", client.get("/api/export/targets").json()
      == {"live": {"repo": "janeq/janeq.github.io", "branch": "main"}})
pub = client.post("/api/export/publish", json={"target": "live"})
check("publish now gets past the target check (to the token check)", pub.status_code == 400
      and "token" in pub.json()["error"]["message"].lower(), pub.text[:200])
out2 = Path(TMP) / "export-jane"
site_export.build_site({"project_slugs": [], "blog_entry_slugs": []}, out_dir=out2)
idx2 = (out2 / "index.html").read_text(encoding="utf-8")
check("export title + footer follow the config", "<title>jane.example</title>" in idx2
      and "<p>&copy; jqueue. Generated by Constructicon.</p>" in idx2)
same = client.post("/api/install-config", json={"owner_name": "Jane Q"})
check("saving the same value writes nothing", same.status_code == 200 and same.json()["batch_id"] is None)

u = client.post(f"/api/changes/{batch}/undo")
check("undo the save", u.status_code == 200, (u.status_code, u.text[:200]))
check("after undo: neutral again", db.source_manual_upload() == "Owner — manual upload"
      and install_config.setup_needed() and client.get("/api/export/targets").json() == {}
      and q("SELECT COUNT(*) FROM install_config")[0][0] == 0)
check("after undo: the banner is back", 'id="install-setup-banner"' in client.get("/admin").text)

# ---- 5. validation ---------------------------------------------------------------------------
bad = client.post("/api/install-config", json={"owner_email": "x"})
check("unknown key -> 400 bad_install_key", bad.status_code == 400 and bad.json()["error"]["code"] == "bad_install_key")
bad = client.post("/api/install-config", json={"publish_targets": {"live": {"repo": "not a repo"}}})
check("bad repo -> 400 bad_publish_target", bad.status_code == 400 and bad.json()["error"]["code"] == "bad_publish_target")
bad = client.post("/api/install-config", data={"owner_name": "x"})
check("form body -> 415", bad.status_code == 415)
check("nothing written by the refusals", q("SELECT COUNT(*) FROM install_config")[0][0] == 0)
check("pages_publish_targets is no longer an app setting", "pages_publish_targets" not in client.get("/api/settings").json())

# ---- 6. an install that predates #562 sees no change ---------------------------------------
db.DB_PATH = Path(TMP) / "legacy.db"
install_config.clear_cache()
db.init_db(migrate=False)  # schema only: the DB as an old install has it before this deploy
c = sqlite3.connect(db.DB_PATH)
c.execute("INSERT INTO capture_events (slug, timestamp, tech, tags, artifact_link) VALUES "
          "('old1', 0, 'Hooptie J (me) — manual upload', '[]', '/f/old1')")
c.execute("INSERT INTO blog_tags (name, slug, is_hobby, hobby_status) VALUES ('Traditional Media', 'traditional-media', 1, 'active')")
c.execute("INSERT INTO blog_tags (name, slug, is_hobby, hobby_status) VALUES ('Collecting', 'collecting', 1, 'active')")
c.execute("DELETE FROM schema_migrations WHERE name IN ('install_config_seed_562', 'hobby_physical_piece_562')")
c.commit()
c.close()
db.run_pending_migrations()
install_config.clear_cache()
check("legacy: manual label byte-identical", db.source_manual_upload() == "Hooptie J (me) — manual upload")
check("legacy: automated label byte-identical", db.source_automated_upload() == "Hooptie J (me) — automated upload")
check("legacy: grouping unchanged", db.source_group("Hooptie J (me) — manual upload") == "Hooptie J (me)"
      and db.source_groups()[:2] == ["Hooptie J (me)", "Claude"])
check("legacy: home name + initials unchanged", install_config.display_owner_name() == "Hooptie J"
      and install_config.owner_initials() == "HJ")
check("legacy: site title + footer unchanged", install_config.site_title() == "hooptiej.com"
      and install_config.copyright_holder() == "hooptiej")
check("legacy: default publish targets unchanged (and in the same order)",
      json.dumps(install_config.publish_targets()) == json.dumps(
          {"test": {"repo": "hooptiej/constructicon-export-test", "branch": "master"},
           "live": {"repo": "hooptiej/hooptiej.github.io", "branch": "master"}}))
check("legacy: no setup banner", not install_config.setup_needed())
check("legacy: the Traditional Media hobby keeps its fields, others don't",
      [h["name"] for h in db.physical_piece_hobbies()] == ["Traditional Media"])
seeded = q("SELECT op, actor FROM audit_log WHERE op LIKE 'migration_%562'")
check("legacy: the seed is logged as the migration (not undoable)",
      sorted(seeded) == [("migration_hobby_physical_piece_562", "migration"), ("migration_install_config_562", "migration")],
      seeded)
before = q("SELECT * FROM install_config ORDER BY key")
install_config.seed_existing_install(sqlite3.connect(db.DB_PATH))
check("legacy: a second seed changes nothing", q("SELECT * FROM install_config ORDER BY key") == before)

# a saved pages_publish_targets setting (it used to override the defaults) is carried over
db.DB_PATH = Path(TMP) / "legacy2.db"
install_config.clear_cache()
db.init_db(migrate=False)
c = sqlite3.connect(db.DB_PATH)
c.execute("INSERT INTO projects (title, slug, created_at, updated_at) VALUES ('A card', 'a-card', 0, 0)")
c.execute("INSERT INTO app_settings (key, value) VALUES ('pages_publish_targets', ?)",
          (json.dumps({"live": {"repo": "someone/site", "branch": "gh-pages"}}),))
c.execute("DELETE FROM schema_migrations WHERE name = 'install_config_seed_562'")
c.commit()
c.close()
db.run_pending_migrations()
install_config.clear_cache()
check("legacy: a saved pages_publish_targets value wins over the defaults",
      install_config.publish_targets() == {"live": {"repo": "someone/site", "branch": "gh-pages"}})

print("FAILED: %s" % FAILS if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
