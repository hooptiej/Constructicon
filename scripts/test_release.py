"""Standalone test for scripts/release.py's version logic (#508). No pytest in
this repo. Run: python scripts/test_release.py
"""
import datetime
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import release

D = datetime.date


def check(name, got, want):
    ok = got == want
    print(("PASS" if ok else "FAIL"), name, "" if ok else f"(got {got!r}, want {want!r})")
    return ok


results = [
    check("no tags", release.next_version([], D(2026, 10, 3)), "2026.10.3"),
    check("non-calver tags ignored", release.next_version(["v1", "desktop-0.1.0"], D(2026, 10, 3)), "2026.10.3"),
    check("same-day repeat", release.next_version(["2026.10.3"], D(2026, 10, 3)), "2026.10.3.1"),
    check("same-day third", release.next_version(["2026.10.3", "2026.10.3.1"], D(2026, 10, 3)), "2026.10.3.2"),
    check("same-day order-independent", release.next_version(["2026.10.3.1", "2026.10.3"], D(2026, 10, 3)), "2026.10.3.2"),
    check("new day", release.next_version(["2026.10.2", "2026.10.2.1"], D(2026, 10, 3)), "2026.10.3"),
    check("new month", release.next_version(["2026.9.30"], D(2026, 10, 1)), "2026.10.1"),
    check("no leading zeros", release.next_version([], D(2027, 1, 5)), "2027.1.5"),
    check("10.3 vs 1.03 not confused", release.next_version(["2026.1.3"], D(2026, 10, 3)), "2026.10.3"),
    check("valid", release.valid_version("2026.10.3.1"), True),
    check("invalid leading zero", release.valid_version("2026.10.03"), False),
    check("invalid junk", release.valid_version("v2026.10.3"), False),
    check("changelog new file", release.prepend_changelog("", "## 1\n").startswith("# Changelog"), True),
    check("changelog prepends above old", release.prepend_changelog(release.CHANGELOG_HEADER + "## old\n", "## new\n").index("## new") <
          release.prepend_changelog(release.CHANGELOG_HEADER + "## old\n", "## new\n").index("## old"), True),
]
print(f"{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
