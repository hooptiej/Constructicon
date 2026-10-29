#!/usr/bin/env python3
"""The type sampler writes every file in its manifest, and each one sniffs as its type (#720).

    python scripts/test_type_samples_720.py

Generates the whole sampler (scripts/make_type_samples.py) into a temp dir, twice, and checks:
  * every manifest file was written, is non-empty and matches its recorded sha256;
  * object_types.detect_media_type(name, path) (the upload's own type detection, sniffers included)
    gives the sample's expected type: a sniffer that stops accepting its sample fails here, without a
    live install;
  * README.md, manifest.json, content.json and urls.txt are written and name every file;
  * two runs are byte-identical, except the files make_type_samples.VARYING lists.
EXECUTABLES: the executable-class samples (PE, installers, the macOS app, ELF, dmg) are generated
only on Linux. On any other OS they are SKIPPED (printed), never written, per the owner's Windows
Defender rule; the suite runs in the Linux app container, where they are covered.
Needs the app's packages and ffmpeg (the app image). No server, no DB writes.
"""

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import _testenv  # noqa: E402

TMP = _testenv.isolate("type-samples-720-")

from core import object_types as ot  # noqa: E402
import make_type_samples as mts  # noqa: E402

failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


if not mts.CAN_WRITE_EXECUTABLES:
    print(f"SKIP executable-class samples on {sys.platform} (Linux only; Defender rule): "
          + ", ".join(s.file for s in mts.SAMPLES if s.executable))

out1 = Path(tempfile.mkdtemp(prefix="samples-a-", dir=TMP))
out2 = Path(tempfile.mkdtemp(prefix="samples-b-", dir=TMP))
manifest, skipped = mts.generate(out1, quiet=True)
manifest2, _ = mts.generate(out2, quiet=True)

expected = [s for s in mts.SAMPLES if not (s.executable and not mts.CAN_WRITE_EXECUTABLES)]
check("manifest lists every generated sample", [f["file"] for f in manifest["files"]] == [s.file for s in expected])
check("skipped list matches the platform", sorted(skipped) == sorted(s.file for s in mts.SAMPLES if s.executable)
      if not mts.CAN_WRITE_EXECUTABLES else skipped == [])

for f in manifest["files"]:
    p = out1 / f["file"]
    data = p.read_bytes() if p.exists() else b""
    check(f"{f['file']}: written", bool(data))
    check(f"{f['file']}: sha256 recorded", hashlib.sha256(data).hexdigest() == f["sha256"])
    got = ot.detect_media_type(f["file"], p) if data else None
    check(f"{f['file']}: detected as {f['media_type']}", got == f["media_type"], f"got {got!r}")

for name in ("README.md", "manifest.json", "content.json", "urls.txt"):
    check(f"{name} written", (out1 / name).exists() and (out1 / name).stat().st_size > 0)
readme = (out1 / "README.md").read_text(encoding="utf-8")
missing = [f["file"] for f in manifest["files"] if f"`{f['file']}`" not in readme]
check("README names every file", not missing, ", ".join(missing))
check("README carries the Defender note", "Defender" in readme)
content = json.loads((out1 / "content.json").read_text(encoding="utf-8"))
check("content.json lists every no-file item", [c["name"] for c in content] == [c.name for c in mts.CONTENT])
urls = (out1 / "urls.txt").read_text(encoding="utf-8")
check("urls.txt lists the URL types",
      all(c.fields["external_url"] in urls for c in mts.CONTENT if c.fields.get("external_url")))

by_name = {f["file"]: f["sha256"] for f in manifest2["files"]}
differ = sorted(f["file"] for f in manifest["files"] if by_name.get(f["file"]) != f["sha256"])
unexpected = [d for d in differ if d not in mts.VARYING]
check("two runs are byte-identical (except VARYING)", not unexpected, ", ".join(unexpected))

# #737: .opus and .m4v are accepted (a registered type claims them), land as audio / video, play
# inline, and behave like their siblings: ffprobe reads them (duration, codec), the video gets a frame
# thumbnail and its creation_time date, the audio its tags.
from core import storage  # noqa: E402
from core.object_types import audio as audio_type, video as video_type  # noqa: E402
by_file = {f["file"]: f for f in manifest["files"]}
check("#737: .opus and .m4v are accepted upload extensions",
      {".opus", ".m4v"} <= set(ot.accepted_extensions()))
for name, kind in (("audio-tone.opus", "audio"), ("video-clip.m4v", "video")):
    f = by_file.get(name)
    check(f"#737: {name} is in the sampler as {kind}, inline", bool(f) and f["media_type"] == kind
          and f["disposition"] == "inline", repr(f and (f["media_type"], f["disposition"])))
    check(f"#737: {name} opens in the browser", ot.opens_in_browser(name))
_real_path_for = storage.path_for
storage.path_for = lambda stored: out1 / stored
try:
    props = audio_type.get_properties({"stored_filename": "audio-tone.opus"})
    check("#737: .opus properties (ffprobe): Duration and Codec OPUS",
          bool(props.get("Duration")) and props.get("Codec") == "OPUS", repr(props))
    tags = audio_type.get_embedded_metadata(out1 / "audio-tone.opus")
    check("#737: .opus tags read (title + artist)", bool(tags.get("content_description"))
          and (tags.get("type_metadata") or {}).get("artist") == "Constructicon Sampler", repr(tags))
    vprops = video_type.get_properties({"stored_filename": "video-clip.m4v"})
    check("#737: .m4v properties (ffprobe): Duration, Resolution, Codec H264",
          bool(vprops.get("Duration")) and bool(vprops.get("Resolution")) and vprops.get("Codec") == "H264",
          repr(vprops))
    frame = video_type.capture_frame(out1 / "video-clip.m4v")
    check("#737: .m4v frame thumbnail is a PNG", bool(frame) and frame[:8] == b"\x89PNG\r\n\x1a\n")
    check("#737: .m4v recording date from creation_time",
          "content_date" in video_type.get_embedded_metadata(out1 / "video-clip.m4v"))
    # #790: the hand-built legacy .xls (an OLE file with no BIFF workbook in it) settles grey "not
    # compatible", exactly like the .doc / .ppt stubs, never red "failed".
    for legacy in ("spreadsheet-legacy.xls", "word-legacy.doc", "powerpoint-legacy.ppt"):
        spec = ot.OBJECT_TYPES[by_file[legacy]["media_type"]]
        try:
            spec.text_extract_fn({"stored_filename": legacy, "slug": legacy})
            got = "returned"
        except ot.NotCompatibleFile as e:
            got = f"grey: {e}"
        except ot.UnreadableFile as e:
            got = f"RED: {e}"
        check(f"#790: {legacy} reads as grey 'not compatible'", got.startswith("grey: legacy ."), got)
finally:
    storage.path_for = _real_path_for

print(f"{len(manifest['files'])} files generated, {len(skipped)} skipped")
print("FAILED: " + ", ".join(failures) if failures else "ALL PASS")
sys.exit(1 if failures else 0)
