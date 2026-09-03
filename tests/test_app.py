import subprocess

from baton import db, session_runner
from baton.web import app as app_module


def _init_repo(path, remote_url):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)


def test_app_state_defaults_afk_hours_to_six(client):
    state = client.get("/api/app-state").json()
    assert state["afk_hours"] == 6


def test_set_afk_hours_persists_and_reflects_in_app_state(client):
    resp = client.post("/api/settings/afk-hours", json={"afk_hours": 12})
    assert resp.json() == {"afk_hours": 12}

    state = client.get("/api/app-state").json()
    assert state["afk_hours"] == 12


def test_app_state_defaults_parallel_implementation_to_true(client):
    state = client.get("/api/app-state").json()
    assert state["parallel_implementation"] is True


def test_set_parallel_implementation_persists_and_reflects_in_app_state(client):
    resp = client.post("/api/settings/parallel-implementation", json={"parallel_implementation": False})
    assert resp.json() == {"parallel_implementation": False}

    state = client.get("/api/app-state").json()
    assert state["parallel_implementation"] is False

    resp = client.post("/api/settings/parallel-implementation", json={"parallel_implementation": True})
    assert resp.json() == {"parallel_implementation": True}
    assert client.get("/api/app-state").json()["parallel_implementation"] is True


def test_root_dir_change_without_confirmation_leaves_projects_untouched(client, tmp_path):
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    _init_repo(root_a / "repo1", "https://github.com/x/repo1.git")

    resp = client.post("/api/settings/root-dir", json={"root_dir": str(root_a)})
    assert resp.json()["projects"], "expected repo1 to be discovered"

    root_b = tmp_path / "root_b"
    root_b.mkdir()

    resp = client.post("/api/settings/root-dir", json={"root_dir": str(root_b)})
    assert resp.json() == {"needs_confirmation": True}

    projects = client.get("/api/app-state").json()["projects"]
    assert len(projects) == 1
    assert projects[0]["name"] == "repo1"


def test_root_dir_change_with_confirmation_clears_projects(client, tmp_path):
    root_a = tmp_path / "root_a"
    root_a.mkdir()
    _init_repo(root_a / "repo1", "https://github.com/x/repo1.git")
    client.post("/api/settings/root-dir", json={"root_dir": str(root_a)})

    root_b = tmp_path / "root_b"
    root_b.mkdir()
    _init_repo(root_b / "repo2", "https://github.com/x/repo2.git")

    resp = client.post("/api/settings/root-dir", json={"root_dir": str(root_b), "confirm": True})
    names = {p["name"] for p in resp.json()["projects"]}
    assert names == {"repo2"}


def test_project_open_close_reopen_round_trip(client, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo1", "https://github.com/x/repo1.git")
    client.post("/api/settings/root-dir", json={"root_dir": str(root)})

    project_id = client.get("/api/app-state").json()["projects"][0]["id"]

    opened = client.post(f"/api/projects/{project_id}/open").json()
    assert opened["project"]["name"] == "repo1"
    assert opened["session_state"] == {}

    state = client.get("/api/app-state").json()
    assert state["active_project"]["id"] == project_id

    client.post(
        f"/api/projects/{project_id}/close",
        json={"session_state": {"session_id": "abc", "console_text": "hi"}},
    )

    state = client.get("/api/app-state").json()
    assert state["active_project"] is None

    reopened = client.post(f"/api/projects/{project_id}/open").json()
    assert reopened["session_state"] == {"session_id": "abc", "console_text": "hi"}


def test_prds_endpoint_returns_sorted_blockage_annotated_list(client, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo1", "https://github.com/x/repo1.git")
    client.post("/api/settings/root-dir", json={"root_dir": str(root)})
    project_id = client.get("/api/app-state").json()["projects"][0]["id"]
    client.post(f"/api/projects/{project_id}/open")

    prds = [
        {"number": 34, "title": "AFK feature", "body": "", "labels": []},
        {"number": 30, "title": "Grilling fix", "body": "", "labels": []},
    ]
    all_open_issues = [
        {"number": 31, "title": "child", "body": "## Parent\n\n#30\n\n## Blocked by\n\nNone - can start immediately.\n"},
        {
            "number": 35,
            "title": "child",
            "body": "## Parent\n\n#34\n\n## Blocked by\n\n- #99\n",
        },
        {"number": 99, "title": "blocker", "body": "## Blocked by\n\nNone - can start immediately.\n"},
    ]
    monkeypatch.setattr(app_module, "_fetch_ready_prds", lambda cwd: prds)
    monkeypatch.setattr(app_module, "_fetch_all_open_issues", lambda cwd: all_open_issues)

    resp = client.get(f"/api/projects/{project_id}/prds")
    assert resp.json() == {
        "prds": [
            {"number": 30, "title": "Grilling fix", "blocked": False},
            {"number": 34, "title": "AFK feature", "blocked": True},
        ]
    }


def test_prds_endpoint_returns_empty_for_non_active_project(client):
    resp = client.get("/api/projects/999/prds")
    assert resp.json() == {"prds": []}


def test_start_implement_endpoint_rejects_duplicate_session_for_same_prd(client, tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    _init_repo(root / "repo1", "https://github.com/x/repo1.git")
    client.post("/api/settings/root-dir", json={"root_dir": str(root)})
    project_id = client.get("/api/app-state").json()["projects"][0]["id"]
    client.post(f"/api/projects/{project_id}/open")

    async def _noop_job(card_id, prd_number, *, cwd):
        return None

    monkeypatch.setattr(session_runner, "start_implement_job", _noop_job)

    first = client.post("/api/session/start-implement", json={"number": 5, "title": "My PRD"})
    assert "card_id" in first.json()

    # A second click for the same PRD number while the first is still live
    # must be rejected without creating a second session row.
    second = client.post("/api/session/start-implement", json={"number": 5, "title": "My PRD"})
    assert second.json() == {"error": "Already implementing"}

    conn = db.get_connection()
    sessions = db.list_sessions_for_project(conn, project_id)
    implement_sessions = [s for s in sessions if s["session_type"] == "implement"]
    assert len(implement_sessions) == 1

    # A different PRD number is unaffected by the first one's in-flight session.
    third = client.post("/api/session/start-implement", json={"number": 6, "title": "Other PRD"})
    assert "card_id" in third.json()
