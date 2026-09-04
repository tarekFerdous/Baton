import asyncio
import json
import subprocess
from pathlib import Path

from baton import db, live_stream, session_runner
from baton.cli_client import ClaudeCLIError


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


def _cwd_for(project_id):
    conn = db.get_connection()
    return db.get_project(conn, project_id)["path"]


def _result_event(text, session_id="s1"):
    return {"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": session_id}


def test_start_session_job_publishes_usage_from_a_rate_limit_event(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    rate_limit_event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": 12.5},
                "seven_day": {"utilization": 3.1},
            }
        },
    }
    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([rate_limit_event, _result_event("❓ **Q1** - **Scope**: Only question?")]),
    )

    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    events = live_stream._buffers.get(row_id, [])
    assert {"type": "usage", "five_hour_pct": 12.5, "seven_day_pct": 3.1} in events
    assert live_stream.last_usage() == {"type": "usage", "five_hour_pct": 12.5, "seven_day_pct": 3.1}


def test_two_sessions_advance_concurrently_without_cross_contamination(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    row_a = db.create_session(conn, project_id)
    row_b = db.create_session(conn, project_id)

    def fake_stream_prompt(prompt, **kw):
        if prompt == "/do feature A":
            return iter([_result_event("❓ **Q1** - **Scope**: Question A?", session_id="sA")])
        if prompt == "/do feature B":
            return iter([_result_event("❓ **Q1** - **Scope**: Question B?", session_id="sB")])
        raise AssertionError(f"unexpected prompt {prompt!r}")

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)

    async def run_both():
        await asyncio.gather(
            session_runner.start_session_job(row_a, "feature A", cwd=cwd),
            session_runner.start_session_job(row_b, "feature B", cwd=cwd),
        )

    asyncio.run(run_both())

    row_a_data = db.get_session(conn, row_a)
    row_b_data = db.get_session(conn, row_b)
    assert row_a_data["claude_session_id"] == "sA"
    assert row_b_data["claude_session_id"] == "sB"

    interview_a = json.loads(row_a_data["interview_json"])
    interview_b = json.loads(row_b_data["interview_json"])
    assert interview_a["sections"][0]["questions"][0]["text"] == "Question A?"
    assert interview_b["sections"][0]["questions"][0]["text"] == "Question B?"

    events_a = live_stream._buffers.get(row_a, [])
    events_b = live_stream._buffers.get(row_b, [])
    assert any(e["type"] == "turn" and e["interview"] == interview_a for e in events_a)
    assert any(e["type"] == "turn" and e["interview"] == interview_b for e in events_b)
    # Neither session's buffer leaked the other's content.
    assert not any("Question B" in json.dumps(e) for e in events_a)
    assert not any("Question A" in json.dumps(e) for e in events_b)


def test_no_cap_on_the_number_of_sessions_running_at_once(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    row_ids = [db.create_session(conn, project_id) for _ in range(8)]

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event(f"❓ **Q1** - **Scope**: {prompt}?", session_id=prompt)]),
    )

    async def run_all():
        await asyncio.gather(
            *[session_runner.start_session_job(row_id, f"feature {i}", cwd=cwd) for i, row_id in enumerate(row_ids)]
        )

    asyncio.run(run_all())

    for i, row_id in enumerate(row_ids):
        row = db.get_session(conn, row_id)
        assert row["claude_session_id"] == f"/do feature {i}"
        interview = json.loads(row["interview_json"])
        assert interview["sections"][0]["questions"][0]["text"] == f"/do feature {i}?"


