import subprocess

import pytest

from baton import db
from baton.cli_client import ClaudeCLIError
from baton.web import app as app_module


def _init_repo(path, remote_url):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)


def _open_project(client, tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    _init_repo(root / "repo", f"https://github.com/x/{name}.git")
    client.post("/api/settings/root-dir", json={"root_dir": str(root), "confirm": True})
    project_id = client.get("/api/app-state").json()["projects"][0]["id"]
    client.post(f"/api/projects/{project_id}/open")
    return project_id


def test_start_session_returns_card_with_grilling_questions(client, tmp_path, monkeypatch):
    _open_project(client, tmp_path, "proj")

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": "- What should it do?\n- Who is it for?"},
    )

    resp = client.post("/api/session/start", json={"prompt": "a new feature"})
    data = resp.json()

    assert data["card_id"] is not None
    assert data["session_id"] == "s1"
    assert len(data["sections"]) == 1
    assert len(data["sections"][0]["questions"]) == 2


def test_continue_session_advances_through_prd_and_issues_to_details(client, tmp_path, monkeypatch):
    _open_project(client, tmp_path, "proj")

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": "- First question?"},
    )
    card_id = client.post("/api/session/start", json={"prompt": "a feature"}).json()["card_id"]

    def fake_run_prompt(prompt, **kw):
        if prompt == "/to-prd":
            return {"session_id": "s1", "result": "Published PRD #5: My PRD"}
        if prompt == "/to-issues":
            return {"session_id": "s1", "result": "Issue #6: Child one\nIssue #7: Child two"}
        # the grilling reply itself: no more bullet/heading questions -> grilling is done
        return {"session_id": "s1", "result": "Thanks, that's everything I need."}

    monkeypatch.setattr(app_module, "run_prompt", fake_run_prompt)
    monkeypatch.setattr(app_module, "clear_session", lambda session_id, cwd=None: "s2")

    resp = client.post("/api/session/continue", json={"card_id": card_id, "reply": "all good"})
    data = resp.json()

    assert data["phase"] == "details"
    assert data["details"]["prd"] == {"number": 5, "title": "My PRD"}
    assert data["details"]["issues"] == [
        {"number": 6, "title": "Child one"},
        {"number": 7, "title": "Child two"},
    ]

    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    assert row["claude_session_id"] == "s2"
    assert row["available_for_reuse"] == 1


def test_to_prd_auth_failure_sets_error_and_needs_github_login(client, tmp_path, monkeypatch):
    _open_project(client, tmp_path, "proj")

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": "- Only question?"},
    )
    card_id = client.post("/api/session/start", json={"prompt": "a feature"}).json()["card_id"]

    def failing_run_prompt(prompt, **kw):
        if prompt == "/to-prd":
            raise ClaudeCLIError("gh: not logged in, run `gh auth login`")
        return {"session_id": "s1", "result": "Thanks, that's everything I need."}

    monkeypatch.setattr(app_module, "run_prompt", failing_run_prompt)

    resp = client.post("/api/session/continue", json={"card_id": card_id, "reply": "all good"})
    data = resp.json()

    assert data["phase"] == "creating_prd"
    assert data["needs_github_login"] is True
    assert "not logged in" in data["error"]


def test_retry_after_login_completes_the_failed_phase(client, tmp_path, monkeypatch):
    _open_project(client, tmp_path, "proj")

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": "- Only question?"},
    )
    card_id = client.post("/api/session/start", json={"prompt": "a feature"}).json()["card_id"]

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: (_ for _ in ()).throw(ClaudeCLIError("not logged in"))
        if prompt == "/to-prd"
        else {"session_id": "s1", "result": "done"},
    )
    client.post("/api/session/continue", json={"card_id": card_id, "reply": "all good"})

    def fake_run_prompt(prompt, **kw):
        if prompt == "/to-prd":
            return {"session_id": "s1", "result": "PRD #9: Retried PRD"}
        if prompt == "/to-issues":
            return {"session_id": "s1", "result": "Issue #10: Only child"}
        return {"session_id": "s1", "result": "done"}

    monkeypatch.setattr(app_module, "run_prompt", fake_run_prompt)
    monkeypatch.setattr(app_module, "clear_session", lambda session_id, cwd=None: "s2")

    resp = client.post(f"/api/sessions/{card_id}/retry")
    data = resp.json()

    assert data["phase"] == "details"
    assert data["error"] is None
    assert data["details"]["prd"] == {"number": 9, "title": "Retried PRD"}


def test_session_reuse_pool_is_scoped_per_project(client, tmp_path, monkeypatch):
    project_a = _open_project(client, tmp_path, "proj_a")

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": "- Only question?"},
    )
    card_id = client.post("/api/session/start", json={"prompt": "a feature"}).json()["card_id"]

    monkeypatch.setattr(
        app_module,
        "run_prompt",
        lambda prompt, **kw: {"session_id": "s1", "result": f"PRD #1: p\nIssue #2: i"}
        if prompt in ("/to-prd", "/to-issues")
        else {"session_id": "s1", "result": "done"},
    )
    monkeypatch.setattr(app_module, "clear_session", lambda session_id, cwd=None: "pooled-session")
    client.post("/api/session/continue", json={"card_id": card_id, "reply": "all good"})

    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    assert row["available_for_reuse"] == 1
    assert row["claude_session_id"] == "pooled-session"

    seen_session_ids = []

    def recording_run_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_session_ids.append(session_id)
        return {"session_id": "new", "result": "- Another question?"}

    monkeypatch.setattr(app_module, "run_prompt", recording_run_prompt)

    # Same project: should resume the pooled session.
    client.post("/api/session/start", json={"prompt": "another feature"})
    assert seen_session_ids[-1] == "pooled-session"

    # A different project must never be handed project A's pooled session.
    _open_project(client, tmp_path, "proj_b")
    client.post("/api/session/start", json={"prompt": "unrelated feature"})
    assert seen_session_ids[-1] is None
