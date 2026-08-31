"""One-off script: seed a handful of real, SPECIFIC `projects` rows so the
home page's Projects column (right two-thirds, see web/app.py's home_page
route) has something real to show, and so there's something to click into
on the project detail page.

This replaces an earlier version of this script that seeded three projects
which were really just "everything tagged category X" rollups (e.g. "every
AlienWhoop/TinyShark post") — not a real project, just the tag tree wearing
a project card. A real project here is a specific build/effort (a specific
quad, a specific KSP ship design, a specific robot) with only the handful of
items that actually belong to that one thing, curated by hand via
db.add_item_to_project rather than derived from tag membership.

This is deliberately NOT wired into app startup or the backfill script —
`projects`/`project_items` are curated by hand, not derived data, same as
seed_test_data.py's fake gallery uploads being opt-in rather than automatic.

Sourcing note (see the PR description for the full writeup): the real
hooptiej.com content lives in two places with different reliability for
figuring out "what belongs together as one project" —
  - The original Wix site (hooptiej.wixsite.com/hooptiej) is the actual
    primary source, but its own "Projects" page is only three broad
    category blurbs (FPV and Flight / 3D Modeling and Printing / Other
    Builds and Tech) with no per-build breakdown — it doesn't group by
    specific build at all except where a build is named directly in the
    page copy (e.g. "Tension Biped", "the Only Flying Skorpion").
  - hooptiej.github.io (what scripts/backfill_from_hooptiej_site.py reads)
    is itself a later migration/reinterpretation of that content — its
    <h3> sub-topic headings and extra backfilled YouTube videos are an
    editorial layer added during that migration, not original site
    structure, so they're treated here as secondary evidence (useful for
    which specific videos exist and their titles) rather than as the
    grouping authority.
Each project below is tagged in its comment with how it was sourced.

Run with the venv's python from the Constructicon repo root, AFTER
scripts/backfill_from_hooptiej_site.py has populated capture_events (this
script only references slugs that script creates — a missing slug is
skipped with a warning rather than failing the whole run):
    python scripts/seed_example_projects.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import db  # noqa: E402


def yt(video_id):
    return f"yt-{video_id}"


# Each entry: title, description, and an ordered list of capture_events
# slugs that actually belong to that one specific build/effort — not a tag
# rollup. cover_slug defaults to the first item with a local file (usually
# none, since almost everything here is backfilled youtube/document
# content); the project card falls back to a placeholder cover when that's
# the case, which is expected.
PROJECT_DEFS = [
    {
        "title": "Tension Biped",
        "description": "A Hackaday-featured 9g-servo walking biped robot on a Raspberry Pi — "
                        "source lives on GitHub (github.com/hooptiej/RPIbot-code); these are the "
                        "video logs of it actually walking.",
        # Wix-confirmed: named directly on the real Projects page ("Check out my
        # Hackaday Project: Tension Biped for a good Real-world learning-to-Engineering
        # example") — the highest-confidence grouping in this batch.
        "items": [yt("v5KVF-_3Txo"), yt("2xEu-t-PUyU")],
    },
    {
        "title": "GammaAtom (RotorX Atom Mini 83)",
        "description": "The RotorX Atom 83 Mini racequad build nicknamed GammaAtom — the camera "
                        "mount fix, flight-time tuning, and a couple of test flights.",
        # Wix-confirmed for the 3 blog posts: all three exist as real posts on the
        # live Wix blog, and \"Hooptie J's GammaAtom\" is captioned there as \"A RotorX
        # Atom 83 Mini racequad build\", directly tying the GammaAtom nickname to the
        # Atom Mini 83 hardware. The 2 extra yt- videos are GH-pages-only backfill
        # content (never appeared on the Wix blog) added on title-match evidence
        # ("Atom 83", "Atom Mini 83") — lower confidence than the 3 blog posts.
        "items": [
            "hooptie-js-gammaatom", "gammaatom-update-camera-mount", "atom-mini-83-flight-time",
            yt("RiaUwqlxnq4"), yt("1c_5Qh-GpT4"),
        ],
    },
    {
        "title": "The Scorpion (FoxFlite 180)",
        "description": "Rebuilding and re-maidening the FoxFlite Scorpion 180 — the flying "
                        "machine the original site literally called itself \"home of the only "
                        "flying Scorpion\" for.",
        # Wix-confirmed for \"ReMaidening the Scorpion.\": verified live on the real Wix
        # site at single-post/2017/07/15/remaidening-the-scorpion, body text confirms
        # \"my Foxflite Scorpion\". \"Scorpion rebuild..\" is presumed the same build (same
        # post date/topic on both the Wix blog listing and the GH-pages migration) though
        # its own Wix single-post URL wasn't independently verified. The 2 extra yt-
        # videos are GH-pages-only backfill content with no Wix equivalent, included on
        # title-match evidence (\"FoxFlite Scorpion 180\") — lower confidence.
        "items": [
            "remaidening-the-scorpion", "scorpion-rebuild",
            yt("5GuoCiJ1ITE"), yt("AximmwsaYUM"),
        ],
    },
    {
        "title": "TinyShark",
        "description": "The TinyShark micro-quad — night flying, gym-session hovering, and "
                        "house-flying clips of the little guy.",
        # Inferred, not Wix-evidenced: the Wix site's Projects page has no AlienWhoop
        # or TinyShark section at all (only 3 broad categories), and these videos never
        # surfaced on the Wix blog archive either — this looks like YouTube-channel-only
        # content that was never written up on the original site. Grouped purely from
        # shared "TinyShark" wording across GH-pages-backfilled video titles.
        "items": [
            yt("kziEiXoVEhM"), yt("5MxbLc0T3zw"), yt("VbAfUV3Ibus"), yt("PwYAItldZUo"), yt("2G18MzDG2c4"),
        ],
    },
    {
        "title": "AlienWhoop F7 — \"The Queen\"",
        "description": "The AlienWhoop F7 build nicknamed \"The Queen\" — LED demos, a battery "
                        "run-down test, and lighting experiments during the #TeamAlienWhoop era.",
        # Inferred, not Wix-evidenced — same caveat as TinyShark: no AlienWhoop section
        # exists on the real Wix site, grouped purely from shared "AlienWhoop F7"/"Queen"
        # wording across GH-pages-backfilled video titles.
        "items": [yt("bs0Sk1xzaTQ"), yt("QFmhsKdeWwY"), yt("IvL5PbUjcyk")],
    },
    {
        "title": "The Corvus Series",
        "description": "A Kerbal Space Program ship design that went through a few generations — "
                        "VTOL, the SeaCrow variant, and a revision 2.",
        # Inferred, not Wix-evidenced: the real Wix Projects page has no Kerbal Space
        # Program section at all. Grouped from the shared "Corvus" name across three
        # GH-pages-backfilled video titles under what that migration's own page called
        # "The Corvus series" — treated here as a title-pattern signal, not as
        # structural ground truth.
        "items": [yt("tRKUF0G2_6I"), yt("Z-W3FmqXLkc"), yt("FHI3IkUz9nQ")],
    },
    {
        "title": "KSP Walker Studies",
        "description": "Early experiments in legged, strut-based movement in Kerbal Space "
                        "Program — long before the real-world Tension Biped years later.",
        # Inferred, not Wix-evidenced — same caveat as The Corvus Series. Grouped from
        # shared "Walker" naming across three GH-pages-backfilled video titles.
        "items": [yt("tVdqpCKNFY4"), yt("oVeS9KO7c2o"), yt("jSC0hAgcCio")],
    },
    {
        "title": "The Project (earliest build)",
        "description": "The oldest footage on the channel, predating FPV and KSP entirely — an "
                        "early build referred to in its own video titles simply as \"The Project.\"",
        # Inferred, not Wix-evidenced: not referenced on the real Wix site at all (it
        # predates everything Wix hosts). Grouped purely because two of the three video
        # titles literally share the words "The Project."
        "items": [yt("Bh9BhP_5Yr0"), yt("rhIAzhdHhhU"), yt("9nHipTF76Qs")],
    },
]


def _pick_cover(slugs):
    """First item in the list that has a local uploaded file — most of this
    content is backfilled youtube/document rows with no file, so this often
    comes back None and the project card just shows its placeholder cover,
    which is expected."""
    for slug in slugs:
        row = db.get_by_slug(slug)
        if row and row.get("filename"):
            return slug
    return None


def main():
    db.init_db()
    seeded = 0
    for project_def in PROJECT_DEFS:
        present = [s for s in project_def["items"] if db.get_by_slug(s) is not None]
        missing = [s for s in project_def["items"] if s not in present]
        if missing:
            print(f"note: {project_def['title']!r} — {len(missing)} slug(s) not found in the DB yet, skipping them: {missing}")
        if not present:
            print(f"skip {project_def['title']!r}: none of its slugs exist yet — run scripts/backfill_from_hooptiej_site.py first")
            continue
        project = db.create_project(
            project_def["title"],
            description=project_def["description"],
            cover_slug=_pick_cover(present),
        )
        for slug in present:
            db.add_item_to_project(project["id"], slug)
        seeded += 1
        print(f"seeded project {project['slug']!r} ({project['title']}) with {len(present)} item(s)")

    if seeded == 0:
        print("Nothing seeded — no matching content found. Run the backfill script first.")


if __name__ == "__main__":
    main()
