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


def test_create_session_starts_in_grilling_phase(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    row_id = db.create_session(conn, project_id=1)

    row = db.get_session(conn, row_id)
    assert row["phase"] == "grilling"
    assert row["available_for_reuse"] == 0


def test_session_phase_transitions_through_state_machine(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    row_id = db.create_session(conn, project_id=1, claude_session_id="s1")

    for phase in ("creating_prd", "creating_issues", "details"):
        db.update_session(conn, row_id, phase=phase)
        assert db.get_session(conn, row_id)["phase"] == phase


def test_claim_available_session_is_scoped_to_its_project(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    session_a = db.create_session(conn, project_id=1, claude_session_id="a1")
    db.mark_session_available(conn, session_a, "a1")

    # Project B has no available session of its own.
    assert db.claim_available_session(conn, project_id=2) is None

    claimed = db.claim_available_session(conn, project_id=1)
    assert claimed["claude_session_id"] == "a1"
    assert claimed["available_for_reuse"] == 0

    # Already claimed -- not handed out twice.
    assert db.claim_available_session(conn, project_id=1) is None


def test_cleanup_sessions_on_shutdown_keeps_only_most_recently_used(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    old = db.create_session(conn, project_id=1, claude_session_id="old")
    conn.execute("UPDATE sessions SET last_activity = '2020-01-01T00:00:00' WHERE id = ?", (old,))

    newer = db.create_session(conn, project_id=2, claude_session_id="newer")
    conn.execute("UPDATE sessions SET last_activity = '2020-01-02T00:00:00' WHERE id = ?", (newer,))

    newest = db.create_session(conn, project_id=1, claude_session_id="newest")
    conn.execute("UPDATE sessions SET last_activity = '2020-01-03T00:00:00' WHERE id = ?", (newest,))
    conn.commit()

    db.cleanup_sessions_on_shutdown(conn)

    remaining = conn.execute("SELECT id FROM sessions").fetchall()
    assert [r["id"] for r in remaining] == [newest]
