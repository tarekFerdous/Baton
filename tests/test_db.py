import json

from baton import db


def test_root_dir_round_trip(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    assert db.get_root_dir(conn) is None

    db.set_root_dir(conn, "/some/root")
    assert db.get_root_dir(conn) == "/some/root"

    db.set_root_dir(conn, "/other/root")
    assert db.get_root_dir(conn) == "/other/root"


def test_afk_hours_round_trip(tmp_path):
    db_path = tmp_path / "baton.db"
    conn = db.get_connection(db_path)
    assert db.get_afk_hours(conn) == 6

    db.set_afk_hours(conn, 10)
    assert db.get_afk_hours(conn) == 10

    reopened = db.get_connection(db_path)
    assert db.get_afk_hours(reopened) == 10


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


def test_create_session_seeds_session_type_phase_and_details(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    row_id = db.create_session(
        conn,
        project_id=1,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 3, "title": "T"}},
    )

    row = db.get_session(conn, row_id)
    assert row["session_type"] == "implement"
    assert row["phase"] == "implementing"
    assert json.loads(row["details_json"]) == {"prd": {"number": 3, "title": "T"}}


def test_create_session_defaults_session_type_to_do(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    row_id = db.create_session(conn, project_id=1)

    row = db.get_session(conn, row_id)
    assert row["session_type"] == "do"


def test_has_active_implement_session_detects_live_session_for_prd_number(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    db.create_session(
        conn, project_id=1, session_type="implement", phase="implementing", details={"prd": {"number": 9, "title": "X"}}
    )

    assert db.has_active_implement_session(conn, project_id=1, prd_number=9) is True
    assert db.has_active_implement_session(conn, project_id=1, prd_number=10) is False
    assert db.has_active_implement_session(conn, project_id=2, prd_number=9) is False


def test_has_active_implement_session_ignores_terminal_or_errored_sessions(tmp_path):
    conn = db.get_connection(tmp_path / "baton.db")
    db.create_session(
        conn, project_id=1, session_type="implement", phase="implemented", details={"prd": {"number": 4, "title": "Y"}}
    )
    errored_id = db.create_session(
        conn, project_id=1, session_type="implement", phase="implementing", details={"prd": {"number": 4, "title": "Y"}}
    )
    db.update_session(conn, errored_id, error_text="boom")

    assert db.has_active_implement_session(conn, project_id=1, prd_number=4) is False


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
