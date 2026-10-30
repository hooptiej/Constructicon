"""Users, sessions (#467 step 1) and per-user MCP tokens (#604 step 5).

Part of the core.db package (#547): import it as `from core import db` and use `db.<name>`;
core/db/__init__.py re-exports every name.
"""


from .conn import get_conn


# --- Users and sessions (#467 step 1) -------------------------------------------------------
# core/users.py owns every write (the `_` writers below are called only from there). Reads that
# hand rows to a page or a listing never include password_hash: only `get_user_credentials`
# returns it, for the password check itself.

USER_PUBLIC_COLUMNS = "id, username, display_name, role, created_at, last_login_at, disabled"


def count_users():
    conn = get_conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def count_active_admins(exclude_user_id=None):
    """Admins that are not disabled (optionally leaving one user out: "would this change leave
    an admin?")."""
    conn = get_conn()
    try:
        sql = "SELECT COUNT(*) FROM users WHERE role = 'admin' AND disabled = 0"
        args = []
        if exclude_user_id is not None:
            sql += " AND id != ?"
            args.append(exclude_user_id)
        return conn.execute(sql, args).fetchone()[0]
    finally:
        conn.close()


def list_users():
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {USER_PUBLIC_COLUMNS} FROM users ORDER BY username COLLATE NOCASE")]
    finally:
        conn.close()


