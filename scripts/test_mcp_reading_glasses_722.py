#!/usr/bin/env python3
"""Batch 8, "MCP Reading Glasses": #722, #763, #789 and the MCP part of #556. Throwaway DB, no server,
no network.

    python scripts/test_mcp_reading_glasses_722.py

In-process (scripts/_testenv.py: temp DB + storage + exports), the MCP tool functions called directly.
Covers:
  1. #722 reading by the page: constructicon_get(slug, text_offset, text_chars) at the start, the middle
     and the end (next_offset walks the whole text, null at the end), text_offset alone, the default
     still whole, and refusals (offset past the end, negative, text_chars 0 / too big / not a number);
  2. #722 list trimming: a long type_metadata text (a write-up body) is cut in list results with
     type_metadata_truncated / type_metadata_cut_keys, a big list value left out; get_project's write-up
     is a preview with body_chars / body_truncated, full_writeup=true sends it whole; get keeps it whole;
  3. #722 tag paging: list_items(which="tag") pages in SQL (limit + cursor), the cursor walk returns every
     item once in order, the last page's next_cursor is null; bad limit / bad cursor / another list's
     cursor are refused; the retired get_posts_for_tag still answers a bare list;
  4. #763 append: append twice gives two dated blocks (Mountain Time) under the old notes, on an item and
     on a card; each append is one undoable batch; append with empty notes is refused, nothing written;
  5. #789 transcripts: a local fixture VTT (YouTube-style rolling auto captions and an SRT) parses into
     cues with no duplicated lines; clean_with_cues keeps offsets; cite maps a position to the cue's
     start and a &t= link; fields.transcript_vtt on a YouTube item stores the transcript with cue times
     (undoable), get with text_offset and a search hit cite the moment; a row with no cues gives null;
     transcript_vtt on a non-video item and a VTT with no speech are refused;
  6. #556: list_items(which="unfiled") = the /unfiled set (paged; filed and brand items left out),
     list_items(which="similar") = GET /api/image/{slug}/similar's look-alikes, caption_review list /
     accept / skip / retry (happy paths) and a refusal each (role, not_found, captions_disabled,
     a parameter that doesn't apply); the ACTION_ROLES rows match the web routes' roles.
Prints PASS/FAIL per check, then ALL PASS or FAILED: <names>.
"""

import base64
import json
import os
import sqlite3
import sys

import _testenv  # noqa: E402  (scripts/_testenv.py: temp DB + storage + exports, refuses otherwise)
TMP = _testenv.isolate("readingglasses722-")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

from core import actor, captions, cards, datefmt, db, membership, paths, roles, transcripts, users  # noqa: E402
_testenv.assert_isolated()

FAILS = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{str(extra)[:600]}]" if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)


def q(sql, *args):
    c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def err(out):
    return out.get("error", {}).get("code") if isinstance(out, dict) and out.get("ok") is False else None


def change_count():
    return q("SELECT COUNT(*) AS n FROM audit_log WHERE op IS NOT NULL")[0]["n"]


db.init_db()


def add_file(slug, name, text="", media_type="document", type_metadata=None, ts=None):
    (paths.storage_dir() / f"{slug}.txt").write_text(text or "x")
    db.insert_upload(slug, name, f"{slug}.txt", "tester", media_type=media_type, type_metadata=type_metadata)
    if text:
        db.set_extracted_text(slug, text)
        db.set_ocr_status(slug, "done")
    if ts is not None:
        c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
        c.execute("UPDATE capture_events SET timestamp = ? WHERE slug = ?", (ts, slug))
        c.commit()
        c.close()


# --- fixture -----------------------------------------------------------------------------
LONG = "".join(f"[{i:05d}] " + "lorem ipsum dolor sit amet " for i in range(2000))  # ~70,000 chars
add_file("manual", "big-manual.pdf", LONG)
BODY = "# Build log\n\n" + ("The rover got a new gearbox and a lot of words about it. " * 200)
add_file("writeup", "Rover write-up", BODY, type_metadata={"body": BODY, "small": "short value",
                                                         "biglist": list(range(800))})
