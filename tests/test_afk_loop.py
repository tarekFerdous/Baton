import asyncio
import time

from baton import afk_loop, db, session_runner

from tests.test_sessions import _cwd_for, _open_project


def _stub_prds(*entries):
    """`entries` are (number, title, blocked) tuples -- build the shape
    `compute_prd_list` returns."""
    prds = [{"number": n, "title": t, "blocked": b} for n, t, b in entries]
    return lambda cwd: prds


def test_fires_after_threshold_with_an_unblocked_prd(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_afk_hours(conn, 1)
    afk_loop._last_activity[project_id] = time.monotonic() - 3700  # just over 1 hour ago

    calls = []

    async def fake_start_or_queue_implement(pid, number, title, job_cwd):
        calls.append((pid, number, title, job_cwd))
        return {"card_id": 1}

    monkeypatch.setattr(session_runner, "start_or_queue_implement", fake_start_or_queue_implement)

    asyncio.run(
        afk_loop.check_once(project_id, cwd, _stub_prds((5, "Top PRD", False), (6, "Other PRD", True)))
    )

    assert calls == [(project_id, 5, "Top PRD", cwd)]


def test_does_not_fire_with_zero_unblocked_prds(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_afk_hours(conn, 1)
    afk_loop._last_activity[project_id] = time.monotonic() - 3700

    calls = []

    async def fake_start_or_queue_implement(pid, number, title, job_cwd):
        calls.append((pid, number, title, job_cwd))
        return {"card_id": 1}

    monkeypatch.setattr(session_runner, "start_or_queue_implement", fake_start_or_queue_implement)

    # Only blocked entries -- must not fire regardless of elapsed time.
    asyncio.run(afk_loop.check_once(project_id, cwd, _stub_prds((6, "Other PRD", True))))
    assert calls == []

    # Empty list -- same result.
    asyncio.run(afk_loop.check_once(project_id, cwd, lambda cwd: []))
    assert calls == []


def test_manual_click_resets_the_clock(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_afk_hours(conn, 1)
    afk_loop._last_activity[project_id] = time.monotonic() - 3700  # past threshold

    calls = []

    async def fake_start_or_queue_implement(pid, number, title, job_cwd):
        calls.append((pid, number, title, job_cwd))
        return {"card_id": 1}

    monkeypatch.setattr(session_runner, "start_or_queue_implement", fake_start_or_queue_implement)

    # Simulate the manual-click endpoint's reset.
    afk_loop.record_activity(project_id)

    asyncio.run(afk_loop.check_once(project_id, cwd, _stub_prds((5, "Top PRD", False))))

    assert calls == []


def test_self_implement_firing_resets_the_clock(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_afk_hours(conn, 1)
    afk_loop._last_activity[project_id] = time.monotonic() - 3700

    calls = []

    async def fake_start_or_queue_implement(pid, number, title, job_cwd):
        calls.append((pid, number, title, job_cwd))
        return {"card_id": 1}

    monkeypatch.setattr(session_runner, "start_or_queue_implement", fake_start_or_queue_implement)

    prd_list = _stub_prds((5, "Top PRD", False))

    asyncio.run(afk_loop.check_once(project_id, cwd, prd_list))
    assert len(calls) == 1

    # Immediately re-check with no time advance -- the internal reset from
    # the first fire must prevent a second fire.
    asyncio.run(afk_loop.check_once(project_id, cwd, prd_list))
    assert len(calls) == 1


def test_switching_projects_scopes_the_clock(client, tmp_path, monkeypatch):
    project_a = _open_project(client, tmp_path, "proj-a")
    cwd_a = _cwd_for(project_a)

    conn = db.get_connection()
    db.set_afk_hours(conn, 1)
    afk_loop._last_activity[project_a] = time.monotonic() - 3700  # well past threshold

    calls = []

    async def fake_start_or_queue_implement(pid, number, title, job_cwd):
        calls.append((pid, number, title, job_cwd))
        return {"card_id": 1}

    monkeypatch.setattr(session_runner, "start_or_queue_implement", fake_start_or_queue_implement)

    project_b = project_a + 1  # never-before-seen project id
    assert project_b not in afk_loop._last_activity

    prd_list = _stub_prds((5, "Top PRD", False))

    before = time.monotonic()
    asyncio.run(afk_loop.check_once(project_b, cwd_a, prd_list))
    after = time.monotonic()

    # B gets a fresh clock starting now, so it must not fire yet.
    assert calls == []
    assert before <= afk_loop._last_activity[project_b] <= after

    # A's clock is untouched and still eligible.
    asyncio.run(afk_loop.check_once(project_a, cwd_a, prd_list))
    assert calls == [(project_a, 5, "Top PRD", cwd_a)]