def test_start_session_job_returns_card_with_grilling_questions(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter(
            [
                _result_event(
                    "❓ **Q1** - **Behavior**: What should it do?\n"
                    "\n"
                    "---\n"
                    "\n"
                    "❓ **Q2** - **Audience**: Who is it for?"
                )
            ]
        ),
    )

    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)

    asyncio.run(session_runner.start_session_job(row_id, "a new feature", cwd=cwd))

    row = db.get_session(conn, row_id)
    interview = json.loads(row["interview_json"])
    assert len(interview["sections"]) == 1
    assert len(interview["sections"][0]["questions"]) == 2
    assert row["claude_session_id"] == "s1"

    events = live_stream._buffers.get(row_id, [])
    assert {"type": "phase", "phase": "grilling"} in events
    assert any(e["type"] == "turn" and e["interview"] == interview for e in events)


def test_start_session_job_publishes_interview_even_with_no_structured_questions(client, tmp_path, monkeypatch):
    """A real /do turn can reply with plain prose (no bullet/heading
    questions qa_parser recognizes as structured). The left card must still
    render that turn -- it must not look like nothing happened."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("Sure, tell me more about what you have in mind.")]),
    )

    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a new feature", cwd=cwd))

    events = live_stream._buffers.get(row_id, [])
    turn_events = [e for e in events if e["type"] == "turn"]
    assert len(turn_events) == 1
    assert turn_events[0]["interview"]["sections"] == []
    assert turn_events[0]["interview"]["preamble"]


def test_continue_session_job_with_remaining_questions_does_not_auto_advance(client, tmp_path, monkeypatch):
    """A reply that still has follow-up questions must never auto-advance --
    this was already true before #33, but this test locks it in explicitly
    and confirms it needs no `confirm_advance` flag."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: First question?")]),
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def fake_stream_prompt(prompt, **kw):
        if prompt in ("/to-prd", "/to-issues"):
            raise AssertionError(f"chain must not run without confirm_advance, got {prompt!r}")
        return iter([_result_event("❓ **Q1** - **Scope**: A follow-up question?")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)

    asyncio.run(session_runner.continue_session_job(row_id, "all good", cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "grilling"
    interview = json.loads(row["interview_json"])
    assert interview["sections"][0]["questions"][0]["text"] == "A follow-up question?"

    events = live_stream._buffers.get(row_id, [])
    turn_events = [e for e in events if e["type"] == "turn"]
    assert turn_events[-1]["interview"]["sections"]


def test_continue_session_job_with_no_more_questions_does_not_auto_advance(client, tmp_path, monkeypatch):
    """Issue #33: a reply that comes back with zero remaining questions must
    stay in grilling and publish the wrap-up turn -- it must NOT silently
    fire /to-prd on its own anymore."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: First question?")]),
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def fake_stream_prompt(prompt, **kw):
        if prompt in ("/to-prd", "/to-issues"):
            raise AssertionError(f"chain must not run without confirm_advance, got {prompt!r}")
        return iter([_result_event("Thanks, that's everything I need.")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)

    asyncio.run(session_runner.continue_session_job(row_id, "all good", cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "grilling"
    assert row["error_text"] is None

    events = live_stream._buffers.get(row_id, [])
    turn_events = [e for e in events if e["type"] == "turn"]
    # One turn from start_session_job's first question, one from this reply.
    assert len(turn_events) == 2
    assert turn_events[-1]["phase"] == "grilling"
    assert turn_events[-1]["interview"]["sections"] == []
    assert turn_events[-1]["interview"]["preamble"]
    assert not any(e == {"type": "phase", "phase": "creating_prd"} for e in events)


def test_confirm_advance_skips_grilling_turn_and_advances_through_chain(client, tmp_path, monkeypatch):
    """Issue #33: the explicit "Yes, proceed" path (confirm_advance=True)
    must go straight to /to-prd -> /to-issues -> details, resuming the
    session's existing claude_session_id, WITHOUT sending another grilling
    CLI turn first."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: Only question?")]),
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    seen_prompts = []

    def fake_stream_prompt(prompt, **kw):
        seen_prompts.append(prompt)
        if prompt == "/to-prd":
            return iter([_result_event("Published PRD #5: My PRD")])
        if prompt == "/to-issues":
            return iter([_result_event("Issue #6: Child one")])
        raise AssertionError(f"unexpected grilling-style prompt {prompt!r} during confirm_advance")

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    # Exactly the two chain prompts ran -- no grilling reply was ever sent.
    assert seen_prompts == ["/to-prd", "/to-issues"]

    row = db.get_session(conn, row_id)
    assert row["phase"] == "details"
    details = json.loads(row["details_json"])
    assert details["prd"] == {"number": 5, "title": "My PRD"}
    assert details["issues"] == [{"number": 6, "title": "Child one"}]
    assert row["available_for_reuse"] == 1

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}
    assert any(e == {"type": "phase", "phase": "creating_prd"} for e in events)
    assert any(e == {"type": "phase", "phase": "creating_issues"} for e in events)
    assert any(e.get("type") == "turn" and e.get("phase") == "details" for e in events)


