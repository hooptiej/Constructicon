"""One-off migration: backfill capture_events + blog_tags/post_tags from the
real content on hooptiej.github.io (the live hooptiej.com static site).

This is a ONE-SHOT data migration, not a sync job. It's meant to run exactly
once against a fresh/empty Constructicon DB (a clone of the site's HTML is
the source of truth; nothing here reaches out to the network). It does not
try to diff against a previously-migrated DB or handle partial re-runs
gracefully — capture_events.slug is UNIQUE, so re-running it against a DB it
already populated will fail loudly on a slug collision rather than silently
duplicating rows. If you need to re-run it, start from a fresh DB.

Deliberately out of scope (see Constructicon's README / Phase 4 notes):
  - The `projects` / `project_items` tables are NOT touched. There's an
    unresolved naming/design question between those curated-collection
    tables and this script's blog_tags tag tree (the README's planned
    "Projects" nav page is actually the auto-generated tag-tree table of
    contents, a different thing from the `projects` tables). Until the
    owner decides how the two relate, this script only ever writes to
    capture_events / blog_tags / post_tags.
  - No web routes or templates. No auth. No schema changes.

What it does:
  1. Reads blog/index.html in the site clone for the canonical list of blog
     posts (title, date, excerpt), and each post's own HTML file to tell
     whether it's a plain written post (media_type='document') or a post
     that's really a wrapper around an embedded YouTube video
     (media_type='youtube') -- inserted via db.insert_upload /
     db.insert_content.
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
    python scripts/backfill_from_hooptiej_site.py /path/to/hooptiej-site-clone
"""

import html
import re
import sys
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


def video_slug(video_id):
    return f"yt-{video_id}"


def read(path):
    return path.read_text(encoding="utf-8")


def parse_blog_index(site_dir):
    """Returns an ordered list of dicts: slug, title, date_epoch, excerpt, href."""
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


def classify_and_insert_post(site_dir, post):
    """Reads the post's own HTML file to decide document vs youtube, then
    inserts the capture_events row. Returns 'document' or 'youtube'."""
    post_path = site_dir / "blog" / post["href"]
    post_html = read(post_path)
    iframe_match = IFRAME_RE.search(post_html)
    if iframe_match:
        video_id = iframe_match.group(1)
        db.insert_content(
            post["slug"],
            UPLOADED_BY,
            media_type="youtube",
            external_url=f"https://www.youtube.com/watch?v={video_id}",
            content_description=post["excerpt"],
            content_date=post["date_epoch"],
            description=CAPTURE_NOTE,
        )
        return "youtube"
    db.insert_content(
        post["slug"],
        UPLOADED_BY,
        media_type="document",
        external_url=f"{SITE_ROOT}/blog/{post['href']}",
        content_description=post["excerpt"],
        content_date=post["date_epoch"],
        description=CAPTURE_NOTE,
    )
    return "document"


def parse_category_title(site_dir, filename):
    text = read(site_dir / "projects" / filename)
    m = H2_TITLE_RE.search(text)
    return clean(m.group(1)) if m else filename


def process_category_page(site_dir, filename, category_tag_id, blog_slugs, created_video_slugs, stats):
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
            slug = video_slug(video_id)
            if slug not in created_video_slugs:
                excerpt_match = EXCERPT_RE.search(section)
                db.insert_content(
                    slug,
                    UPLOADED_BY,
                    media_type="youtube",
                    external_url=f"https://www.youtube.com/watch?v={video_id}",
                    content_description=clean(excerpt_match.group(1)) if excerpt_match else None,
                    content_date=None,
                    description=CAPTURE_NOTE,
                )
                created_video_slugs.add(slug)
                stats["videos_created"] += 1
            db.attach_tags(slug, [tag_id])
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
                slug = video_slug(video_id)
                if slug not in created_video_slugs:
                    db.insert_content(
                        slug,
                        UPLOADED_BY,
                        media_type="youtube",
                        external_url=f"https://www.youtube.com/watch?v={video_id}",
                        content_description=None,
                        content_date=None,
                        description=CAPTURE_NOTE,
                    )
                    created_video_slugs.add(slug)
                    stats["videos_created"] += 1
                db.attach_tags(slug, [tag_id])
                stats["tag_attachments"] += 1
            elif "../blog/" in href:
                post_slug = Path(href).stem
                if post_slug in blog_slugs:
                    db.attach_tags(post_slug, [tag_id])
                    stats["tag_attachments"] += 1
            # else: a plain external reference link (Thingiverse, the raw
            # YouTube channel URL, etc.) -- not archived content, skip.


def main():
    if len(sys.argv) != 2:
        print(f"Usage: python {Path(__file__).name} /path/to/hooptiej-site-clone")
        sys.exit(1)
    site_dir = Path(sys.argv[1]).resolve()
    if not (site_dir / "blog" / "index.html").exists():
        print(f"Doesn't look like a hooptiej.github.io clone: {site_dir}")
        sys.exit(1)

    db.init_db()

    stats = {"subtags_created": set(), "videos_created": 0, "tag_attachments": 0}

    # --- Blog posts ---
    posts = parse_blog_index(site_dir)
    blog_slugs = set()
    doc_count = video_from_blog_count = 0
    for post in posts:
        kind = classify_and_insert_post(site_dir, post)
        blog_slugs.add(post["slug"])
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
    created_video_slugs = set()
    for filename in CATEGORY_FILES:
        process_category_page(
            site_dir, filename, category_tag_ids[filename], blog_slugs, created_video_slugs, stats
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
    print("  projects / project_items: left untouched (owner decision pending)")
    print("=" * 60)


if __name__ == "__main__":
    main()
