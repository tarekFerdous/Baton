from baton import db


def test_root_dir_round_trip(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    assert db.get_root_dir(conn) is None

    db.set_root_dir(conn, "/some/root")
    assert db.get_root_dir(conn) == "/some/root"

    db.set_root_dir(conn, "/other/root")
    assert db.get_root_dir(conn) == "/other/root"


def test_project_open_close_reopen_preserves_state(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    db.upsert_project(conn, "/repos/foo", "foo", "main")

    [row] = db.list_projects(conn)
    assert db.load_session_state(row) == {}

    db.mark_opened(conn, row["id"])
    db.save_session_state(conn, row["id"], {"session_id": "abc123", "console_text": "hello"})

    reopened = db.get_project(conn, row["id"])
    assert db.load_session_state(reopened) == {"session_id": "abc123", "console_text": "hello"}
    assert reopened["last_opened"] is not None


def test_clear_projects_removes_all_rows(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    db.upsert_project(conn, "/repos/foo", "foo", "main")
    db.upsert_project(conn, "/repos/bar", "bar", "dev")
    assert len(db.list_projects(conn)) == 2

    db.clear_projects(conn)
    assert db.list_projects(conn) == []