def test_start_session_job_passes_the_configured_model_to_the_cli(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_model(conn, "claude-opus-4-8")

    seen_models = []

    def recording_stream_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_models.append(model)
        return iter([_result_event("- Only question?")])

    monkeypatch.setattr(session_runner, "stream_prompt", recording_stream_prompt)

    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    assert seen_models == ["claude-opus-4-8"]
    # The model actually used is persisted onto the row for later turns.
    assert db.get_session(conn, row_id)["model"] == "claude-opus-4-8"


def test_start_implement_job_passes_the_model_the_session_was_launched_with(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_model(conn, "claude-opus-4-8")

    seen_models = []

    def recording_stream_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_models.append(model)
        return iter([_result_event("Implemented PRD #5", session_id="impl1")])

    monkeypatch.setattr(session_runner, "stream_prompt", recording_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-1")

    started = asyncio.run(session_runner.start_or_queue_implement(project_id, 5, "My PRD", cwd))
    card_id = started["card_id"]

    assert seen_models == ["claude-opus-4-8"]
    assert db.get_session(conn, card_id)["model"] == "claude-opus-4-8"


def test_chain_steps_use_the_model_the_session_was_created_with(client, tmp_path, monkeypatch):
    """/to-prd and /to-issues (run via advance_past_grilling) must be
    invoked with the same model the session's grilling turn used, not
    whatever `settings.model` currently is."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_model(conn, "claude-opus-4-8")

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("- Only question?")])
    )
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    seen_models = []

    def recording_stream_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_models.append((prompt, model))
        if prompt == "/to-prd":
            return iter([_result_event("PRD #5: My PRD")])
        if prompt == "/to-issues":
            return iter([_result_event("Issue #6: Child one")])
        return iter([_result_event("Thanks, that's everything I need.")])

    monkeypatch.setattr(session_runner, "stream_prompt", recording_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    asyncio.run(session_runner.continue_session_job(row_id, "all good", cwd=cwd))
    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    assert seen_models == [
        ("all good", "claude-opus-4-8"),
        ("/to-prd", "claude-opus-4-8"),
        ("/to-issues", "claude-opus-4-8"),
    ]


def test_in_flight_session_keeps_its_original_model_after_setting_changes_mid_session(client, tmp_path, monkeypatch):
    """A session already grilling must keep using the model it started with,
    even if `settings.model` is changed before its later turns run."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_model(conn, "claude-opus-4-8")

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("- First question?")])
    )
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    # Setting changes mid-session -- this row must not pick it up.
    db.set_model(conn, "claude-sonnet-4-6")

    seen_models = []

    def recording_stream_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_models.append(model)
        return iter([_result_event("Thanks, that's everything I need.")])

    monkeypatch.setattr(session_runner, "stream_prompt", recording_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    asyncio.run(session_runner.continue_session_job(row_id, "a reply", cwd=cwd))

    assert all(m == "claude-opus-4-8" for m in seen_models)

    # A brand-new session started after the change picks up the new setting.
    new_row_id = db.create_session(conn, project_id)
    seen_models.clear()
    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: (seen_models.append(kw.get("model")), iter([_result_event("- Q?")]))[1],
    )
    asyncio.run(session_runner.start_session_job(new_row_id, "another feature", cwd=cwd))
    assert seen_models == ["claude-sonnet-4-6"]