for i in range(7):
    add_file(f"tagged{i}", f"tagged-{i}.txt", f"tagged file {i}", ts=1_700_000_000 + i)
add_file("filed1", "filed.txt", "on a card")
add_file("brand1", "logo.txt", "brand")
c = sqlite3.connect(os.environ["CONSTRUCTICON_DB_PATH"])
c.execute("UPDATE capture_events SET is_brand_asset = 1 WHERE slug = 'brand1'")
c.commit()
c.close()

with actor.acting_as(actor.ACTOR_UI):
    cards.create("Rover")
    ROVER = next(p for p in db.list_projects() if p["title"] == "Rover")
    membership.add_files(ROVER["id"], ["filed1", "writeup"], **membership.UI_EFFECTS)
    cards.update(ROVER["id"], writeup_slug="writeup")
    users.create_user("vic", "viewer-password-722", roles.VIEWER)
    users.create_user("eddie", "editor-password-722", roles.EDITOR)

from mcp_server import auth as mcp_auth  # noqa: E402
from mcp_server import server as S  # noqa: E402  (after the DB env is set)


def as_user(name, fn, *a, **kw):
    token = mcp_auth.IDENTITY.set({"actor": f"user:{name}", "user_id": None, "username": name})
    try:
        return fn(*a, **kw)
    finally:
        mcp_auth.IDENTITY.reset(token)


with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_item_tags("tagged0", ["Robots722"])
    for i in range(1, 7):
        S.constructicon_item_tags(f"tagged{i}", ["Robots722"])

# --- 1. #722 reading by the page ---------------------------------------------------------
print("--- 1. constructicon_get by the page ---")
whole = S.constructicon_get("manual")
check("default get: the whole text, as before", whole["extracted_text"] == LONG and "next_offset" not in whole)
p1 = S.constructicon_get("manual", text_chars=20000)
check("first page: 20000 chars from 0, total, truncated, next_offset",
      p1["extracted_text"] == LONG[:20000] and p1["extracted_text_chars"] == len(LONG) and p1["text_offset"] == 0
      and p1["extracted_text_truncated"] is True and p1["next_offset"] == 20000, {k: p1.get(k) for k in
                                                                                 ("text_offset", "next_offset")})
check("a windowed read on a non-video says null timestamps", p1["timestamp_seconds"] is None and p1["timestamp_url"] is None)
mid = S.constructicon_get("manual", text_offset=31337, text_chars=1000)
check("middle page: exactly that slice", mid["extracted_text"] == LONG[31337:32337] and mid["next_offset"] == 32337)
walked, off, pages = "", 0, 0
while off is not None and pages < 20:
    pg = S.constructicon_get("manual", text_offset=off, text_chars=20000)
    walked += pg["extracted_text"]
    off = pg["next_offset"]
    pages += 1
check("next_offset walks the whole text, null at the end", walked == LONG and off is None and pages == 4, (pages, len(walked)))
end = S.constructicon_get("manual", text_offset=len(LONG) - 10, text_chars=500)
check("end page: the last 10 chars, next_offset null", end["extracted_text"] == LONG[-10:] and end["next_offset"] is None)
rest = S.constructicon_get("manual", text_offset=len(LONG) - 100)
check("text_offset alone: from there to the end", rest["extracted_text"] == LONG[-100:] and rest["next_offset"] is None)
check("offset past the end: bad_request naming the length",
      err(S.constructicon_get("manual", text_offset=len(LONG), text_chars=10)) == "bad_request"
      and f"{len(LONG):,}" in S.constructicon_get("manual", text_offset=len(LONG) + 5)["error"]["message"])
check("negative offset: bad_request", err(S.constructicon_get("manual", text_offset=-1, text_chars=10)) == "bad_request")
check("text_chars 0: bad_request", err(S.constructicon_get("manual", text_chars=0)) == "bad_request")
check("text_chars over the cap: bad_request", err(S.constructicon_get("manual", text_chars=S.GET_WINDOW_MAX_CHARS + 1)) == "bad_request")
check("text_chars not a number: bad_request", err(S.constructicon_get("manual", text_chars="lots")) == "bad_request")
check("unknown item: still not_found", err(S.constructicon_get("nope", text_chars=10)) == "not_found")

