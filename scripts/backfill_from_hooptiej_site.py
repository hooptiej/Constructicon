"""One-off migration: backfill capture_events + blog_tags/post_tags from the
real content on hooptiej.github.io (the live hooptiej.com static site).

This is a ONE-SHOT data migration, not a sync job. It's meant to run exactly
once against a fresh/empty Constructicon instance (a clone of the site's
HTML is the source of truth; the only network traffic this script makes is
to the Constructicon instance itself, given via --base-url — it never
reaches out to hooptiej.com or anywhere else).

Issue #21: content rows (capture_events) are now created by POSTing to the
target instance's own POST /api/content — the same endpoint the upload
drawer's "add a link" flow and any other real caller use — instead of
calling core.db.insert_content directly in-process. That matters because
/api/content is what actually schedules the post-#15 object-type dispatch
(core/object_types.py): it looks up the media_type's ObjectTypeSpec,
schedules background_tasks.add_task(ocr.run_ocr, ...) for anything
ocr_capable (which fetches/generates the type's thumbnail as a side effect
of preparing an OCR source — see core/ocr.py), and schedules a capture-only
thumbnail job for a CAPTURE-sourced type that isn't OCR-capable. Calling
core.db.insert_content directly skips all of that scheduling — the row
would sit at ocr_status='pending' with no thumbnail until something else
(an app restart's pending-OCR self-heal, or the 10-minute stale-OCR
watchdog — see web/app.py) happened to notice it. Going through the real
endpoint means thumbnails/OCR run the same way they would for anything a
human actually clicked "add" on.

The `projects`/`project_items` tables and blog_tags/post_tags tag tree have
no HTTP equivalent (no web route creates/attaches a tag) and are pure
metadata bookkeeping with no thumbnail/OCR/dispatch behavior riding on
them, so those two calls (db.get_or_create_tag / db.attach_tags) still go
straight to the database — the same database file the target instance
itself reads and writes, so run this against the SAME instance/DB pair
--base-url points at (see Usage below).

Safety-semantics note (changed by the #21 rework): the old direct-db-write
version relied on deterministic slugs (a blog post's own filename stem,
"yt-<video id>") plus capture_events.slug's UNIQUE constraint to fail
loudly on a re-run against an already-populated DB. POST /api/content lets
the server mint each row's slug (core/storage.make_slug — a random token),
so a second run no longer collides — it silently creates a full second copy
of every row instead. The "one-shot against a fresh DB" contract is
unchanged; only the failure mode if that contract is violated is (loud
error -> silent duplication). Still no in-script protection against a
partial re-run; start from a fresh DB/instance if something goes wrong
partway through.

Deliberately out of scope (see Constructicon's README / Phase 4 notes, and
issue #21's own guidance not to re-litigate #10's project groupings here):
  - The `projects` / `project_items` tables are NOT touched. There's an
    unresolved naming/design question between those curated-collection
    tables and this script's blog_tags tag tree (the README's planned
    "Projects" nav page is actually the auto-generated tag-tree table of
    contents, a different thing from the `projects` tables). Until the
    owner decides how the two relate, this script only ever writes to
    capture_events (via the API) / blog_tags / post_tags (direct db calls).
  - No web routes or templates. No auth. No schema changes.

What it does:
  1. Reads blog/index.html in the site clone for the canonical list of blog
     posts (title, date, excerpt), and each post's own HTML file to tell
     whether it's a plain written post (media_type='document') or a post
     that's really a wrapper around an embedded YouTube video
     (media_type='youtube') -- created via POST /api/content.
  2. Reads projects/index.html for the five top-level categories (title +
     description) and creates one root blog_tag per category via
     db.get_or_create_tag.
  3. Reads each projects/<category>.html page. Content is organized into
     <section class="card"> blocks, each optionally headed by an <h3> (a
     real named sub-topic the site author chose, e.g. "TinyShark", "The
     Corvus series") or an <h2> (a plain "where to look" links section, no
     sub-topic identity). Every <h3> becomes a child tag under its category
     tag -- this mirrors real structure already on the site rather than
     inventing new nesting.
  4. Within each section, every <li> pointing at a relative ../blog/*.html
     link tags that already-created blog post with the section's tag
     (category tag, or the closer child tag if the section has an <h3>).
     Every <li> pointing at a youtube.com/watch?v=... link, and every
     directly-embedded <iframe src=".../embed/...">, becomes its own new
     capture_events row (media_type='youtube', external_url set, tagged the
     same way). Plain reference links (Thingiverse, the raw YouTube channel
     URL) are skipped -- they aren't content, just navigation.
  5. Prints a summary of tags / posts / videos created so a human can
     sanity-check the counts against the site.

Usage:
    python scripts/backfill_from_hooptiej_site.py /path/to/hooptiej-site-clone \\
        --base-url http://localhost:8000

    --base-url must point at the SAME running Constructicon instance whose
    database this process can also see at core.db.DB_PATH (the default,
    repo-relative imagerepo.db) — e.g. run this from inside the app's own
    container (`docker exec <container> python3 scripts/backfill_from_hooptiej_site.py ...
    --base-url http://localhost:80`, matching whatever port/host the app's
    own `uvicorn` command binds), not from an unrelated machine pointed at
    the instance over the network, since the tag-tree calls bypass HTTP
    entirely and write to that file directly.
"""

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from calendar import timegm
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import db  # noqa: E402

