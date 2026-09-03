"""SQLite persistence for root directory settings and discovered projects.

Uses stdlib `sqlite3` -- no new dependency, per CLAUDE.md's minimal-footprint
approach. `db_path` is accepted explicitly everywhere so tests can point at
an isolated temp database instead of the real one under the user's home dir.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / ".baton" / "baton.db"


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    # Concurrent sessions each open their own connection (sqlite3 connections
    # aren't shareable across threads) and can write around the same time --
    # WAL lets readers and a writer proceed together, and busy_timeout makes
    # a writer wait out a brief conflict instead of raising "database is locked".
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            root_dir TEXT
        )
        """
    )
    # CREATE TABLE IF NOT EXISTS above won't add new columns to a settings
    # table that already exists on disk from before this column existed --
    # add it here, no-op'ing if it's already present.
    existing_columns = {row["name"] for row in conn.execute("PRAGMA table_info(settings)")}
    if "afk_hours" not in existing_columns:
        conn.execute("ALTER TABLE settings ADD COLUMN afk_hours INTEGER NOT NULL DEFAULT 6")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            path TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            last_opened TEXT,
            branch TEXT,
            session_state TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            claude_session_id TEXT,
            phase TEXT NOT NULL DEFAULT 'grilling',
            console_text TEXT NOT NULL DEFAULT '',
            interview_json TEXT,
            details_json TEXT,
            error_text TEXT,
            needs_github_login INTEGER NOT NULL DEFAULT 0,
            last_activity TEXT NOT NULL,
            available_for_reuse INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
        """
    )
    # Same guarded-migration approach as afk_hours above: a sessions table
    # that already exists on disk from before session_type existed won't get
    # the new column from CREATE TABLE IF NOT EXISTS.
    session_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sessions)")}
    if "session_type" not in session_columns:
        conn.execute("ALTER TABLE sessions ADD COLUMN session_type TEXT NOT NULL DEFAULT 'do'")
    conn.commit()
    return conn


def get_root_dir(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT root_dir FROM settings WHERE id = 1").fetchone()
    return row["root_dir"] if row else None


def set_root_dir(conn: sqlite3.Connection, root_dir: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (id, root_dir) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET root_dir = excluded.root_dir
        """,
        (root_dir,),
    )
    conn.commit()


def get_afk_hours(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT afk_hours FROM settings WHERE id = 1").fetchone()
    return row["afk_hours"] if row else 6


def set_afk_hours(conn: sqlite3.Connection, hours: int) -> None:
    conn.execute(
        """
        INSERT INTO settings (id, afk_hours) VALUES (1, ?)
        ON CONFLICT(id) DO UPDATE SET afk_hours = excluded.afk_hours
        """,
        (hours,),
    )
    conn.commit()


def clear_projects(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM projects")
    conn.commit()


def list_projects(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM projects ORDER BY last_opened IS NULL, last_opened DESC, name ASC"
    ).fetchall()


def get_project(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()


def upsert_project(conn: sqlite3.Connection, path: str, name: str, branch: str) -> None:
    conn.execute(
        """
        INSERT INTO projects (path, name, branch) VALUES (?, ?, ?)
        ON CONFLICT(path) DO UPDATE SET name = excluded.name, branch = excluded.branch
        """,
        (path, name, branch),
    )
    conn.commit()


def mark_opened(conn: sqlite3.Connection, project_id: int) -> None:
    conn.execute(
        "UPDATE projects SET last_opened = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), project_id),
    )
    conn.commit()


def save_session_state(conn: sqlite3.Connection, project_id: int, state: dict) -> None:
    conn.execute(
        "UPDATE projects SET session_state = ? WHERE id = ?",
        (json.dumps(state), project_id),
    )
    conn.commit()


def load_session_state(row: sqlite3.Row) -> dict:
    raw = row["session_state"]
    return json.loads(raw) if raw else {}


# --- Concurrent session orchestration (PRD 2) -----------------------------

PHASES = ("grilling", "creating_prd", "creating_issues", "details", "implementing", "implemented")


def create_session(
    conn: sqlite3.Connection,
    project_id: int,
    claude_session_id: str | None = None,
    *,
    session_type: str = "do",
    phase: str = "grilling",
    details: dict | None = None,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    details_json = json.dumps(details) if details is not None else None
    cur = conn.execute(
        """
        INSERT INTO sessions (project_id, claude_session_id, session_type, phase, details_json, last_activity, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (project_id, claude_session_id, session_type, phase, details_json, now, now),
    )
    conn.commit()
    return cur.lastrowid


def get_session(conn: sqlite3.Connection, session_row_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM sessions WHERE id = ?", (session_row_id,)).fetchone()


def list_sessions_for_project(conn: sqlite3.Connection, project_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM sessions WHERE project_id = ? ORDER BY created_at ASC",
        (project_id,),
    ).fetchall()


def update_session(conn: sqlite3.Connection, session_row_id: int, **fields) -> None:
    if not fields:
        return
    fields = {**fields, "last_activity": datetime.now(timezone.utc).isoformat()}
    columns = ", ".join(f"{key} = ?" for key in fields)
    conn.execute(
        f"UPDATE sessions SET {columns} WHERE id = ?",
        (*fields.values(), session_row_id),
    )
    conn.commit()


def mark_session_available(conn: sqlite3.Connection, session_row_id: int, claude_session_id: str) -> None:
    update_session(
        conn,
        session_row_id,
        claude_session_id=claude_session_id,
        available_for_reuse=1,
    )


def claim_available_session(conn: sqlite3.Connection, project_id: int) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT * FROM sessions
        WHERE project_id = ? AND available_for_reuse = 1
        ORDER BY last_activity DESC
        LIMIT 1
        """,
        (project_id,),
    ).fetchone()
    if row is not None:
        conn.execute("UPDATE sessions SET available_for_reuse = 0 WHERE id = ?", (row["id"],))
        conn.commit()
        row = get_session(conn, row["id"])
    return row


def has_active_implement_session(conn: sqlite3.Connection, project_id: int, prd_number: int) -> bool:
    """True if this project already has a live (non-terminal) implement
    session for `prd_number`. There's no dedicated PRD-number column on
    `sessions`, so this filters the (short) candidate list in Python by
    peeking into each row's `details_json` (seeded at creation with
    `{"prd": {"number": ...}}`)."""
    rows = conn.execute(
        """
        SELECT details_json FROM sessions
        WHERE project_id = ? AND session_type = 'implement' AND phase = 'implementing' AND error_text IS NULL
        """,
        (project_id,),
    ).fetchall()
    for row in rows:
        if not row["details_json"]:
            continue
        details = json.loads(row["details_json"])
        prd = details.get("prd") if isinstance(details, dict) else None
        if prd and prd.get("number") == prd_number:
            return True
    return False


def cleanup_sessions_on_shutdown(conn: sqlite3.Connection) -> None:
    """Keep only the single most-recently-used session across all projects."""
    rows = conn.execute(
        "SELECT id FROM sessions WHERE claude_session_id IS NOT NULL ORDER BY last_activity DESC"
    ).fetchall()
    if not rows:
        return
    keep_id = rows[0]["id"]
    conn.execute("DELETE FROM sessions WHERE id != ?", (keep_id,))
    conn.commit()
