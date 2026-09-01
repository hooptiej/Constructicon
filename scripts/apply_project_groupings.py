"""Issue #51: create the 8 confirmed projects from issue #10 and attach the
correct re-imported content rows (#21/#22) to each.

Background
----------
#21 (backfill_from_hooptiej_site.py) and #22
(import_new_youtube_from_channel_rss.py) re-imported all of PR #7's original
content via POST /api/content, but deliberately left `projects`/
`project_items` untouched pending the owner's confirmation of PR #7's
groupings. That confirmation happened on issue #10 (see
https://github.com/hooptiej/Constructicon/issues/10#issuecomment-5498225811
and the follow-up owner comment
https://github.com/hooptiej/Constructicon/issues/10#issuecomment-5501287114),
including an explicit ruling that "The Corvus Series" grouping stays as-is
even though two of its three videos may be the same build revision — they're
still 2 distinct videos and belong grouped together either way.

This script creates those 8 projects and attaches their members now that the
content actually exists in the target instance's database.

Matching strategy
------------------
Every project member is matched against the target's live database, never
assumed by slug (re-imported slugs are server-random per #46/#50's rework):

  - Video members: matched by YouTube video ID, extracted from each row's
    `external_url` via core.object_types.extract_youtube_id — the same
    extraction web/app.py's embed player and thumbnail URL both use, so
    there's exactly one source of truth for "what video ID does this row
    represent" (see that module's docstring for why).

  - Blog-post members (GammaAtom's and The Scorpion's non-video items) are
    trickier than a plain title match, because `capture_events` never stores
    a post's actual title -- content_description holds the *excerpt*
    backfill_from_hooptiej_site.py scraped (post["excerpt"]), not its title
    (see that script's parse_blog_index/classify_and_insert_post). Confirmed
    by inspecting the test container's real rows: e.g. the "Hooptie J's
    GammaAtom" post's content_description is "A RotorX Atom 83 Mini racequad
    build. 2\" of absolute insanity." -- not the title string at all.

    What backfill_from_hooptiej_site.py DOES preserve reliably for a plain
    written post is its original blog URL in `external_url`
    (https://hooptiej.com/blog/<slug>.html) -- so a post matches by the
    hyphenated URL slug its title corresponds to on the live site (visible
    directly in issue #10's own comment links, e.g. "GammaAtom Update: camera
    mount" -> .../blog/gammaatom-update-camera-mount.html).

    Two of the five named blog posts ("Atom mini 83: Flight Time" and
    "ReMaidening the Scorpion.") are exceptions: their post page itself
    embeds a YouTube iframe, so classify_and_insert_post's iframe branch (see
    that script's docstring) imported them as media_type='youtube' with
    external_url set to the *video's* watch URL, not the blog post's own URL
    -- the blog URL is lost entirely, so slug-matching can't find them.
    Verified against the actual source pages in hooptiej/hooptiej.github.io
    (github.com/hooptiej/hooptiej.github.io/blob/main/blog/
    atom-mini-83-flight-time.html and .../remaidening-the-scorpion.html):
    each embeds exactly one YouTube video (wZJBGKiKWQg and YQl5Rdh1vYc
    respectively), and each row's content_description in the target DB is a
    verbatim match of that page's body paragraph. So these two are matched
    the same way a plain video is -- by their (known, page-confirmed) video
    ID -- rather than by a blog URL slug or fuzzy text matching.

    BLOG_POSTS below records, per title, whichever of {blog_slug,
    youtube_id} actually identifies its row; matching tries blog_slug (an
    external_url substring match) first, then youtube_id, and reports a
    clear failure by title if neither is found.

Idempotency
-----------
Safe to re-run against the same or a different target at any time:
  - A project is only created if no existing project has that exact title
    (case-insensitive) -- re-running finds it and reuses it rather than
    creating "Tension Biped 2".
  - db.add_item_to_project and db.attach_tags are both INSERT OR IGNORE
    already (see core/db.py) -- attaching an already-attached item/tag is a
    silent no-op, not a duplicate or an error.
  - Every member lookup re-derives from the live DB on each run, so it never
    depends on state this script itself wrote earlier.

What this does NOT do
----------------------
Create content rows. This assumes #21/#22 (or an equivalent import) already
populated the target's capture_events table -- if a video/post genuinely
isn't in that instance's database yet (e.g. #22 hasn't been run against
production yet, so its 2 gap videos won't exist there until it has), this
script reports that member as NOT FOUND by name/ID rather than silently
skipping or inventing a row for it. Re-run this script after the missing
import has happened; already-applied groupings are unaffected.

Usage
-----
    python scripts/apply_project_groupings.py --base-url http://localhost:80

    Must run somewhere that can also see the target's database at
    core.db.DB_PATH (e.g. docker exec into the app's own container) --
    project *creation* goes through the target's own POST /api/projects (the
    same real code path api_create_project uses, including its
    get_or_create_tag call -- see web/app.py), exactly like #21/#46/#22 route
    their content-row creation through POST /api/content. Finding existing
    projects/members and attaching items/tags has no HTTP equivalent (no
    route edits an existing project's membership) and goes straight to the
    same database file the target instance itself reads and writes, via
    core.db -- same "share a DB file, but only for the parts with no route"
    approach as the two scripts before it.

    To run this against production once #22 has also been applied there:
        python scripts/apply_project_groupings.py --base-url http://<prod-host>

    (No production run is performed by this script's author -- see the PR
    description for the review/merge process this should go through first.)
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db, object_types  # noqa: E402


def video(video_id):
    return {"kind": "video", "video_id": video_id}


def blog_post(title, blog_slug=None, youtube_id=None):
    assert blog_slug or youtube_id, f"blog_post {title!r} needs blog_slug and/or youtube_id"
    return {"kind": "blog_post", "title": title, "blog_slug": blog_slug, "youtube_id": youtube_id}


# The 8 confirmed groupings from issue #10 (see module docstring for the
# blog_post matching caveats on GammaAtom's and The Scorpion's non-video
# members).
PROJECTS = [
    {
        "title": "Tension Biped",
        "members": [video("v5KVF-_3Txo"), video("2xEu-t-PUyU")],
    },
    {
        "title": "GammaAtom",
        "members": [
            blog_post("Hooptie J's GammaAtom", blog_slug="hooptie-js-gammaatom"),
            blog_post("GammaAtom Update: camera mount", blog_slug="gammaatom-update-camera-mount"),
            blog_post("Atom mini 83: Flight Time", blog_slug="atom-mini-83-flight-time", youtube_id="wZJBGKiKWQg"),
            video("RiaUwqlxnq4"),
            video("1c_5Qh-GpT4"),
        ],
    },
    {
        "title": "The Scorpion",
        "members": [
            blog_post("ReMaidening the Scorpion.", blog_slug="remaidening-the-scorpion", youtube_id="YQl5Rdh1vYc"),
            blog_post("Scorpion rebuild..", blog_slug="scorpion-rebuild"),
            video("5GuoCiJ1ITE"),
            video("AximmwsaYUM"),
        ],
    },
    {
        "title": "TinyShark",
        "members": [
            video("kziEiXoVEhM"), video("5MxbLc0T3zw"), video("VbAfUV3Ibus"),
            video("PwYAItldZUo"), video("2G18MzDG2c4"),
        ],
    },
    {
        "title": 'AlienWhoop F7 "The Queen"',
        "members": [video("bs0Sk1xzaTQ"), video("QFmhsKdeWwY"), video("IvL5PbUjcyk")],
    },
    {
        "title": "The Corvus Series",
        # Confirmed correct even though "Corvus 2 - Vtol" and "The Corvus
        # r.2" may be the same build revision -- see module docstring /
        # issue #10's closing comment.
        "members": [video("tRKUF0G2_6I"), video("Z-W3FmqXLkc"), video("FHI3IkUz9nQ")],
    },
    {
        "title": "KSP Walker Studies",
        "members": [video("tVdqpCKNFY4"), video("oVeS9KO7c2o"), video("jSC0hAgcCio")],
    },
    {
        "title": 'The Project ("Where it started")',
        "members": [video("Bh9BhP_5Yr0"), video("rhIAzhdHhhU"), video("9nHipTF76Qs")],
    },
]


def build_video_id_index():
    """slug -> capture_events row, keyed by extracted YouTube video ID, for
    every media_type='youtube' row in the target's live database."""
    index = {}
    for row in db.search(limit=1000000):
        if row.get("media_type") != "youtube":
            continue
        vid = object_types.extract_youtube_id(row.get("external_url"))
        if vid:
            index[vid] = row
    return index