# --- 2. #722 list trimming ---------------------------------------------------------------
print("--- 2. type_metadata and write-ups are trimmed in lists ---")
with actor.acting_as(actor.ACTOR_MCP):
    gp = S.constructicon_get_project(ROVER["slug"])
wu = gp["writeup"]
check("get_project write-up: body cut to a preview, body_chars + body_truncated",
      wu["body_truncated"] is True and wu["body_chars"] == len(BODY)
      and len(wu["type_metadata"]["body"]) == S.TEXT_PREVIEW_CHARS + 1 and wu["type_metadata_truncated"] is True,
      {k: wu.get(k) for k in ("body_chars", "body_truncated", "type_metadata_cut_keys")})
check("get_project write-up: a big list value is left out and named", "biglist" not in wu["type_metadata"]
      and set(wu["type_metadata_cut_keys"]) == {"body", "biglist"} and wu["type_metadata"]["small"] == "short value")
check("get_project write-up: its extracted_text is a preview too", wu["extracted_text_truncated"] is True
      and wu["extracted_text_chars"] == len(BODY))
full = S.constructicon_get_project(ROVER["slug"], full_writeup=True)["writeup"]
check("full_writeup=true: the write-up whole", full["type_metadata"]["body"] == BODY and full["body_truncated"] is False
      and full["extracted_text"] == BODY and full["type_metadata"]["biglist"] == list(range(800)))
item_in_list = next(i for i in gp["items"] if i["slug"] == "writeup")
check("get_project items: the same trimming", item_in_list["type_metadata_truncated"] is True
      and "biglist" not in item_in_list["type_metadata"])
small_item = next(i for i in gp["items"] if i["slug"] == "filed1")
check("an item with small metadata: type_metadata_truncated false, no cut keys",
      small_item["type_metadata_truncated"] is False and "type_metadata_cut_keys" not in small_item)
srch = S.constructicon_search("gearbox")
hit = next((r for r in srch if r["slug"] == "writeup"), {})
check("search results trim it too", hit.get("type_metadata_truncated") is True and "biglist" not in hit.get("type_metadata", {}))
one = S.constructicon_get("writeup")
check("constructicon_get (one item) keeps type_metadata whole", one["type_metadata"]["body"] == BODY
      and one["type_metadata"]["biglist"] == list(range(800)) and "type_metadata_truncated" not in one)
win = S.constructicon_get("writeup", text_chars=100)
check("a windowed get trims type_metadata (it would repeat the text)", win["type_metadata_truncated"] is True
      and "biglist" in win["type_metadata_cut_keys"])

# --- 3. #722 tag paging ------------------------------------------------------------------
print("--- 3. tag reads are paged ---")
first = S.constructicon_list_items(which="tag", tag="Robots722", limit=3)
check("page 1: 3 items, more, a cursor", len(first["items"]) == 3 and first["more"] is True and first["next_cursor"]
      and first["limit"] == 3, first.get("next_cursor"))
seen, cur, n = [], None, 0
while n < 10:
    pg = S.constructicon_list_items(which="tag", tag="Robots722", limit=3, cursor=cur)
    seen += [i["slug"] for i in pg["items"]]
    cur = pg["next_cursor"]
    n += 1
    if cur is None:
        break
check("the cursor walk returns every tagged item once, newest first, then stops",
      seen == [f"tagged{i}" for i in range(6, -1, -1)] and cur is None and n == 3, (seen, n))
default = S.constructicon_list_items(which="tag", tag="Robots722")
check("default limit is 100: all 7, no next page", len(default["items"]) == 7 and default["next_cursor"] is None
      and default["more"] is False and default["limit"] == S.LIST_PAGE_DEFAULT)