def get_user(user_id=None, username=None):
    """One user (no password hash) by id or by username (case-insensitive); None when absent."""
    conn = get_conn()
    try:
        if user_id is not None:
            row = conn.execute(f"SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE id = ?", (user_id,)).fetchone()
        else:
            row = conn.execute(f"SELECT {USER_PUBLIC_COLUMNS} FROM users WHERE username = ? COLLATE NOCASE",
                               (username or "",)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_credentials(username):
    """{id, username, password_hash, disabled} for the sign-in check only; None when absent."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, username, password_hash, disabled FROM users WHERE username = ? "
                           "COLLATE NOCASE", (username or "",)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_user_password_hash(user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def get_session(token_hash, now, created_after=None):
    """The live session for a token hash joined with its (enabled) user, else None. With
    `created_after`, a session created at or before it is dead too (#651's absolute cap)."""
    sql = ("SELECT s.token_hash, s.user_id, s.csrf_token, s.created_at, s.last_seen, s.expires_at, "
           "u.username, u.display_name, u.role FROM sessions s JOIN users u ON u.id = s.user_id "
           "WHERE s.token_hash = ? AND s.expires_at > ? AND u.disabled = 0")
    params = [token_hash, now]
    if created_after is not None:
        sql += " AND s.created_at > ?"
        params.append(created_after)
    conn = get_conn()
    try:
        row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def count_sessions(user_id=None):
    conn = get_conn()
    try:
        if user_id is None:
            return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        return conn.execute("SELECT COUNT(*) FROM sessions WHERE user_id = ?", (user_id,)).fetchone()[0]
    finally:
        conn.close()


def _insert_user(username, display_name, role, password_hash, now):
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO users (username, display_name, role, password_hash, created_at, disabled) "
                           "VALUES (?, ?, ?, ?, ?, 0)", (username, display_name, role, password_hash, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


_USER_WRITABLE = ("display_name", "role", "password_hash", "disabled", "last_login_at")


def _update_user(user_id, **fields):
    bad = set(fields) - set(_USER_WRITABLE)
    if bad:
        raise ValueError(f"not a writable user column: {sorted(bad)}")
    if not fields:
        return
    conn = get_conn()
    try:
        conn.execute(f"UPDATE users SET {', '.join(f'{c} = ?' for c in fields)} WHERE id = ?",
                     list(fields.values()) + [user_id])
        conn.commit()
    finally:
        conn.close()


def _delete_user(user_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,))  # #604 step 5
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()
    finally:
        conn.close()


# --- #604 step 5: per-user MCP tokens (core/mcp_tokens.py owns every write) -----------------------

MCP_TOKEN_PUBLIC_COLUMNS = "id, user_id, name, created_at, rotated_at, last_used_at"


def list_mcp_tokens(user_id):
    """A user's MCP tokens, newest first: {id, user_id, name, created_at, rotated_at, last_used_at}
    (never the hash)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,))]
    finally:
        conn.close()


def list_all_mcp_tokens():
    """(#766) Every user's MCP tokens, for Admin > Users: {id, user_id, name, created_at, rotated_at,
    last_used_at}, newest first per user (never the hash)."""
    conn = get_conn()
    try:
        return [dict(r) for r in conn.execute(
            f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens ORDER BY user_id, created_at DESC, id DESC")]
    finally:
        conn.close()


def _delete_user_mcp_tokens(user_id):
    """(#766) Deletes every MCP token of one user (core/mcp_tokens.revoke_all); returns how many."""
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM mcp_tokens WHERE user_id = ?", (user_id,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def get_mcp_token(token_id):
    conn = get_conn()
    try:
        r = conn.execute(f"SELECT {MCP_TOKEN_PUBLIC_COLUMNS} FROM mcp_tokens WHERE id = ?", (token_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def find_mcp_token(token_hash):
    """The token row (no hash) whose sha256 is `token_hash`, plus its user's username / role /
    disabled (None for a deleted user); None when no such token."""
    conn = get_conn()
    try:
        r = conn.execute(
            "SELECT t.id, t.user_id, t.name, t.created_at, t.rotated_at, t.last_used_at, u.username, u.role, "
            "u.disabled, u.display_name FROM mcp_tokens t LEFT JOIN users u ON u.id = t.user_id WHERE t.token_hash = ?",
            (token_hash,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _insert_mcp_token(user_id, name, token_hash, now):
    conn = get_conn()
    try:
        cur = conn.execute("INSERT INTO mcp_tokens (user_id, name, token_hash, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, name, token_hash, now))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _rehash_mcp_token(token_id, token_hash, now):
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET token_hash = ?, rotated_at = ?, last_used_at = NULL WHERE id = ?",
                     (token_hash, now, token_id))
        conn.commit()
    finally:
        conn.close()


def _rename_mcp_token(token_id, name):
    """#778: a token's new name (core/mcp_tokens.rename). Credits read the name at read time."""
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET name = ? WHERE id = ?", (name, token_id))
        conn.commit()
    finally:
        conn.close()


def _delete_mcp_token(token_id):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM mcp_tokens WHERE id = ?", (token_id,))
        conn.commit()
    finally:
        conn.close()


def _touch_mcp_token(token_id, now):
    conn = get_conn()
    try:
        conn.execute("UPDATE mcp_tokens SET last_used_at = ? WHERE id = ?", (now, token_id))
        conn.commit()
    finally:
        conn.close()


def _insert_session(token_hash, user_id, csrf_token, now, expires_at):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO sessions (token_hash, user_id, csrf_token, created_at, last_seen, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (token_hash, user_id, csrf_token, now, now, expires_at))
        conn.commit()
    finally:
        conn.close()


def _touch_session(token_hash, now, expires_at):
    conn = get_conn()
    try:
        conn.execute("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                     (now, expires_at, token_hash))
        conn.commit()
    finally:
        conn.close()


def _delete_session(token_hash):
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _delete_user_sessions(user_id, keep_token_hash=None):
    conn = get_conn()
    try:
        if keep_token_hash:
            n = conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                             (user_id, keep_token_hash)).rowcount
        else:
            n = conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()


def _purge_expired_sessions(now, created_before=None):
    """Deletes sessions past expires_at, and (with `created_before`) those created at or before
    it (#651's absolute cap)."""
    conn = get_conn()
    try:
        if created_before is None:
            n = conn.execute("DELETE FROM sessions WHERE expires_at <= ?", (now,)).rowcount
        else:
            n = conn.execute("DELETE FROM sessions WHERE expires_at <= ? OR created_at <= ?",
                             (now, created_before)).rowcount
        conn.commit()
        return n
    finally:
        conn.close()
