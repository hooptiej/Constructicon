#!/usr/bin/env python3
"""Static check (#720): the type sampler covers every registered object type and extension.

    python scripts/check_type_samples.py

Reads the registry (core/object_types/, imported at run time) and the sampler's manifest
(scripts/make_type_samples.py SAMPLES + CONTENT; nothing is generated) and fails when:
  * a registered type has no sample (a file, or a content item for the types with no file);
  * an extension a type claims has no sample file of THAT type (".zip" needs an archive zip AND a
    macOS-app zip, ".exe" an application AND an installer);
  * a sample names an unregistered type, an extension its type doesn't claim, or a duplicate file name;
  * a sample's expectations can't be derived (a type that decides thumbnails per file, has_thumbnail_fn,
    needs the sample to say whether it carries one).
A new type or extension therefore fails here until scripts/make_type_samples.py gets a sample for it.
Needs the app's packages (run it in the app image, like the rest of the suite).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from core import object_types as ot  # noqa: E402
import make_type_samples as mts  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


registered = set(ot.OBJECT_TYPES)
file_types = {s.media_type for s in mts.SAMPLES}
content_types = {c.media_type for c in mts.CONTENT}

missing_types = sorted(registered - file_types - content_types)
check("every registered type has a sample", not missing_types, ", ".join(missing_types))

unknown = sorted((file_types | content_types) - registered)
check("every sample names a registered type", not unknown, ", ".join(unknown))

have = {(s.media_type, ot.file_extension(s.file)) for s in mts.SAMPLES}
missing_ext = sorted(f"{k} {e}" for k, spec in ot.OBJECT_TYPES.items() for e in spec.extensions if (k, e) not in have)
check("every registered extension has a sample of its type", not missing_ext, ", ".join(missing_ext))

wrong_ext = sorted(s.file for s in mts.SAMPLES
                   if s.media_type in ot.OBJECT_TYPES
                   and ot.file_extension(s.file) not in ot.OBJECT_TYPES[s.media_type].extensions)
check("every sample's extension is claimed by its type", not wrong_ext, ", ".join(wrong_ext))

names = [s.file for s in mts.SAMPLES] + [c.name for c in mts.CONTENT]
dupes = sorted({n for n in names if names.count(n) > 1})
check("sample names are unique", not dupes, ", ".join(dupes))

no_file = sorted(k for k, spec in ot.OBJECT_TYPES.items() if not spec.extensions)
covered_by_content = sorted(k for k in no_file if k in content_types)
check("types with no file are covered by content items", covered_by_content == no_file,
      ", ".join(sorted(set(no_file) - set(covered_by_content))))

bad_exp = []
for s in mts.SAMPLES:
    if s.media_type not in ot.OBJECT_TYPES:
        continue
    try:
        mts.expectations(s)
    except Exception as e:  # report every sample whose expectations can't be built, not just the first
        bad_exp.append(f"{s.file} ({e})")
check("every sample's expectations derive from the registry", not bad_exp, "; ".join(bad_exp))

exec_exts = {".exe", ".dll", ".msi", ".msix", ".appx", ".msixbundle", ".appxbundle", ".pkg", ".mpkg",
             ".xip", ".deb", ".rpm", ".dmg", ".elf"}
unmarked = sorted(s.file for s in mts.SAMPLES if ot.file_extension(s.file) in exec_exts and not s.executable)
check("executable/installer samples are marked executable (Linux only, never committed)", not unmarked,
      ", ".join(unmarked))

print(f"{len(registered)} registered types, {len(mts.SAMPLES)} sample files, {len(mts.CONTENT)} content items")
print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
sys.exit(1 if failures else 0)