check("limit 0 / over 500: bad_limit", err(S.constructicon_list_items(which="tag", tag="Robots722", limit=0)) == "bad_limit"
      and err(S.constructicon_list_items(which="tag", tag="Robots722", limit=501)) == "bad_limit")
check("a cursor that isn't ours: bad_cursor", err(S.constructicon_list_items(which="tag", tag="Robots722", cursor="!!nope")) == "bad_cursor")
forged = base64.urlsafe_b64encode(json.dumps({"v": "unfiled", "o": 3}).encode()).decode().rstrip("=")
check("another list's cursor: cursor_view_mismatch",
      err(S.constructicon_list_items(which="tag", tag="Robots722", cursor=forged)) == "cursor_view_mismatch")
check("slug= doesn't apply to which=tag: refused",
      err(S.constructicon_list_items(which="tag", tag="Robots722", slug="manual")) == "bad_request")
alias = S.constructicon_get_posts_for_tag("Robots722")
check("retired get_posts_for_tag: still a bare list", isinstance(alias, list) and len(alias) == 7)

# --- 4. #763 append mode -----------------------------------------------------------------
print("--- 4. set_agent_notes(append=true) ---")
today = datefmt.iso_day(__import__("time").time())
with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_set_agent_notes("filed1", "Owner said it came from the swap meet.")
    n0 = change_count()
    a1 = S.constructicon_set_agent_notes("filed1", "First added story.", append=True)
    a2 = S.constructicon_set_agent_notes("filed1", "Second added story.", append=True)
want = (f"Owner said it came from the swap meet.\n\n--- {today} ---\nFirst added story.\n\n"
        f"--- {today} ---\nSecond added story.")
check("item: two appends = two dated blocks under the old notes", a2["agent_notes"] == want, a2.get("agent_notes"))
check("item: each append one batch", a1["batch_id"] and a2["batch_id"] and a1["batch_id"] != a2["batch_id"]
      and change_count() == n0 + 2)
with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_undo(a2["batch_id"])
check("item: undo the second append leaves the first",
      S.constructicon_get("filed1", include=["agent_notes"])["agent_notes"] ==
      f"Owner said it came from the swap meet.\n\n--- {today} ---\nFirst added story.")
n1 = change_count()
check("append with empty notes: bad_request, nothing written",
      err(S.constructicon_set_agent_notes("filed1", "   ", append=True)) == "bad_request"
      and err(S.constructicon_set_agent_notes("filed1", None, append=True)) == "bad_request" and change_count() == n1)
with actor.acting_as(actor.ACTOR_MCP):
    c1 = S.constructicon_set_agent_notes(card=ROVER["slug"], notes="Card story one.", append=True)
    c2 = S.constructicon_set_agent_notes(card=ROVER["id"], notes="Card story two.", append=True)
check("card: the first append on empty notes is just the block; the second adds another",
      c1["agent_notes"] == f"--- {today} ---\nCard story one." and
      c2["agent_notes"] == f"--- {today} ---\nCard story one.\n\n--- {today} ---\nCard story two.", c2.get("agent_notes"))
with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_undo(c2["batch_id"])
check("card: undo restores the one-block notes", db.get_card_agent_notes(ROVER["id"]) == f"--- {today} ---\nCard story one.")
check("card: append with empty notes refused", err(S.constructicon_set_agent_notes(card=ROVER["slug"], notes="", append=True)) == "bad_request")
check("cards.appended_notes uses the Mountain Time day",
      cards.appended_notes("", "x", epoch=1_700_000_000 + 6 * 3600 - 1) ==
      f"--- {datefmt.iso_day(1_700_000_000 + 6 * 3600 - 1)} ---\nx")

# --- 5. #789 transcripts with cue times --------------------------------------------------
print("--- 5. transcript cue timestamps ---")
SPEECH = ["today we take apart the gearbox and look at every shim",
          "the first thing you notice is the bearing on the input shaft",
          "then we measure the backlash with a dial indicator on the ring gear",
          "finally we put it back together with fresh grease and new seals"]
