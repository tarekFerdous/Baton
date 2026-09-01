import subprocess


def _init_repo(path, remote_url):
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "checkout", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", remote_url], cwd=path, check=True)


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