UPLOADED_BY = db.source_migrated_from("hooptiej.github.io")  # Source: this is an automated migration, not a real "tech" doing the capturing
CAPTURE_NOTE = "Backfilled from hooptiej.github.io (Phase 4 content migration)"
SITE_ROOT = "https://hooptiej.com"

CATEGORY_FILES = [
    "fpv-flight.html",
    "alienwhoop.html",
    "ksp-builds.html",
    "3d-printing.html",
    "other-builds.html",
]

BLOG_LI_RE = re.compile(
    r'<li>\s*<img[^>]*>\s*<div>\s*'
    r'<div class="post-date">([^<]*)</div>\s*'
    r'<a href="([^"]+)"[^>]*>([^<]*)</a>\s*'
    r'<div class="post-excerpt">(.*?)</div>\s*</div>\s*</li>',
    re.S,
)
SECTION_RE = re.compile(r'<section class="card">(.*?)</section>', re.S)
HEADING_RE = re.compile(r'<h([23])[^>]*>(.*?)</h\1>', re.S)
EXCERPT_RE = re.compile(r'<p class="post-excerpt">(.*?)</p>', re.S)
IFRAME_RE = re.compile(
    r'<iframe src="https://www\.youtube\.com/embed/([A-Za-z0-9_-]+)"[^>]*title="([^"]*)"'
)
LI_ANCHOR_RE = re.compile(r'<li>.*?<a href="([^"]+)"[^>]*>([^<]*)</a>', re.S)
LI_SPLIT_RE = re.compile(r'<li>.*?</li>', re.S)
WATCH_ID_RE = re.compile(r'youtube\.com/watch\?v=([A-Za-z0-9_-]+)')
H2_TITLE_RE = re.compile(r'<h2>(.*?)</h2>')


def clean(text):
    return html.unescape(re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', '', text)).strip())


def parse_date(text):
    """'Aug 14, 2017' -> epoch seconds (UTC midnight). Returns None if unparsable."""
    text = clean(text)
    try:
        dt = datetime.strptime(text, "%b %d, %Y")
    except ValueError:
        return None
    return timegm(dt.timetuple())


def read(path):
    return path.read_text(encoding="utf-8")


def create_content_row(base_url, **fields):
    """POSTs to the target instance's real POST /api/content — see this
    module's docstring for why that (not core.db.insert_content) is what
    actually gets thumbnail/OCR dispatch scheduled. Returns the created
    row's public JSON (as returned by web/app.py's _to_public), including
    the server-minted `slug` callers need for any follow-up db.attach_tags
    call. None-valued fields are dropped rather than sent as the literal
    string "None", relying on api_create_content's own Form(...) defaults.
    """
    payload = {k: v for k, v in fields.items() if v is not None}
    data = urllib.parse.urlencode(payload).encode("utf-8")
    url = f"{base_url.rstrip('/')}/api/content"
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"POST {url} failed ({e.code}): {body}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Couldn't reach {url} ({e.reason}) — is the target instance running and --base-url correct?"
        ) from e