VTT = ("WEBVTT\nKind: captions\nLanguage: en\n\n"
       "00:00:01.000 --> 00:00:04.000 align:start position:0%\n"
       f"{SPEECH[0].split(' the gearbox')[0]}<00:00:02.000><c> the gearbox and look at every shim</c>\n\n"
       "00:00:04.000 --> 00:00:04.010\n"
       f"{SPEECH[0]}\n\n"
       "00:00:04.010 --> 00:01:05.500\n"
       f"{SPEECH[0]}\n{SPEECH[1]}\n\n"
       "NOTE this is a comment block\n\n"
       "01:05.500 --> 01:10.000\n"
       f"{SPEECH[1]}\n{SPEECH[2]}\n\n"
       "00:02:00.250 --> 00:02:04.000\n"
       f"<v Host>{SPEECH[3]}</v>\n")
cues = transcripts.parse_vtt(VTT)
check("VTT: four cues, start times read (h:m:s and m:s forms)", [c[0] for c in cues] == [1.0, 4.01, 65.5, 120.25], cues)
check("VTT: rolling repeats dropped, tags removed", [c[1] for c in cues] == SPEECH, cues)
SRT = "1\n00:00:01,500 --> 00:00:03,000\nhello there\n\n2\n00:00:03,000 --> 00:00:05,000\ngeneral kenobi\n"
check("SRT: comma times and counters", transcripts.parse_vtt(SRT) == [(1.5, "hello there"), (3.0, "general kenobi")])
text, words, cq = transcripts.clean_with_cues(["[Music]", "hello world", "um", "second line"], [0, 2.5, 3, 7.5])
check("clean_with_cues: offsets point at the kept snippets", text == "hello world second line"
      and cq == [[2.5, 0], [7.5, 12]], (text, cq))
long_vtt = "WEBVTT\n\n" + "".join(f"00:00:{i:02d}.000 --> 00:00:{i:02d}.900\n{s} part {i}\n\n" for i, s in
                                    enumerate(SPEECH * 3))
fields = transcripts.fields_from_vtt(long_vtt)
check("fields_from_vtt: found, cue list, no language", fields["transcript_status"] == "found"
      and len(fields["transcript_cues"]) == 12 and fields["transcript_language"] is None, fields.get("transcript_status"))
row = {"type_metadata": fields, "extracted_text": "Description:\nhi\n\nTranscript:\n" + fields["transcript"],
       "external_url": "https://www.youtube.com/watch?v=qFEuYUfauNY"}
pos = row["extracted_text"].index("part 5")
st = transcripts.cite(row, pos)
check("cite: a position maps to its cue's start and a &t= link",
      st["timestamp_seconds"] == 5.0 and st["timestamp_url"] == "https://www.youtube.com/watch?v=qFEuYUfauNY&t=5s", st)
check("cite: a position in the description gives nulls", transcripts.cite(row, 3)["timestamp_seconds"] is None)
check("cite: a row with no cues gives nulls", transcripts.cite({"type_metadata": {"transcript": "abc"},
                                                                "extracted_text": "abc"}, 1)["timestamp_seconds"] is None)
try:
    transcripts.fields_from_vtt("WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n[Music]\n")
    check("a VTT with no speech is refused", False)
except Exception as e:  # noqa: BLE001
    check("a VTT with no speech is refused (bad_transcript)", getattr(e, "code", None) == "bad_transcript", e)

db.insert_content("vid1", "tester", "youtube", external_url="https://www.youtube.com/watch?v=qFEuYUfauNY",
                  type_metadata={"description": "Low Level takes the viral repo apart."})
db.set_extracted_text("vid1", "Description:\nLow Level takes the viral repo apart.", source="description")
db.set_ocr_status("vid1", "done")
before_vid = S.constructicon_get("vid1")
check("an old video item (no cues): a windowed get answers null timestamps",
      S.constructicon_get("vid1", text_offset=0, text_chars=10)["timestamp_seconds"] is None)
with actor.acting_as(actor.ACTOR_MCP):
    up = S.constructicon_update("vid1", fields={"transcript_vtt": long_vtt})