def find_blog_post(row_by_url_slug, video_id_index, member):
    """Resolves one blog_post member. Tries its blog_slug (a substring match
    against every row's external_url -- covers a plain document-type post
    whose external_url is still its original hooptiej.com/blog/<slug>.html
    link), then its youtube_id (covers a post that embeds a video and so was
    imported as media_type='youtube' with the video's URL instead -- see
    module docstring)."""
    if member["blog_slug"]:
        row = row_by_url_slug.get(member["blog_slug"])
        if row is not None:
            return row
    if member["youtube_id"]:
        row = video_id_index.get(member["youtube_id"])
        if row is not None:
            return row
    return None


def build_blog_url_slug_index():
    """blog url slug ('hooptie-js-gammaatom') -> row, for every row whose
    external_url looks like a hooptiej.com blog post link. Built once so
    find_blog_post is a plain dict lookup rather than an O(n) scan per
    member."""
    index = {}
    for row in db.search(limit=1000000):
        url = row.get("external_url") or ""
        if "/blog/" not in url or not url.endswith(".html"):
            continue
        slug = url.rsplit("/blog/", 1)[1][: -len(".html")]
        index[slug] = row
    return index


def member_label(member):
    if member["kind"] == "video":
        return f"video {member['video_id']}"
    return f'blog post "{member["title"]}"'


