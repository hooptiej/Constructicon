"""Watches the configured folder (Desktop, by default) for new screenshots.

Two callbacks, not one: a file that's clearly a screenshot by name (macOS's
own "Screenshot ..."/"Screen Shot ..." convention) goes straight to silent
auto-upload. Anything else that's still an image — a PNG dragged out of
Photos, a saved diagram, a logo — is NOT assumed to be upload-worthy
capture-event material, so it's routed to the same ask-first prompt as the
manual drop zone instead of silently appearing in the gallery.

IMAGE_EXTENSIONS matches core/storage.py's IMAGE_EXTENSIONS server-side —
kept in sync by hand since this app doesn't share a Python environment
with the server.
"""

import re
import threading
import time
from pathlib import Path

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}

# Mac-only today, but the folder-watch approach is the same on Windows, so
# this stays a list of recognized OS naming conventions rather than one
# Mac-specific pattern — a future Windows build only needs to add its own
# entry here. Known forms: macOS "Screenshot 2026-08-21 at 11.34.32 AM.png"
# and the pre-Catalina "Screen Shot ..."; Windows PrtScn's default
# "Screenshot (12).png" and the Snip & Sketch "Screenshot_2026-08-21_113432.png".
# All of these share a "screenshot"/"screen shot" prefix followed by a
# separator — deliberately NOT \b for this: regex treats "_" as a word
# character, so \b fails to match right where the Windows underscore form
# needs it to. Listing the real separator set instead.
SCREENSHOT_NAME_RE = re.compile(r"^screen ?shot(?=[\s_().-]|$)", re.IGNORECASE)

# A screenshot write is effectively instant, but not always atomic (iCloud
# Desktop sync in particular can briefly show a partial file, pause, then
# keep writing). One matching pair of readings isn't enough to prove that —
# it also matches "checked exactly during the pause" — so this requires two
# consecutive stable pairs before treating the file as done.
SETTLE_CHECK_INTERVAL_SECONDS = 0.4
SETTLE_STABLE_READINGS_REQUIRED = 2
SETTLE_MAX_WAIT_SECONDS = 10


def _is_settled(path):
    try:
        size_before = path.stat().st_size
    except OSError:
        return False
    stable_count = 0
    waited = 0.0
    while waited < SETTLE_MAX_WAIT_SECONDS:
        time.sleep(SETTLE_CHECK_INTERVAL_SECONDS)
        waited += SETTLE_CHECK_INTERVAL_SECONDS
        try:
            size_now = path.stat().st_size
        except OSError:
            return False
        if size_now == size_before and size_now > 0:
            stable_count += 1
            if stable_count >= SETTLE_STABLE_READINGS_REQUIRED:
                return True
        else:
            stable_count = 0
        size_before = size_now
    return False


class _Handler(FileSystemEventHandler):
    def __init__(self, on_screenshot, on_ambiguous_image):
        self.on_screenshot = on_screenshot
        self.on_ambiguous_image = on_ambiguous_image
        self._seen = set()
        self._lock = threading.Lock()

    def on_created(self, event):
        self._handle(event.src_path)

    def on_moved(self, event):
        self._handle(event.dest_path)

    def _handle(self, raw_path):
        path = Path(raw_path)
        if path.suffix.lower() not in IMAGE_EXTENSIONS or path.name.startswith("."):
            return
        with self._lock:
            if raw_path in self._seen:
                return
            self._seen.add(raw_path)
        # Settling and the eventual upload both take real time — do this off
        # the watchdog dispatch thread so a second file landing right after
        # isn't stuck waiting behind it.
        threading.Thread(target=self._process, args=(path,), daemon=True).start()

    def _process(self, path):
        if not _is_settled(path):
            return  # disappeared, or never finished writing — nothing to upload
        if SCREENSHOT_NAME_RE.match(path.name):
            self.on_screenshot(path)
        else:
            self.on_ambiguous_image(path)


class DesktopWatcher:
    def __init__(self, folder, on_screenshot, on_ambiguous_image):
        self.folder = folder
        self._handler = _Handler(on_screenshot, on_ambiguous_image)
        self._observer = Observer()

    def start(self):
        self._observer.schedule(self._handler, str(self.folder), recursive=False)
        self._observer.start()

    def stop(self):
        self._observer.stop()
        self._observer.join(timeout=5)
