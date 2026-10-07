# Constructicon desktop uploader

Menu-bar app: silently uploads screenshots from a watched folder (Desktop
by default), plus a drop zone for anything else. Source only — this needs
building on an actual Mac.

## Build

Double-click **`Build.command`** in this folder. It sets up a venv,
installs dependencies, and runs the py2app build — the terminal window
stays open with the result (or a specific error) until you press a key.

Produces `dist/Constructicon Uploader.app`. Move it to `/Applications` and
open it.

Prefer to do it by hand instead:

```
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py py2app
```

## First run

1. Launch the app. It defaults to watching your Desktop and pointing at the
   Constructicon dev server — change either from the menu bar icon
   (**Change Watched Folder…** / **Change Server URL…**) if needed.
2. Menu bar icon shows status: 🟢 idle, 🟡 uploading, 🔴 last upload failed
   (check Notification Center for the actual error).

3. **Set the install token** (menu bar icon > **Set Install Token…**). Since
   Constructicon #467 step 2 the server refuses anonymous uploads: the app sends
   the install token as `Authorization: Bearer <token>`. Ask the Constructicon
   admin for it (it's the token file the server's containers mount). Without it,
   uploads fail with "The server needs the install token…". The token is stored
   in `~/Library/Application Support/Constructicon Uploader/config.json`, which
   the app writes owner-only (mode 600), and is never shown back in full.

## Known gap

This build hasn't had a real hands-on smoke test yet — pyobjc/rumps can't
be exercised in a sandboxed environment, so the drag-and-drop drop zone in
particular needs verifying on a real Mac before this goes to other techs.