def get_or_create_project(base_url, title):
    """Reuses an existing project with this exact title (case-insensitive)
    if one exists; otherwise creates it via the target's own POST
    /api/projects (the real code path, including its get_or_create_tag call
    -- see web/app.py's api_create_project) so a freshly-created project
    gets a linked tag exactly like any UI-driven one would."""
    for project in db.list_projects():
        if project["title"].strip().lower() == title.strip().lower():
            return project, False
    payload = urllib.parse.urlencode({"title": title}).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/projects"
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            created = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?"
        ) from e
    # api_create_project's JSON response is the slim dropdown shape (id,
    # slug, title, status) and doesn't include tag_id -- re-read the full
    # row directly so attach_project_member below has it.
    return db.get_project(created["id"]), True


def attach_project_member(project, row):
    """Mirrors web/app.py's _attach_to_project exactly (both project_items
    membership AND the project's linked tag) so a script-created attachment
    looks identical to one the upload-time flow would have made -- see #1
    and _attach_to_project's docstring for why both halves matter."""
    db.add_item_to_project(project["id"], row["slug"])
    if project.get("tag_id"):
        db.attach_tags(row["slug"], [project["tag_id"]])


def main():
    parser = argparse.ArgumentParser(
        description="Create the 8 confirmed projects from issue #10 and attach the correct "
                     "re-imported content rows to each."
    )
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the running Constructicon instance to POST /api/projects against "
             "for any project that doesn't already exist (must share a database with this "
             "process — see module docstring).",
    )
    args = parser.parse_args()

    db.init_db()

    video_id_index = build_video_id_index()
    blog_url_slug_index = build_blog_url_slug_index()

    total_matched = 0
    total_missing = 0
    missing_report = []

    print("=" * 70)
    for project_def in PROJECTS:
        title = project_def["title"]
        project, was_created = get_or_create_project(args.base_url, title)
        print(f'\nProject "{title}" (slug={project["slug"]}) '
              f'{"created" if was_created else "already existed"}')

        for member in project_def["members"]:
            if member["kind"] == "video":
                row = video_id_index.get(member["video_id"])
            else:
                row = find_blog_post(blog_url_slug_index, video_id_index, member)

            if row is None:
                total_missing += 1
                missing_report.append((title, member))
                print(f"  MISSING: {member_label(member)} — no matching row in the target database")
                continue

            attach_project_member(project, row)
            total_matched += 1
            print(f"  OK: {member_label(member)} -> slug {row['slug']}")

    print("\n" + "=" * 70)
    print(f"Done. {total_matched} member(s) matched and attached, {total_missing} missing.")
    if missing_report:
        print("\nMissing members (by project):")
        for title, member in missing_report:
            print(f"  - {title}: {member_label(member)}")
        print(
            "\nA missing video is most likely one of #22's 2 gap videos "
            "(aAYQKauKA8M / _Trkb3k0UI0's siblings aren't part of any #10 "
            "grouping, but any other video listed above that's absent means "
            "the target hasn't had the relevant import run against it yet). "
            "Re-run this script after the missing import completes — "
            "already-applied groupings are unaffected."
        )
    print("=" * 70)


if __name__ == "__main__":
    main()
