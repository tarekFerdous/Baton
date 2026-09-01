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

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
