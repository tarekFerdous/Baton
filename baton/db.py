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

PHASES = ("grilling", "creating_prd", "creating_issues", "details")


def create_session(conn: sqlite3.Connection, project_id: int, claude_session_id: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """
        INSERT INTO sessions (project_id, claude_session_id, phase, last_activity, created_at)
        VALUES (?, ?, 'grilling', ?, ?)
        """,
        (project_id, claude_session_id, now, now),
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