got = S.constructicon_get("vid1")
check("fields.transcript_vtt: transcript + cue times stored, in the searchable text",
      got["type_metadata"]["transcript_status"] == "found" and got["type_metadata"]["transcript_cues"]
      and "Transcript:" in got["extracted_text"] and "part 11" in got["extracted_text"], got["extracted_text"][:200])
at = got["extracted_text"].index("part 7")
gw = S.constructicon_get("vid1", text_offset=at, text_chars=200)
check("get(text_offset in the transcript): timestamp_seconds + &t= url",
      gw["timestamp_seconds"] == 7.0 and gw["timestamp_url"].endswith("v=qFEuYUfauNY&t=7s"), {k: gw.get(k) for k in
                                                                                         ("timestamp_seconds", "timestamp_url")})
sh = next((r for r in S.constructicon_search("dial indicator") if r["slug"] == "vid1"), {})
check("search hit in a transcript carries its moment", sh.get("timestamp_seconds") == 2.0
      and (sh.get("timestamp_url") or "").endswith("&t=2s"), {k: sh.get(k) for k in ("timestamp_seconds", "timestamp_url")})
check("the search result trims the stored transcript in type_metadata",
      "transcript" in sh.get("type_metadata_cut_keys", []) and sh.get("type_metadata_truncated") is True,
      sh.get("type_metadata_cut_keys"))
check("a long cue list is left out of a list result", transcripts.FOUND == "found" and
      S._trim_meta({"transcript_cues": [[float(i), i * 40] for i in range(400)]})[1] == ["transcript_cues"])
with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_undo(up["batch_id"])
after_undo = S.constructicon_get("vid1")
check("undo takes the transcript and its cues back out", after_undo["extracted_text"] == before_vid["extracted_text"]
      and "transcript_cues" not in after_undo["type_metadata"], after_undo["type_metadata"])
n2 = change_count()
check("transcript_vtt on a non-video item: bad_transcript, nothing written",
      err(S.constructicon_update("manual", fields={"transcript_vtt": long_vtt})) == "bad_transcript" and change_count() == n2)
check("transcript_vtt with no timed captions: bad_transcript",
      err(S.constructicon_update("vid1", fields={"transcript_vtt": "just some words"})) == "bad_transcript")

# --- 6. #556 unfiled, similar, caption review --------------------------------------------
print("--- 6. unfiled, similar, caption_review ---")
unf = S.constructicon_list_items(which="unfiled", limit=500)
slugs = {i["slug"] for i in unf["items"]}
web_set = {r["slug"] for r in db.list_unfiled_items(limit=10_000)}
check("unfiled = the /unfiled page's set (filed and brand items left out)", slugs == web_set
      and "filed1" not in slugs and "brand1" not in slugs and "manual" in slugs, sorted(slugs ^ web_set))
walk, cur = [], None
for _ in range(20):
    pg = S.constructicon_list_items(which="unfiled", limit=4, cursor=cur)
    walk += [i["slug"] for i in pg["items"]]
    cur = pg["next_cursor"]
    if cur is None:
        break
check("unfiled pages walk to the same list", walk == [i["slug"] for i in unf["items"]] and cur is None)
tag_cur = S.constructicon_list_items(which="tag", tag="Robots722", limit=2)["next_cursor"]
check("a tag cursor on unfiled: cursor_view_mismatch", err(S.constructicon_list_items(which="unfiled", cursor=tag_cur)) == "cursor_view_mismatch")

PH = "f0e1d2c3b4a59687"
for s in ("tagged0", "tagged1", "tagged2"):
    db.set_perceptual_hash(s, PH)
sim = S.constructicon_list_items(which="similar", slug="tagged0")
check("similar: look-alikes with reason and score", {r["slug"] for r in sim} == {"tagged1", "tagged2"}
      and all(r["similarity_reason"] == "visual" and r["similarity_score"] == 1.0 for r in sim), sim)
