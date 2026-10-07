"""Safe-by-construction environment for the in-process test scripts (#578, #576).

A test that writes files, uploads, exports, trashes or calls delete-all must never touch the
install it runs inside (constructicon-test or, by mistake, prod). Call `isolate()` FIRST, before
any `core` / `web` import:

    import _testenv
    TMP = _testenv.isolate("my-test-")        # temp DB + storage + exports, env vars set, asserted

It creates a fresh temp directory, points CONSTRUCTICON_DB_PATH, CONSTRUCTICON_STORAGE_DIR and
CONSTRUCTICON_EXPORTS_DIR into it (always overwriting whatever was set), then runs
`assert_isolated()` and exits non-zero if any resolved location is not inside a temp dir or is
inside the repo checkout. `core/paths.py` reads the environment on every call, so the guard also
holds for modules imported afterwards; call `assert_isolated()` again after the imports (it also
checks `db.DB_PATH` and `paths.*` as the code actually resolves them) for the belt and braces.

Tests that deliberately talk to a LIVE server (urllib to localhost, e.g. test_office.py) don't use
this module: they say so at the top and never call delete-all or empty-trash.
"""

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _inside(child, parent):
    try:
        Path(child).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def _refuse(what, path):
    sys.exit(f"_testenv: REFUSING TO RUN: {what} resolves to {path}, which is not a throwaway "
             f"temp directory (or is inside the repo checkout). A test must never touch a real "
             f"install's data.")


def assert_isolated():
    """Exit unless the DB, storage, trash, backups and exports locations, as the code resolves
    them right now, are all inside the system temp dir and outside the repo checkout."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from core import paths
    resolved = {
        "storage dir": paths.storage_dir(),
        "trash dir": paths.trash_dir(),
        "backup dir": paths.backup_dir(),
        "exports dir": paths.exports_dir(),
        "current export dir": paths.current_export_dir(),
    }
    db_path = os.environ.get("CONSTRUCTICON_DB_PATH")
    if not db_path:
        _refuse("the DB path (CONSTRUCTICON_DB_PATH is unset)", "<repo default>")
    resolved["database"] = Path(db_path)
    try:  # when core.db is already imported, trust what it actually resolved
        mod = sys.modules.get("core.db")
        if mod is not None:
            resolved["database (core.db.DB_PATH)"] = Path(mod.DB_PATH)
    except AttributeError:
        pass
    tmp_root = Path(tempfile.gettempdir())
    for what, path in resolved.items():
        if not _inside(path, tmp_root) or _inside(path, ROOT):
            _refuse(what, path)


def isolate(prefix="test-", db_name="test.db"):
    """Create the temp dir, set the three env vars, assert, and return the temp dir (a str)."""
    tmp = tempfile.mkdtemp(prefix=prefix)
    os.environ["CONSTRUCTICON_DB_PATH"] = os.path.join(tmp, db_name)
    os.environ["CONSTRUCTICON_STORAGE_DIR"] = os.path.join(tmp, "storage")
    os.environ["CONSTRUCTICON_EXPORTS_DIR"] = os.path.join(tmp, "exports")
    os.makedirs(os.environ["CONSTRUCTICON_STORAGE_DIR"], exist_ok=True)
    os.makedirs(os.environ["CONSTRUCTICON_EXPORTS_DIR"], exist_ok=True)
    use_token()
    assert_isolated()
    return tmp


# The per-run install token isolate() configures (see client()).
import secrets as _secrets  # noqa: E402
TOKEN = "test-" + _secrets.token_urlsafe(40)


def use_token():
    """#467 step 2: roles are enforced, so a test talking to the app in-process needs a credential.
    Configures a fresh random install token for this run (never the container's own: the token-file
    variables are dropped, so a test run inside constructicon-test can't read or use the real one).
    isolate() calls it; a test that sets up its own temp DB calls it before importing core/web."""
    for name in ("CONSTRUCTICON_INSTALL_TOKEN_FILE", "CONSTRUCTICON_MCP_TOKEN", "CONSTRUCTICON_MCP_TOKEN_FILE"):
        os.environ.pop(name, None)
    os.environ["CONSTRUCTICON_INSTALL_TOKEN"] = TOKEN
    mod = sys.modules.get("core.install_token")
    if mod is not None:
        mod.reset_cache()


def auth_headers():
    """{"Authorization": "Bearer <TOKEN>"}: an admin request as actor `token`."""
    return {"Authorization": f"Bearer {TOKEN}"}


def client(app, **kwargs):
    """A TestClient that sends the install token on every request (role admin, actor `token`),
    for tests that exercise routes rather than sign-in. Tests about sign-in use a bare TestClient."""
    from fastapi.testclient import TestClient
    headers = {**auth_headers(), **(kwargs.pop("headers", None) or {})}
    return TestClient(app, headers=headers, **kwargs)
