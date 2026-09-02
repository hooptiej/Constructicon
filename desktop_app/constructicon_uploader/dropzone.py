"""A small floating window that accepts dragged-in files for upload.

Unlike the silent Desktop watcher, anything landing here is a deliberate
action, so every drop goes through the same ask-first prompt as an
ambiguous (non-screenshot-named) image from the watcher: a short
description field, then upload. No file-type restriction here — the
watcher only silently trusts image files, but a deliberate drag can be any
file type the server already accepts (capture_events isn't image-only).
"""

import AppKit
import Foundation
import objc

WINDOW_SIZE = (220, 160)


class _DropView(AppKit.NSView):
    on_files_dropped = None  # set by DropZoneWindow after init

    def initWithFrame_(self, frame):
        self = objc.super(_DropView, self).initWithFrame_(frame)
        if self is None:
            return None
        self.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])
        self._label = AppKit.NSTextField.labelWithString_("Drop files here\nto upload")
        self._label.setAlignment_(AppKit.NSTextAlignmentCenter)
        self._label.setTextColor_(AppKit.NSColor.secondaryLabelColor())
        self._label.setFrame_(((0, frame.size.height / 2 - 20), (frame.size.width, 40)))
        self._label.setAutoresizingMask_(AppKit.NSViewWidthSizable | AppKit.NSViewMinYMargin | AppKit.NSViewMaxYMargin)
        self.addSubview_(self._label)
        return self

    def drawRect_(self, rect):
        AppKit.NSColor.controlBackgroundColor().setFill()
        AppKit.NSBezierPath.fillRect_(rect)
        path = AppKit.NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            Foundation.NSInsetRect(rect, 6, 6), 10, 10,
        )
        path.setLineWidth_(2)
        pattern = (6.0, 4.0)
        path.setLineDash_count_phase_(pattern, 2, 0.0)
        AppKit.NSColor.tertiaryLabelColor().setStroke()
        path.stroke()

    def draggingEntered_(self, sender):
        pasteboard = sender.draggingPasteboard()
        if pasteboard.types() and AppKit.NSPasteboardTypeFileURL in pasteboard.types():
            return AppKit.NSDragOperationCopy
        return AppKit.NSDragOperationNone

    def prepareForDragOperation_(self, sender):
        return True

    def performDragOperation_(self, sender):
        pasteboard = sender.draggingPasteboard()
        classes = [AppKit.NSURL]
        options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
        urls = pasteboard.readObjectsForClasses_options_(classes, options) or []
        paths = [str(url.path()) for url in urls if url.path()]
        if paths and self.on_files_dropped is not None:
            self.on_files_dropped(paths)
        return bool(paths)


class DropZoneWindow:
    """Owns the NSWindow lifecycle; kept alive on the app object so it
    isn't garbage-collected out from under Cocoa the moment show() returns."""

    def __init__(self, on_files_dropped):
        width, height = WINDOW_SIZE
        rect = Foundation.NSMakeRect(0, 0, width, height)
        self.window = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            rect,
            AppKit.NSWindowStyleMaskTitled | AppKit.NSWindowStyleMaskClosable | AppKit.NSWindowStyleMaskUtilityWindow,
            AppKit.NSBackingStoreBuffered,
            False,
        )
        self.window.setTitle_("Constructicon")
        self.window.setLevel_(AppKit.NSFloatingWindowLevel)
        self.window.setReleasedWhenClosed_(False)
        self.view = _DropView.alloc().initWithFrame_(rect)
        self.view.on_files_dropped = on_files_dropped
        self.window.setContentView_(self.view)
        self.window.center()

    def show(self):
        self.window.makeKeyAndOrderFront_(None)
        AppKit.NSApp.activateIgnoringOtherApps_(True)

    def toggle(self):
        if self.window.isVisible():
            self.window.orderOut_(None)
        else:
            self.show()