check("similar: needs slug", err(S.constructicon_list_items(which="similar")) == "bad_request")
check("similar: unknown slug is not_found", err(S.constructicon_list_items(which="similar", slug="nope")) == "not_found")
check("similar: limit doesn't apply, refused", err(S.constructicon_list_items(which="similar", slug="tagged0", limit=5)) == "bad_request")

add_file("cap1", "photo1.jpg", media_type="image",
         type_metadata={"auto_caption": "A green monster truck on a workbench.", "auto_caption_status": "done",
                        "auto_caption_model": "moondream", "auto_caption_title": "Green monster truck"})
add_file("cap2", "photo2.jpg", media_type="image",
         type_metadata={"auto_caption": "A dachshund asleep on a couch.", "auto_caption_status": "done",
                        "auto_caption_model": "moondream"})
LONG_CAP = "A very long caption about a rover on a bench. " * 20  # ~940 chars
for i in range(5):
    add_file(f"capq{i}", f"queue{i}.jpg", media_type="image", ts=1_600_000_000 + i,
             type_metadata={"auto_caption": LONG_CAP, "auto_caption_status": "done", "auto_caption_model": "moondream"})


def queue_slugs():
    out, cur = [], None
    for _ in range(50):
        pg = S.constructicon_caption_review(limit=500, cursor=cur)
        out += [r["slug"] for r in pg["items"]]
        cur = pg["next_cursor"]
        if cur is None:
            return out
    return out


lst = S.constructicon_caption_review()
web_q = captions.review_queue()
check("caption_review list: paged shape, default limit 50", set(lst) == {"items", "next_cursor", "more", "limit"}
      and lst["limit"] == S.CAPTION_REVIEW_PAGE_DEFAULT and lst["next_cursor"] is None, sorted(lst))
check("caption_review list = the review page's queue (same slugs, same order)",
      [r["slug"] for r in lst["items"]] == [r["slug"] for r in web_q] and {"cap1", "cap2"} <= set(queue_slugs()))
q0 = next(r for r in lst["items"] if r["slug"] == "capq0")
check("caption_review list: a long caption is a preview with caption_chars / caption_truncated",
      q0["caption_truncated"] is True and q0["caption_chars"] == len(LONG_CAP.strip())
      and q0["caption"] == LONG_CAP.strip()[:S.TEXT_PREVIEW_CHARS] + "…", q0)
c2 = next(r for r in lst["items"] if r["slug"] == "cap2")
check("caption_review list: a short caption is whole, truncated false", c2["caption_truncated"] is False
      and c2["caption"] == "A dachshund asleep on a couch.")
check("the web queue still carries the whole caption",
      next(r for r in web_q if r["slug"] == "capq0")["caption"] == LONG_CAP.strip())
walk, cur, pages = [], None, 0
while pages < 20:
    pg = S.constructicon_caption_review(limit=2, cursor=cur)
    walk += [r["slug"] for r in pg["items"]]
    cur = pg["next_cursor"]
    pages += 1
    if cur is None:
        break
