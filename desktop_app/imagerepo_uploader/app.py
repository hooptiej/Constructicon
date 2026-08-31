"""Menu-bar app: silent Desktop-folder watch + a drop zone for everything
else. Status is a single colored-circle emoji in the menu bar title —
green/yellow/red, the same vocabulary as the OCR lamp on the web app's
image detail page, so the color already means something to every tech.

Uploads are processed one at a time through a single background queue,
deliberately — not for throughput, but because running OCR concurrently
against the server turned out to cause spurious failures during a batch
upload (see core/ocr.py's OCR_SEMAPHORE on the server, and the web upload
drawer's switch to sequential uploads). A lone desktop tech is unlikely to
generate a real burst, but serializing costs nothing here and rules the
same failure mode out entirely.
"""

import queue
import threading
import webbrowser
from pathlib import Path

import rumps
from PyObjCTools.AppHelper import callAfter

from . import api, config
from .dropzone import DropZoneWindow
from .watcher import DesktopWatcher

STATUS_IDLE = "🟢"
STATUS_UPLOADING = "🟡"
STATUS_ERROR = "🔴"
ERROR_DISPLAY_SECONDS = 8


class ImageRepoUploaderApp(rumps.App):
    def __init__(self):
        super().__init__("imagerepo", title=STATUS_IDLE, quit_button=None)
        self.config = config.load_config()

        self._upload_queue = queue.Queue()
        self._error_revert_timer = None

        self.dropzone = DropZoneWindow(on_files_dropped=self._enqueue_dropped_files)

        self.menu = [
            rumps.MenuItem("Show Drop Zone", callback=self._toggle_dropzone),
            None,
            rumps.MenuItem("Change Watched Folder…", callback=self._prompt_for_watch_folder),
            rumps.MenuItem("Change Server URL…", callback=self._prompt_for_base_url),
            None,
            rumps.MenuItem("Open imagerepo", callback=self._open_web_app),
            None,
            rumps.MenuItem("Quit", callback=rumps.quit_application),
        ]

        self.watcher = None
        self._start_watcher()

        threading.Thread(target=self._upload_worker, daemon=True).start()

    # --- Watcher lifecycle ---

    def _start_watcher(self):
        if self.watcher is not None:
            self.watcher.stop()
        folder = Path(self.config["watch_folder"]).expanduser()
        if not folder.is_dir():
            rumps.notification("imagerepo", "Watched folder not found", str(folder))
            return
        self.watcher = DesktopWatcher(folder, self._enqueue_screenshot, self._enqueue_ambiguous_image)
        self.watcher.start()

    # --- Intake: three entry points feed the same upload queue ---

    def _enqueue_screenshot(self, path):
        self._upload_queue.put((path, ""))

    def _enqueue_ambiguous_image(self, path):
        self._ask_then_enqueue(path)

    def _enqueue_dropped_files(self, paths):
        for raw_path in paths:
            self._ask_then_enqueue(Path(raw_path))

    def _ask_then_enqueue(self, path):
        # rumps.Window must run on the main thread — this can be called
        # from the watcher's background thread or (already-main-thread)
        # Cocoa drag callback, so hop over via the run loop rather than
        # assuming which one we're on.
        callAfter(self._ask_then_enqueue_main, path)

    def _ask_then_enqueue_main(self, path):
        response = rumps.Window(
            title="Upload to imagerepo?",
            message=path.name,
            default_text="",
            ok="Upload",
            cancel="Skip",
            dimensions=(300, 40),
        ).run()
        if response.clicked:
            self._upload_queue.put((path, response.text.strip()))

    # --- Upload worker ---

    def _upload_worker(self):
        while True:
            path, description = self._upload_queue.get()
            self._set_status(STATUS_UPLOADING)
            try:
                api.upload_file(self.config["base_url"], str(path), description=description)
            except api.DuplicateUploadError:
                pass  # already in imagerepo — not an error, nothing to report
            except api.UploadError as e:
                rumps.notification("imagerepo upload failed", path.name, str(e))
                self._flash_error()
            else:
                self._set_status(STATUS_IDLE)
            self._upload_queue.task_done()

    def _set_status(self, status):
        self.title = status

    def _flash_error(self):
        self.title = STATUS_ERROR
        if self._error_revert_timer is not None:
            self._error_revert_timer.stop()
        self._error_revert_timer = rumps.Timer(self._revert_to_idle, ERROR_DISPLAY_SECONDS)
        self._error_revert_timer.start()

    def _revert_to_idle(self, _timer):
        self.title = STATUS_IDLE
        self._error_revert_timer.stop()
        self._error_revert_timer = None

    # --- Menu actions ---

    def _toggle_dropzone(self, _sender):
        self.dropzone.toggle()

    def _prompt_for_watch_folder(self, _sender):
        response = rumps.Window(
            title="Watched folder",
            message="Folder to silently watch for new screenshots:",
            default_text=self.config["watch_folder"],
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 40),
        ).run()
        if not response.clicked or not response.text.strip():
            return
        self.config["watch_folder"] = response.text.strip()
        config.save_config(self.config)
        self._start_watcher()

    def _prompt_for_base_url(self, _sender):
        response = rumps.Window(
            title="imagerepo server URL",
            message="Base URL of the imagerepo server:",
            default_text=self.config["base_url"],
            ok="Save",
            cancel="Cancel",
            dimensions=(320, 40),
        ).run()
        if not response.clicked or not response.text.strip():
            return
        self.config["base_url"] = response.text.strip()
        config.save_config(self.config)

    def _open_web_app(self, _sender):
        webbrowser.open(self.config["base_url"])