def parse_blog_index(site_dir):
    """Returns an ordered list of dicts: slug, title, date_epoch, excerpt, href.
    `slug` here is only this script's own logical key for wiring up tags
    later (see blog_slug_to_real_slug in main()) -- it is NOT the slug the
    created capture_events row ends up with, which the server mints fresh
    per create_content_row's docstring."""
    text = read(site_dir / "blog" / "index.html")
    posts = []
    for m in BLOG_LI_RE.finditer(text):
        date_text, href, title, excerpt = m.groups()
        slug = Path(href).stem
        posts.append({
            "slug": slug,
            "href": href,
            "title": clean(title),
            "date_epoch": parse_date(date_text),
            "excerpt": clean(excerpt),
        })
    return posts


def classify_and_insert_post(site_dir, post, base_url):
    """Reads the post's own HTML file to decide document vs youtube, then
    creates the capture_events row via POST /api/content. Returns
    (kind, real_slug) where kind is 'document' or 'youtube' and real_slug is
    the slug the server actually assigned the new row."""
    post_path = site_dir / "blog" / post["href"]
    post_html = read(post_path)
    content_date = str(post["date_epoch"]) if post["date_epoch"] is not None else None
    iframe_match = IFRAME_RE.search(post_html)
    if iframe_match:
        video_id = iframe_match.group(1)
        row = create_content_row(
            base_url,
            media_type="youtube",
            external_url=f"https://www.youtube.com/watch?v={video_id}",
            content_description=post["excerpt"],
            content_date=content_date,
            description=CAPTURE_NOTE,
        )
        return "youtube", row["slug"]
    row = create_content_row(
        base_url,
        media_type="document",
        external_url=f"{SITE_ROOT}/blog/{post['href']}",
        content_description=post["excerpt"],
        content_date=content_date,
        description=CAPTURE_NOTE,
    )
    return "document", row["slug"]


def parse_category_title(site_dir, filename):
    text = read(site_dir / "projects" / filename)
    m = H2_TITLE_RE.search(text)
    return clean(m.group(1)) if m else filename