def test_continue_session_job_advances_through_prd_and_issues_to_details(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: First question?")])
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def fake_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            return iter([_result_event("Published PRD #5: My PRD")])
        if prompt == "/to-issues":
            return iter([_result_event("Issue #6: Child one\nIssue #7: Child two")])
        # the grilling reply itself: no more bullet/heading questions -> grilling is done
        return iter([_result_event("Thanks, that's everything I need.")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    # The grilling reply itself: no more questions -> stays in grilling and
    # publishes the wrap-up turn, no auto-advance.
    asyncio.run(session_runner.continue_session_job(row_id, "all good", cwd=cwd))
    row = db.get_session(conn, row_id)
    assert row["phase"] == "grilling"

    # Explicit "Yes, proceed" is what actually advances the chain.
    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "details"
    details = json.loads(row["details_json"])
    assert details["prd"] == {"number": 5, "title": "My PRD"}
    assert details["issues"] == [
        {"number": 6, "title": "Child one"},
        {"number": 7, "title": "Child two"},
    ]
    assert row["claude_session_id"] == "s2"
    assert row["available_for_reuse"] == 1

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}
    assert any(e.get("type") == "turn" and e.get("phase") == "details" for e in events)
    assert any(e == {"type": "phase", "phase": "creating_prd"} for e in events)
    assert "$" not in json.dumps(events)


def test_to_prd_auth_failure_sets_error_and_needs_github_login(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: Only question?")])
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def failing_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            raise ClaudeCLIError("gh: not logged in, run `gh auth login`")
        return iter([_result_event("Thanks, that's everything I need.")])

    monkeypatch.setattr(session_runner, "stream_prompt", failing_stream_prompt)

    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "creating_prd"
    assert bool(row["needs_github_login"]) is True
    assert "not logged in" in row["error_text"]

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}


def test_retry_after_login_completes_the_failed_phase(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: Only question?")])
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def failing_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            raise ClaudeCLIError("not logged in")
        return iter([_result_event("done")])

    monkeypatch.setattr(session_runner, "stream_prompt", failing_stream_prompt)
    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    def fake_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            return iter([_result_event("PRD #9: Retried PRD")])
        if prompt == "/to-issues":
            return iter([_result_event("Issue #10: Only child")])
        return iter([_result_event("done")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    asyncio.run(session_runner.retry_session_job(row_id, cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "details"
    assert row["error_text"] is None
    details = json.loads(row["details_json"])
    assert details["prd"] == {"number": 9, "title": "Retried PRD"}


def test_retry_on_errored_implement_session_creates_a_new_row_and_completes(client, tmp_path, monkeypatch):
    """Retrying an errored implement session must not resume the original
    row's (possibly dead) claude_session_id -- it must go through
    start_or_queue_implement, the same entry point a fresh PRD-list click
    uses, creating a brand-new session row that then progresses normally."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 12, "title": "Errored PRD"}},
    )
    db.update_session(conn, row_id, error_text="agent crashed mid-turn")

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("Implemented PRD #12", session_id="impl-retry")]),
    )
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-retry")

    response = client.post(f"/api/sessions/{row_id}/retry")
    assert response.status_code == 200

    sessions = db.list_sessions_for_project(conn, project_id)
    implement_sessions = [s for s in sessions if s["session_type"] == "implement"]
    assert len(implement_sessions) == 2

    new_row = next(s for s in implement_sessions if s["id"] != row_id)
    assert new_row["phase"] == "implemented"
    assert json.loads(new_row["details_json"])["prd"] == {"number": 12, "title": "Errored PRD"}

    original_row = db.get_session(conn, row_id)
    assert original_row["phase"] == "implementing"
    assert original_row["error_text"] == "agent crashed mid-turn"


def test_retry_on_errored_implement_session_respects_serial_mode_queueing(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_parallel_implementation(conn, False)

    # An already-live implement session for this project (serial mode: a
    # retry while this is running must queue rather than start immediately).
    db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 20, "title": "Currently Running"}},
    )

    errored_row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 21, "title": "Errored PRD"}},
    )
    db.update_session(conn, errored_row_id, error_text="agent crashed mid-turn")

    async def _noop_job(card_id, prd_number, *, cwd):
        return None

    monkeypatch.setattr(session_runner, "start_implement_job", _noop_job)

    asyncio.run(session_runner.retry_session_job(errored_row_id, cwd))

    sessions = db.list_sessions_for_project(conn, project_id)
    implement_sessions = [s for s in sessions if s["session_type"] == "implement"]
    # No new row was created -- the retry was enqueued instead.
    assert len(implement_sessions) == 2
    assert session_runner._implement_queues.get(project_id) == [{"number": 21, "title": "Errored PRD"}]

    original_row = db.get_session(conn, errored_row_id)
    assert original_row["phase"] == "implementing"
    assert original_row["error_text"] == "agent crashed mid-turn"


def test_retry_on_creating_prd_phase_is_unaffected_by_implement_branch(client, tmp_path, monkeypatch):
    """Guard against regressing the existing creating_prd/creating_issues
    retry path when adding the implement-session branch."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: Only question?")])
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_id)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd))

    def failing_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            raise ClaudeCLIError("not logged in")
        return iter([_result_event("done")])

    monkeypatch.setattr(session_runner, "stream_prompt", failing_stream_prompt)
    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd, confirm_advance=True))

    def fake_stream_prompt(prompt, **kw):
        if prompt == "/to-prd":
            return iter([_result_event("PRD #30: Regression PRD")])
        if prompt == "/to-issues":
            return iter([_result_event("Issue #31: Only child")])
        return iter([_result_event("done")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "s2")

    asyncio.run(session_runner.retry_session_job(row_id, cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "details"
    assert row["error_text"] is None
    details = json.loads(row["details_json"])
    assert details["prd"] == {"number": 30, "title": "Regression PRD"}


def test_start_implement_job_reaches_implemented_and_pools_falling_back_to_seeded_details(client, tmp_path, monkeypatch):
    """No `.claude/implement-tracker.json` written -- the seeded `{"prd": ...}`
    details from creation must survive into the terminal `implemented` row."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    monkeypatch.setattr(
        session_runner,
        "stream_prompt",
        lambda prompt, **kw: iter([_result_event("Implemented PRD #5", session_id="impl1")]),
    )
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-1")

    conn = db.get_connection()
    row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 5, "title": "My PRD"}},
    )

    asyncio.run(session_runner.start_implement_job(row_id, 5, cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "implemented"
    assert row["available_for_reuse"] == 1
    assert row["claude_session_id"] == "pooled-1"
    assert json.loads(row["details_json"]) == {"prd": {"number": 5, "title": "My PRD"}}

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}
    assert {"type": "phase", "phase": "implementing"} in events
    assert any(e.get("type") == "turn" and e.get("phase") == "implemented" for e in events)


def test_start_implement_job_populates_details_from_tracker_file_when_present(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    tracker = {
        "prd": {"number": 7, "title": "Tracked PRD"},
        "issues": [{"number": 8, "title": "Child", "summary": "does the thing", "acceptance_criteria": ["works"]}],
        "qa_changes": [],
        "status": "implemented",
    }
    claude_dir = Path(cwd) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "implement-tracker.json").write_text(json.dumps(tracker), encoding="utf-8")

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("done", session_id="impl2")])
    )
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-2")

    conn = db.get_connection()
    row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 7, "title": "Tracked PRD"}},
    )

    asyncio.run(session_runner.start_implement_job(row_id, 7, cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "implemented"
    assert json.loads(row["details_json"]) == tracker

    events = live_stream._buffers.get(row_id, [])
    assert any(e.get("type") == "turn" and e.get("phase") == "implemented" and e.get("details") == tracker for e in events)


def test_start_implement_job_error_leaves_session_in_implementing_with_error_text(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    def failing_stream_prompt(prompt, **kw):
        raise ClaudeCLIError("gh: not logged in, run `gh auth login`")

    monkeypatch.setattr(session_runner, "stream_prompt", failing_stream_prompt)

    conn = db.get_connection()
    row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 11, "title": "Errorable"}},
    )

    asyncio.run(session_runner.start_implement_job(row_id, 11, cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "implementing"
    assert bool(row["needs_github_login"]) is True
    assert "not logged in" in row["error_text"]

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}


def test_start_implement_job_wraps_unexpected_worker_exceptions_as_session_errors(client, tmp_path, monkeypatch):
    """A non-ClaudeCLIError exception out of the streaming worker (e.g. the
    CLI producing non-JSON output) must still resolve into a recorded
    session error, not an unhandled exception on the fire-and-forget
    asyncio task that would leave the card stuck in `implementing` forever."""
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    def crashing_stream_prompt(prompt, **kw):
        def gen():
            raise ValueError("boom")
            yield  # pragma: no cover

        return gen()

    monkeypatch.setattr(session_runner, "stream_prompt", crashing_stream_prompt)

    conn = db.get_connection()
    row_id = db.create_session(
        conn,
        project_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": 11, "title": "Errorable"}},
    )

    asyncio.run(session_runner.start_implement_job(row_id, 11, cwd=cwd))

    row = db.get_session(conn, row_id)
    assert row["phase"] == "implementing"
    assert "boom" in row["error_text"]

    events = live_stream._buffers.get(row_id, [])
    assert events[-1] == {"type": "done"}


def test_parallel_mode_starts_multiple_prds_immediately_without_queueing(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    async def _noop_job(card_id, prd_number, *, cwd):
        return None

    monkeypatch.setattr(session_runner, "start_implement_job", _noop_job)

    # parallel_implementation defaults to True -- two different PRDs both
    # start immediately, no queueing involved.
    first = asyncio.run(session_runner.start_or_queue_implement(project_id, 5, "PRD Five", cwd))
    assert "card_id" in first

    second = asyncio.run(session_runner.start_or_queue_implement(project_id, 6, "PRD Six", cwd))
    assert "card_id" in second

    conn = db.get_connection()
    sessions = db.list_sessions_for_project(conn, project_id)
    implement_sessions = [s for s in sessions if s["session_type"] == "implement"]
    assert len(implement_sessions) == 2
    assert session_runner._implement_queues.get(project_id, []) == []


def test_serial_mode_queues_second_prd_and_drains_it_when_first_finishes(client, tmp_path, monkeypatch):
    project_id = _open_project(client, tmp_path, "proj")
    cwd = _cwd_for(project_id)

    conn = db.get_connection()
    db.set_parallel_implementation(conn, False)

    # First PRD starts immediately (nothing else is running yet).
    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("Implemented PRD #5", session_id="impl-a")])
    )
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-a")

    started = asyncio.run(session_runner.start_or_queue_implement(project_id, 5, "PRD Five", cwd))
    row_a = started["card_id"]
    assert row_a is not None

    # A's session row is still "implementing" (its background job hasn't run
    # yet -- it was only scheduled via asyncio.create_task).
    assert db.get_session(conn, row_a)["phase"] == "implementing"

    # Second PRD, while A is live, must queue instead of starting.
    queued = asyncio.run(session_runner.start_or_queue_implement(project_id, 6, "PRD Six", cwd))
    assert queued == {"queued": True}

    sessions_before_drain = db.list_sessions_for_project(conn, project_id)
    implement_sessions_before = [s for s in sessions_before_drain if s["session_type"] == "implement"]
    assert len(implement_sessions_before) == 1

    # Now run A's job for real (as its scheduled asyncio.create_task would),
    # and let any follow-on drain task it schedules run to completion too.
    def fake_stream_prompt(prompt, **kw):
        if prompt == "/implement prd: 6":
            return iter([_result_event("Implemented PRD #6", session_id="impl-b")])
        return iter([_result_event("Implemented PRD #5", session_id="impl-a")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: f"pooled-{session_id}")

    async def run_a_and_drain():
        await session_runner.start_implement_job(row_a, 5, cwd=cwd)
        # start_implement_job's own completion schedules the queue drain's
        # follow-on job via asyncio.create_task -- let it run to completion
        # before asserting on it, matching how this module fires background
        # work across a job boundary.
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if pending:
            await asyncio.gather(*pending)

    asyncio.run(run_a_and_drain())

    assert session_runner._implement_queues.get(project_id, []) == []

    sessions_after_drain = db.list_sessions_for_project(conn, project_id)
    implement_sessions_after = [s for s in sessions_after_drain if s["session_type"] == "implement"]
    assert len(implement_sessions_after) == 2

    row_b = next(s for s in implement_sessions_after if s["id"] != row_a)
    assert row_b["phase"] == "implemented"
    assert json.loads(row_b["details_json"])["prd"] == {"number": 6, "title": "PRD Six"}


def test_session_reuse_pool_is_scoped_per_project(client, tmp_path, monkeypatch):
    project_a = _open_project(client, tmp_path, "proj_a")
    cwd_a = _cwd_for(project_a)

    monkeypatch.setattr(
        session_runner, "stream_prompt", lambda prompt, **kw: iter([_result_event("❓ **Q1** - **Scope**: Only question?")])
    )
    conn = db.get_connection()
    row_id = db.create_session(conn, project_a)
    asyncio.run(session_runner.start_session_job(row_id, "a feature", cwd=cwd_a))

    def fake_stream_prompt(prompt, **kw):
        if prompt in ("/to-prd", "/to-issues"):
            return iter([_result_event("PRD #1: p\nIssue #2: i")])
        return iter([_result_event("done")])

    monkeypatch.setattr(session_runner, "stream_prompt", fake_stream_prompt)
    monkeypatch.setattr(session_runner, "clear_session", lambda session_id, cwd=None, model=None: "pooled-session")
    asyncio.run(session_runner.continue_session_job(row_id, "", cwd=cwd_a, confirm_advance=True))

    row = db.get_session(conn, row_id)
    assert row["available_for_reuse"] == 1
    assert row["claude_session_id"] == "pooled-session"

    seen_session_ids = []

    def recording_stream_prompt(prompt, *, session_id=None, cwd=None, model=None):
        seen_session_ids.append(session_id)
        return iter([_result_event("❓ **Q1** - **Scope**: Another question?", session_id="new")])

    monkeypatch.setattr(session_runner, "stream_prompt", recording_stream_prompt)

    # Same project: should resume the pooled session.
    reused = db.claim_available_session(conn, project_a)
    resume_id = reused["claude_session_id"] if reused is not None else None
    new_row_id = db.create_session(conn, project_a, claude_session_id=resume_id)
    asyncio.run(session_runner.start_session_job(new_row_id, "another feature", cwd=cwd_a))
    assert seen_session_ids[-1] == "pooled-session"

    # A different project must never be handed project A's pooled session.
    project_b = _open_project(client, tmp_path, "proj_b")
    cwd_b = _cwd_for(project_b)
    reused_b = db.claim_available_session(conn, project_b)
    resume_id_b = reused_b["claude_session_id"] if reused_b is not None else None
    row_id_b = db.create_session(conn, project_b, claude_session_id=resume_id_b)
    asyncio.run(session_runner.start_session_job(row_id_b, "unrelated feature", cwd=cwd_b))
    assert seen_session_ids[-1] is None
