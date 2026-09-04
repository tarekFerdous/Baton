"""Per-session background jobs: run CLI turns off the event loop thread and
stream their translated output live via `baton.live_stream`.

Each job publishes app-level events for a session's card_id: `phase` at the
start of each step, `text`/`action`/`usage` as a turn streams, a richer
`turn` event once a turn's semantics (interview/details/error) are known,
and `done` once the session has reached a terminal state (details or an
unrecoverable error).
"""

import asyncio
import json
import re
from pathlib import Path

_QA_GRILLING_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```")

from baton import db
from baton.cli_client import ClaudeCLIError, clear_session, stream_prompt
from baton.live_stream import publish
from baton.qa_parser import parse_grilling_response
from baton.stream_translate import translate_event

_AUTH_FAILURE_RE = re.compile(r"auth|login|not logged in|permission denied|401|403", re.IGNORECASE)
_DETAIL_RE = re.compile(r"\b(PRD|Issue)\s*#(\d+)\s*[:\-]\s*(.+)", re.IGNORECASE)

# In-memory, per-project FIFO queue for serial-mode ("parallel_implementation"
# off) PRD implementation requests. Process-lifetime only, same as
# `_active_project_id` in app.py -- nothing here needs to survive a restart.
_implement_queues: dict[int, list[dict]] = {}


def _enqueue_implement(project_id: int, number: int, title: str) -> None:
    _implement_queues.setdefault(project_id, []).append({"number": number, "title": title})


def _pop_next_implement(project_id: int) -> dict | None:
    queue = _implement_queues.get(project_id)
    if not queue:
        return None
    return queue.pop(0)


def parse_details(text: str) -> dict:
    prd = None
    issues = []
    for match in _DETAIL_RE.finditer(text):
        kind, number, title = match.group(1).lower(), int(match.group(2)), match.group(3).strip()
        if kind == "prd" and prd is None:
            prd = {"number": number, "title": title}
        elif kind == "issue":
            issues.append({"number": number, "title": title})
    return {"prd": prd, "issues": issues, "raw": text}


async def _run_turn(
    card_id: int, prompt: str, *, session_id: str | None, cwd: str | None, model: str | None = None
) -> dict:
    """Run one CLI turn in a background thread, streaming translated events
    into the session's live buffer as they arrive. Returns the raw `result`
    event's translated boundary marker ({"result", "session_id", "is_error"})
    once the turn finishes; raises ClaudeCLIError on failure -- the caller
    decides what that means for the session (grilling vs. chain phase).
    """
    holder: dict = {}

    def worker():
        for raw_event in stream_prompt(prompt, session_id=session_id, cwd=cwd, model=model):
            translated = translate_event(raw_event)
            if translated is None:
                continue
            if translated["type"] == "turn":
                holder["turn"] = translated
                continue
            publish(card_id, translated)

    try:
        await asyncio.to_thread(worker)
    except ClaudeCLIError as e:
        holder["error"] = e
    except Exception as e:
        # Anything unexpected (a malformed CLI event, a bug in translation)
        # must still resolve into a recorded session error, not an
        # unhandled exception on the fire-and-forget asyncio task -- that
        # would leave the card stuck in its in-flight phase silently
        # instead of surfacing the failure to the user.
        holder["error"] = ClaudeCLIError(str(e))

    if "error" in holder:
        raise holder["error"]
    return holder["turn"]


def _turn_event(*, phase: str, interview=None, details=None, error=None, needs_github_login=False) -> dict:
    return {
        "type": "turn",
        "phase": phase,
        "interview": interview,
        "details": details,
        "error": error,
        "needs_github_login": needs_github_login,
    }


async def _run_grilling_turn(
    card_id: int, conn, row, prompt: str, *, cwd: str | None, model: str | None, publish_when_empty: bool = False
) -> dict | None:
    """Run one grilling-phase turn and publish its `turn` event. Returns the
    parsed interview dict if grilling is still ongoing, `None` if this turn
    finished grilling (no more questions) or failed.

    `model` is the model this *session* (not just this turn) was created
    with -- see `start_session_job`/`continue_session_job` for where it comes
    from. It's persisted back onto the row alongside the other per-turn
    fields so a later call (a follow-up reply, a retry) can keep reading it
    off the row instead of re-checking the current setting.

    `publish_when_empty` covers `start_session_job`: a brand-new session's
    very first turn must always render (even a bare preamble with no
    structured questions), matching the old behavior of always surfacing
    `parse_grilling_response`'s result on start. `continue_session_job` now
    always passes `True` too -- zero sections there means grilling is done
    and the turn's preamble is the assistant's wrap-up message, which the
    frontend renders as a "ready to proceed?" gate rather than the chain
    auto-advancing on its own."""
    publish(card_id, {"type": "phase", "phase": "grilling"})

    try:
        turn = await _run_turn(card_id, prompt, session_id=row["claude_session_id"], cwd=cwd, model=model)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, card_id, model=model, error_text=message, needs_github_login=needs_login)
        publish(card_id, _turn_event(phase="grilling", error=message, needs_github_login=bool(needs_login)))
        return None

    parsed = parse_grilling_response(turn["result"])
    console_text = row["console_text"] + "\n\n" + turn["result"] if row["console_text"] else turn["result"]
    db.update_session(
        conn,
        card_id,
        model=model,
        claude_session_id=turn["session_id"],
        console_text=console_text,
        interview_json=json.dumps(parsed),
    )

    if parsed["sections"] or publish_when_empty:
        publish(card_id, _turn_event(phase="grilling", interview=parsed))

    return parsed if parsed["sections"] else None


async def _run_chain_step(
    card_id: int, conn, row, *, phase: str, prompt: str, cwd: str | None, model: str | None
) -> tuple[bool, str | None]:
    """Run one /to-prd or /to-issues step, live-streamed. Returns (ok, claude_session_id).

    On failure, publishes the error `turn` event and `done` itself -- the
    chain stops here exactly as the old blocking version did.
    """
    db.update_session(conn, row["id"], phase=phase, error_text=None, needs_github_login=0)
    publish(card_id, {"type": "phase", "phase": phase})

    try:
        turn = await _run_turn(card_id, prompt, session_id=row["claude_session_id"], cwd=cwd, model=model)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, row["id"], error_text=message, needs_github_login=needs_login)
        publish(card_id, _turn_event(phase=phase, error=message, needs_github_login=bool(needs_login)))
        publish(card_id, {"type": "done"})
        return False, None

    db.update_session(
        conn,
        row["id"],
        claude_session_id=turn["session_id"],
        console_text=row["console_text"] + "\n\n" + turn["result"],
    )
    return True, turn["session_id"]


async def _finish_chain(card_id: int, conn, claude_session_id: str, cwd: str | None) -> None:
    row = db.get_session(conn, card_id)
    details = parse_details(row["console_text"])
    db.update_session(conn, card_id, phase="details", details_json=json.dumps(details))

    new_session_id = await asyncio.to_thread(clear_session, claude_session_id, cwd=cwd, model=row["model"])
    db.mark_session_available(conn, card_id, new_session_id)

    publish(card_id, _turn_event(phase="details", details=details))
    publish(card_id, {"type": "done"})


async def advance_past_grilling(card_id: int, cwd: str | None) -> None:
    """Grilling just finished: run /to-prd, /to-issues (live-streamed), then
    auto-/clear and pool the session. Uses the model already recorded on the
    row (set back when the session started grilling) -- this is a
    continuation of that same session, not a fresh one, so the configured
    model is not re-read here even if it's changed since."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    model = row["model"]
    ok, _ = await _run_chain_step(card_id, conn, row, phase="creating_prd", prompt="/to-prd", cwd=cwd, model=model)
    if not ok:
        return

    row = db.get_session(conn, card_id)
    ok, claude_session_id = await _run_chain_step(
        card_id, conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd, model=model
    )
    if not ok:
        return

    await _finish_chain(card_id, conn, claude_session_id, cwd)


async def start_session_job(card_id: int, prompt: str, *, cwd: str | None) -> None:
    """A brand-new grilling session begins here -- read the currently
    configured model once, now, and use it for this session's entire
    lifetime (later turns read it back off the row instead of re-checking
    the setting)."""
    conn = db.get_connection()
    model = db.get_model(conn)
    row = db.get_session(conn, card_id)
    await _run_grilling_turn(card_id, conn, row, f"/do {prompt}", cwd=cwd, model=model, publish_when_empty=True)


async def continue_session_job(card_id: int, reply: str, *, cwd: str | None, confirm_advance: bool = False) -> None:
    """A grilling reply, or an explicit "yes, proceed" confirmation.

    Normal replies (`confirm_advance=False`, the default) always run one
    more grilling CLI turn and publish its `turn` event -- whether or not
    `sections` comes back empty -- and then stop; there is no auto-advance
    into the PRD/issues chain anymore. When grilling has no more questions,
    the frontend shows the turn's `preamble` with "Yes, proceed" / "No, keep
    discussing" buttons, and "Yes" is what re-invokes this function with
    `confirm_advance=True`.

    `confirm_advance=True` skips running a grilling CLI turn entirely and
    goes straight to `advance_past_grilling`, resuming the session's existing
    `claude_session_id` -- exactly like `retry_session_job` does for a
    `creating_prd` row. Both paths use the model already recorded on the row
    -- this is a continuation of an existing session, not a fresh one."""
    if confirm_advance:
        await advance_past_grilling(card_id, cwd)
        return

    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    await _run_grilling_turn(card_id, conn, row, reply, cwd=cwd, model=row["model"], publish_when_empty=True)


def _parse_qa_grilling_block(text: str) -> dict | None:
    """Extract the first JSON code block with phase=='qa_grilling' from a
    CLI turn result, as emitted by the /qa skill Phase 2. Returns None when
    no such block is found (normal /implement run without the /qa auto-handoff)."""
    for match in _QA_GRILLING_BLOCK_RE.finditer(text):
        try:
            data = json.loads(match.group(1))
            if isinstance(data, dict) and data.get("phase") == "qa_grilling":
                return data
        except (json.JSONDecodeError, ValueError):
            continue
    # Fallback: bare JSON (no code fence)
    try:
        data = json.loads(text.strip())
        if isinstance(data, dict) and data.get("phase") == "qa_grilling":
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _read_tracker_file(cwd: str | None) -> dict | None:
    """Read `.claude/implement-tracker.json` from the project's working
    directory, written by the `/implement` skill's Phase 4. Returns `None`
    if the file is missing or isn't valid JSON -- a run that errored before
    Phase 4, or that finished but never wrote it, shouldn't crash the job."""
    if not cwd:
        return None
    path = Path(cwd) / ".claude" / "implement-tracker.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _launch_implement(conn, project_id: int, number: int, title: str, cwd: str | None) -> int:
    """Claim a pooled session (if any), create the session row, and fire the
    background `/implement` job -- the shared plumbing behind both an
    immediate PRD click and a queued PRD's turn coming up in serial mode.

    A fresh implement session begins here -- read the currently configured
    model once, now, and record it on the new row so `start_implement_job`
    (and any later retry of *this* row) uses it for the row's lifetime."""
    reused = db.claim_available_session(conn, project_id)
    resume_id = reused["claude_session_id"] if reused is not None else None
    model = db.get_model(conn)

    row_id = db.create_session(
        conn,
        project_id,
        claude_session_id=resume_id,
        session_type="implement",
        phase="implementing",
        details={"prd": {"number": number, "title": title}},
        model=model,
    )

    asyncio.create_task(start_implement_job(row_id, number, cwd=cwd))

    return row_id


async def start_or_queue_implement(
    project_id: int, number: int, title: str, cwd: str | None, *, allow_queue: bool = True
) -> dict:
    """Decide whether a clicked PRD starts implementing right away or, in
    serial mode with another implement session already live for this
    project, gets enqueued to auto-start once that slot frees up.

    `allow_queue=False` (used by `afk_loop.check_once`) skips queuing
    entirely when another implement session is already active: the AFK
    opportunity is dropped outright rather than deferred, so a background
    timer decision never chains through a backlog the instant a slot frees
    up. A manually-clicked PRD (the default, `allow_queue=True`) is
    unaffected -- it still queues and drains as soon as the running session
    finishes."""
    conn = db.get_connection()
    if db.has_active_implement_session(conn, project_id, number):
        return {"error": "Already implementing"}

    if not db.get_parallel_implementation(conn) and db.has_any_active_implement_session(conn, project_id):
        if not allow_queue:
            return {"skipped": True}
        _enqueue_implement(project_id, number, title)
        return {"queued": True}

    card_id = _launch_implement(conn, project_id, number, title, cwd)
    return {"card_id": card_id}


async def _drain_implement_queue(project_id: int, cwd: str | None) -> None:
    """Called right as a running implement session frees its "one running at
    a time" slot (serial mode). Starts at most one queued PRD -- that job's
    own completion will drain the one after it, in turn."""
    entry = _pop_next_implement(project_id)
    if entry is None:
        return

    conn = db.get_connection()
    _launch_implement(conn, project_id, entry["number"], entry["title"], cwd)


async def start_implement_job(card_id: int, prd_number: int, *, cwd: str | None) -> None:
    """Run a single `/implement prd: N` turn end to end: `implementing` while
    it's in flight, then `implemented` on success with details replaced by
    the tracker file's contents (falling back to the seeded PRD stub if the
    tracker file is missing), then pool the session exactly like the /do
    chain does."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    model = row["model"]
    publish(card_id, {"type": "phase", "phase": "implementing"})

    try:
        turn = await _run_turn(
            card_id, f"/implement prd: {prd_number}", session_id=row["claude_session_id"], cwd=cwd, model=model
        )
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, card_id, error_text=message, needs_github_login=needs_login)
        publish(card_id, _turn_event(phase="implementing", error=message, needs_github_login=bool(needs_login)))
        publish(card_id, {"type": "done"})
        await _drain_implement_queue(row["project_id"], cwd)
        return

    console_text = row["console_text"] + "\n\n" + turn["result"] if row["console_text"] else turn["result"]
    db.update_session(conn, card_id, console_text=console_text)

    tracker = _read_tracker_file(cwd)
    if tracker is not None:
        details = tracker
    else:
        details = json.loads(row["details_json"]) if row["details_json"] else None

    db.update_session(
        conn,
        card_id,
        phase="implemented",
        details_json=json.dumps(details) if details is not None else None,
    )

    qa_data = _parse_qa_grilling_block(turn["result"])
    if qa_data is not None:
        # /implement Phase 5 ran /qa and emitted the checklist block.
        # Hand the session_id to the QA session instead of pooling it here.
        qa_row_id = db.create_session(
            conn,
            row["project_id"],
            claude_session_id=turn["session_id"],
            session_type="qa",
            phase="qa_grilling",
            details={"prd": qa_data.get("prd")},
            model=model,
        )
        publish(card_id, {"type": "qa_started", "qa_card_id": qa_row_id})
        publish(card_id, _turn_event(phase="implemented", details=details))
        publish(card_id, {"type": "done"})
        await _drain_implement_queue(row["project_id"], cwd)
        asyncio.create_task(start_qa_job(qa_row_id, qa_data, cwd=cwd))
    else:
        new_session_id = await asyncio.to_thread(clear_session, turn["session_id"], cwd=cwd, model=model)
        db.mark_session_available(conn, card_id, new_session_id)
        publish(card_id, _turn_event(phase="implemented", details=details))
        publish(card_id, {"type": "done"})
        await _drain_implement_queue(row["project_id"], cwd)


async def start_qa_job(card_id: int, qa_data: dict, *, cwd: str | None) -> None:
    """Publish the qa_grilling turn event for a QA session created by the
    /implement auto-handoff. The JSON block was already parsed from the
    implement turn's result; emit it and leave the session suspended (no
    'done') until POST /api/session/qa-complete is called."""
    publish(card_id, {"type": "phase", "phase": "qa_grilling"})
    publish(card_id, {
        "type": "turn",
        "phase": "qa_grilling",
        "prd": qa_data.get("prd"),
        "checklist": qa_data.get("checklist", []),
        "interview": None,
        "details": None,
        "error": None,
        "needs_github_login": False,
    })


async def continue_qa_job(card_id: int, notes: str, *, cwd: str | None) -> None:
    """Called from POST /api/session/qa-complete. Unblocks the QA session
    by running Phase 3+ with the user's notes forwarded as context."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    model = row["model"]

    db.update_session(conn, card_id, phase="qa_closing", error_text=None)
    publish(card_id, {"type": "phase", "phase": "qa_closing"})

    note_ctx = f"\nUser notes: {notes}" if notes.strip() else ""
    prompt = (
        f"The user reviewed the implementation and clicked Perfect.{note_ctx}\n\n"
        "Please continue from Phase 3: close all child issues, close the parent PRD, "
        "clear the tracker, commit, push, and run the final /clear."
    )

    try:
        turn = await _run_turn(card_id, prompt, session_id=row["claude_session_id"], cwd=cwd, model=model)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, card_id, error_text=message, needs_github_login=needs_login)
        publish(card_id, _turn_event(phase="qa_closing", error=message, needs_github_login=bool(needs_login)))
        publish(card_id, {"type": "done"})
        return

    db.update_session(conn, card_id, claude_session_id=turn["session_id"])
    publish(card_id, _turn_event(phase="qa_closing"))
    publish(card_id, {"type": "done"})


async def retry_session_job(card_id: int, cwd: str | None) -> None:
    """Resume a session left in `creating_prd` or `creating_issues` -- either
    because that phase errored (e.g. GitHub auth) or because the app process
    restarted mid-phase. Resumes from that phase and completes the rest live,
    matching what a fresh run through the chain would have done.

    An errored `implement` session is handled differently: its own
    `claude_session_id` may belong to a CLI turn that died mid-flight, so
    resuming it isn't safe. Instead this reads the PRD it was implementing
    out of `details_json` and hands off to `start_or_queue_implement`, the
    same entry point a fresh PRD-list click uses -- a brand-new session row
    is created (and queued instead of started immediately if serial mode is
    on and another implement session is already live), while the original
    errored row is left exactly as it is, kept around as history."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)

    if row["session_type"] == "implement":
        details = json.loads(row["details_json"]) if row["details_json"] else None
        prd = details.get("prd") if details else None
        if prd is None:
            return
        await start_or_queue_implement(row["project_id"], prd["number"], prd.get("title", ""), cwd)
        return

    if row["phase"] == "creating_prd":
        await advance_past_grilling(card_id, cwd)
    elif row["phase"] == "creating_issues":
        ok, claude_session_id = await _run_chain_step(
            card_id, conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd, model=row["model"]
        )
        if ok:
            await _finish_chain(card_id, conn, claude_session_id, cwd)