def process_category_page(site_dir, filename, category_tag_id, blog_slug_to_real_slug,
                           video_id_to_real_slug, stats, base_url):
    text = read(site_dir / "projects" / filename)
    for section_match in SECTION_RE.finditer(text):
        section = section_match.group(1)
        heading = HEADING_RE.search(section)
        tag_id = category_tag_id
        if heading and heading.group(1) == "3":
            tag = db.get_or_create_tag(clean(heading.group(2)), parent_id=category_tag_id)
            tag_id = tag["id"]
            stats["subtags_created"].add((category_tag_id, tag["name"]))

        # A directly-embedded video (not inside an <li>) -- e.g. "The Maiden",
        # "Most-watched video on the channel".
        iframe_match = IFRAME_RE.search(section)
        if iframe_match:
            video_id, title = iframe_match.groups()
            if video_id not in video_id_to_real_slug:
                excerpt_match = EXCERPT_RE.search(section)
                row = create_content_row(
                    base_url,
                    media_type="youtube",
                    external_url=f"https://www.youtube.com/watch?v={video_id}",
                    content_description=clean(excerpt_match.group(1)) if excerpt_match else None,
                    description=CAPTURE_NOTE,
                )
                video_id_to_real_slug[video_id] = row["slug"]
                stats["videos_created"] += 1
            db.attach_tags(video_id_to_real_slug[video_id], [tag_id])
            stats["tag_attachments"] += 1

        # Every <li> in the section -- either a link back to a blog post
        # (tag-only, the post already exists) or a raw YouTube watch link
        # (new video row) or a plain reference link (skip).
        for li_html in LI_SPLIT_RE.findall(section):
            anchor = LI_ANCHOR_RE.search(li_html)
            if not anchor:
                continue
            href, link_text = anchor.groups()
            watch_match = WATCH_ID_RE.search(href)
            if watch_match:
                video_id = watch_match.group(1)
                if video_id not in video_id_to_real_slug:
                    row = create_content_row(
                        base_url,
                        media_type="youtube",
                        external_url=f"https://www.youtube.com/watch?v={video_id}",
                        description=CAPTURE_NOTE,
                    )
                    video_id_to_real_slug[video_id] = row["slug"]
                    stats["videos_created"] += 1
                db.attach_tags(video_id_to_real_slug[video_id], [tag_id])
                stats["tag_attachments"] += 1
            elif "../blog/" in href:
                post_slug = Path(href).stem
                if post_slug in blog_slug_to_real_slug:
                    db.attach_tags(blog_slug_to_real_slug[post_slug], [tag_id])
                    stats["tag_attachments"] += 1
            # else: a plain external reference link (Thingiverse, the raw
            # YouTube channel URL, etc.) -- not archived content, skip.


def main():
    parser = argparse.ArgumentParser(
        description="Backfill Constructicon from a local hooptiej.github.io site clone."
    )
    parser.add_argument("site_dir", help="Path to a local clone of hooptiej.github.io")
    parser.add_argument(
        "--base-url", required=True,
        help="Base URL of the running Constructicon instance to POST /api/content against "
             "(must share a database with this process — see module docstring)",
    )
    args = parser.parse_args()
    site_dir = Path(args.site_dir).resolve()
    if not (site_dir / "blog" / "index.html").exists():
        print(f"Doesn't look like a hooptiej.github.io clone: {site_dir}")
        sys.exit(1)

    db.init_db()

    stats = {"subtags_created": set(), "videos_created": 0, "tag_attachments": 0}

    # --- Blog posts ---
    posts = parse_blog_index(site_dir)
    blog_slug_to_real_slug = {}
    doc_count = video_from_blog_count = 0
    for post in posts:
        kind, real_slug = classify_and_insert_post(site_dir, post, args.base_url)
        blog_slug_to_real_slug[post["slug"]] = real_slug
        if kind == "document":
            doc_count += 1
        else:
            video_from_blog_count += 1

    # --- Top-level category tags ---
    category_tag_ids = {}
    for filename in CATEGORY_FILES:
        title = parse_category_title(site_dir, filename)
        tag = db.get_or_create_tag(title, parent_id=None)
        category_tag_ids[filename] = tag["id"]

    # --- Per-category sub-tags, video rows, and tagging ---
    video_id_to_real_slug = {}
    for filename in CATEGORY_FILES:
        process_category_page(
            site_dir, filename, category_tag_ids[filename], blog_slug_to_real_slug,
            video_id_to_real_slug, stats, args.base_url,
        )

    total_tags = len(category_tag_ids) + len(stats["subtags_created"])
    print("=" * 60)
    print("Backfill complete.")
    print(f"  Tags created:            {total_tags} "
          f"({len(category_tag_ids)} top-level + {len(stats['subtags_created'])} sub-tags)")
    print(f"  Blog posts inserted:     {len(posts)} "
          f"({doc_count} document, {video_from_blog_count} youtube-embed)")
    print(f"  Standalone videos inserted: {stats['videos_created']}")
    print(f"  Total capture_events rows:  {len(posts) + stats['videos_created']}")
    print(f"  Tag attachments made:    {stats['tag_attachments']}")
    print("  projects / project_items: left untouched (owner decision pending, see issue #10)")
    print("=" * 60)


if __name__ == "__main__":
    main()
