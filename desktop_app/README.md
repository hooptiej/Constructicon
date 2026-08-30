# imagerepo desktop uploader

Menu-bar app: silently uploads screenshots from a watched folder (Desktop
by default), plus a drop zone for anything else. Source only — this needs
building on an actual Mac.

## Build

Double-click **`Build.command`** in this folder. It sets up a venv,
installs dependencies, and runs the py2app build — the terminal window
stays open with the result (or a specific error) until you press a key.

Produces `dist/ImageRepo Uploader.app`. Move it to `/Applications` and
open it.

Prefer to do it by hand instead:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py py2app
```

## First run

1. Get an API token: imagerepo → Account → Desktop uploader tokens → **+ New token**.
2. Launch the app. It'll prompt for the token on first run (menu bar icon →
   **Set API Token…** if you need to do it again later).
3. Menu bar icon shows status: 🟢 idle, 🟡 uploading, 🔴 last upload failed
   (check Notification Center for the actual error).

## Known gap

This build hasn't had a real hands-on smoke test yet — pyobjc/rumps can't
be exercised in a sandboxed environment, so the drag-and-drop drop zone in
particular needs verifying on a real Mac before this goes to other techs.
