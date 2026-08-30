"""py2app build script.

Build on an actual Mac (this needs Xcode command line tools):
    pip install -r requirements.txt
    python setup.py py2app

Produces dist/ImageRepo Uploader.app.
"""

from setuptools import setup

APP = ["run.py"]
OPTIONS = {
    "argv_emulation": False,
    # py2app bundles these defensively even though nothing here imports
    # them — no GUI beyond rumps/AppKit (no tkinter), no CJK text handling,
    # no test-suite internals. Cuts ~7MB of dead weight from the bundle.
    "excludes": [
        "tkinter", "_tkinter",
        "test", "unittest", "distutils",
        "_codecs_cn", "_codecs_hk", "_codecs_iso2022", "_codecs_jp", "_codecs_kr", "_codecs_tw", "_multibytecodec",
    ],
    "plist": {
        "CFBundleName": "ImageRepo Uploader",
        "CFBundleDisplayName": "ImageRepo Uploader",
        "CFBundleIdentifier": "net.computercats.imagerepo-uploader",
        "LSUIElement": True,  # menu-bar-only — no Dock icon, no app switcher entry
        "NSHumanReadableCopyright": "Computer Cats",
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