check("caption_review list: limit=2 pages walk the whole queue once, in order",
      walk == [r["slug"] for r in web_q] and pages == (len(web_q) + 1) // 2, (walk, pages))
check("caption_review list: bad limit / bad cursor / another list's cursor refused",
      err(S.constructicon_caption_review(limit=501)) == "bad_limit"
      and err(S.constructicon_caption_review(cursor="!!nope")) == "bad_cursor"
      and err(S.constructicon_caption_review(cursor=tag_cur)) == "cursor_view_mismatch")
check("limit does not apply to accept: refused", err(S.constructicon_caption_review(action="accept", slug="cap1", limit=5)) == "bad_request")
check("caption_review list is a viewer read", isinstance(as_user("vic", S.constructicon_caption_review).get("items"), list))
with actor.acting_as(actor.ACTOR_MCP):
    acc = S.constructicon_caption_review(action="accept", slug="cap1")
check("accept: the suggestion becomes description + short title, one batch",
      acc["item"]["content_description"] == "A green monster truck on a workbench."
      and acc["item"]["short_title"] == "Green monster truck" and acc["batch_id"])
check("accept: it left the queue", "cap1" not in set(queue_slugs()))
with actor.acting_as(actor.ACTOR_MCP):
    S.constructicon_undo(acc["batch_id"])
check("accept: undoable (back in the queue)", "cap1" in set(queue_slugs()))
with actor.acting_as(actor.ACTOR_MCP):
    sk = S.constructicon_caption_review(action="skip", slug="cap2")
check("skip: out of the queue, one batch", sk["batch_id"] and "cap2" not in set(queue_slugs()))
check("skip: what= must be one of all/caption/title", err(S.constructicon_caption_review(action="skip", slug="cap2", what="nah")) is not None)
check("accept by a viewer: forbidden (editor needed)",
      err(as_user("vic", S.constructicon_caption_review, action="accept", slug="cap1")) == "forbidden")
check("accept of an unknown item: not_found", err(S.constructicon_caption_review(action="accept", slug="nope")) == "not_found")
check("accept needs slug", err(S.constructicon_caption_review(action="accept")) == "bad_request")
check("list with a slug: refused (doesn't apply)", err(S.constructicon_caption_review(action="list", slug="cap1")) == "bad_request")
queued = []
real_enqueue, real_disabled = captions.enqueue_caption, captions.DISABLED
captions.enqueue_caption = lambda slug, start_step=0, cascade=True: queued.append((slug, start_step, cascade))
try:
    captions.DISABLED = True
    check("retry with captions off: captions_disabled, nothing queued",
          err(S.constructicon_caption_review(action="retry", slug="cap2")) == "captions_disabled" and queued == [])
    captions.DISABLED = False
    rt = S.constructicon_caption_review(action="retry", slug="cap2")
    check("retry: queued for the web worker, status pending", queued == [("cap2", 0, True)]
          and rt["item"]["type_metadata"]["auto_caption_status"] == "pending", queued)
    S.constructicon_caption_review(action="retry", slug="cap2", advance=True)
    check("retry advance=true: the next step, no cascade", queued[-1] == ("cap2", 1, False), queued)
    check("retry of a non-caption type: bad_request", err(S.constructicon_caption_review(action="retry", slug="manual")) == "bad_request")
finally:
    captions.enqueue_caption, captions.DISABLED = real_enqueue, real_disabled

# the role rows match the web routes' labels
from web import app as webapp  # noqa: E402,F401  (builds the routers)
from web import roles as web_roles  # noqa: E402


def route_role(method, path):
    for r in web_roles.flat_routes(webapp.app.routes):
        if getattr(r, "path", None) == path and method in (getattr(r, "methods", None) or ()):
            return web_roles.label_of(r)
    return "(no such route)"


want_roles = {("constructicon_caption_review", "list"): roles.VIEWER, ("constructicon_caption_review", "accept"): roles.EDITOR,
              ("constructicon_caption_review", "skip"): roles.EDITOR, ("constructicon_caption_review", "retry"): roles.EDITOR,
              ("constructicon_list_items", "unfiled"): roles.VIEWER, ("constructicon_list_items", "similar"): roles.VIEWER}
check("ACTION_ROLES rows for the new actions match the web routes (viewer reads, editor writes)",
      all(S.ACTION_ROLES.get(k) == v for k, v in want_roles.items()), {k: S.ACTION_ROLES.get(k) for k in want_roles})
web_want = {("GET", "/api/captions/unreviewed"): roles.VIEWER, ("POST", "/api/captions/{slug}/accept"): roles.EDITOR,
            ("POST", "/api/captions/{slug}/skip"): roles.EDITOR, ("POST", "/api/image/{slug}/caption"): roles.EDITOR,
            ("GET", "/api/image/{slug}/similar"): roles.VIEWER}
got_web = {k: route_role(*k) for k in web_want}
check("...and the web routes carry the same roles", all(got_web[k] == v for k, v in web_want.items()), got_web)

print()
if FAILS:
    print("FAILED: " + ", ".join(FAILS))
    sys.exit(1)
print("ALL PASS")
