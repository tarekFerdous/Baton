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

from baton import db
from baton.cli_client import ClaudeCLIError, clear_session, stream_prompt
from baton.live_stream import publish
from baton.qa_parser import parse_grilling_response
from baton.stream_translate import translate_event

_AUTH_FAILURE_RE = re.compile(r"auth|login|not logged in|permission denied|401|403", re.IGNORECASE)
_DETAIL_RE = re.compile(r"\b(PRD|Issue)\s*#(\d+)\s*[:\-]\s*(.+)", re.IGNORECASE)


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


async def _run_turn(card_id: int, prompt: str, *, session_id: str | None, cwd: str | None) -> dict:
    """Run one CLI turn in a background thread, streaming translated events
    into the session's live buffer as they arrive. Returns the raw `result`
    event's translated boundary marker ({"result", "session_id", "is_error"})
    once the turn finishes; raises ClaudeCLIError on failure -- the caller
    decides what that means for the session (grilling vs. chain phase).
    """
    holder: dict = {}

    def worker():
        for raw_event in stream_prompt(prompt, session_id=session_id, cwd=cwd):
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
    card_id: int, conn, row, prompt: str, *, cwd: str | None, publish_when_empty: bool = False
) -> dict | None:
    """Run one grilling-phase turn and publish its `turn` event. Returns the
    parsed interview dict if grilling is still ongoing, `None` if this turn
    finished grilling (no more questions) or failed.

    `publish_when_empty` covers `start_session_job`: a brand-new session's
    very first turn must always render (even a bare preamble with no
    structured questions), matching the old behavior of always surfacing
    `parse_grilling_response`'s result on start. `continue_session_job`
    leaves it False -- zero sections there means grilling just finished and
    hands off to the chain instead of rendering an intermediate turn."""
    publish(card_id, {"type": "phase", "phase": "grilling"})

    try:
        turn = await _run_turn(card_id, prompt, session_id=row["claude_session_id"], cwd=cwd)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, card_id, error_text=message, needs_github_login=needs_login)
        publish(card_id, _turn_event(phase="grilling", error=message, needs_github_login=bool(needs_login)))
        return None

    parsed = parse_grilling_response(turn["result"])
    console_text = row["console_text"] + "\n\n" + turn["result"] if row["console_text"] else turn["result"]
    db.update_session(
        conn,
        card_id,
        claude_session_id=turn["session_id"],
        console_text=console_text,
        interview_json=json.dumps(parsed),
    )

    if parsed["sections"] or publish_when_empty:
        publish(card_id, _turn_event(phase="grilling", interview=parsed))

    return parsed if parsed["sections"] else None


async def _run_chain_step(card_id: int, conn, row, *, phase: str, prompt: str, cwd: str | None) -> tuple[bool, str | None]:
    """Run one /to-prd or /to-issues step, live-streamed. Returns (ok, claude_session_id).

    On failure, publishes the error `turn` event and `done` itself -- the
    chain stops here exactly as the old blocking version did.
    """
    db.update_session(conn, row["id"], phase=phase, error_text=None, needs_github_login=0)
    publish(card_id, {"type": "phase", "phase": phase})

    try:
        turn = await _run_turn(card_id, prompt, session_id=row["claude_session_id"], cwd=cwd)
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

    new_session_id = await asyncio.to_thread(clear_session, claude_session_id, cwd=cwd)
    db.mark_session_available(conn, card_id, new_session_id)

    publish(card_id, _turn_event(phase="details", details=details))
    publish(card_id, {"type": "done"})


async def advance_past_grilling(card_id: int, cwd: str | None) -> None:
    """Grilling just finished: run /to-prd, /to-issues (live-streamed), then
    auto-/clear and pool the session."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    ok, _ = await _run_chain_step(card_id, conn, row, phase="creating_prd", prompt="/to-prd", cwd=cwd)
    if not ok:
        return

    row = db.get_session(conn, card_id)
    ok, claude_session_id = await _run_chain_step(
        card_id, conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd
    )
    if not ok:
        return

    await _finish_chain(card_id, conn, claude_session_id, cwd)


async def start_session_job(card_id: int, prompt: str, *, cwd: str | None) -> None:
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    await _run_grilling_turn(card_id, conn, row, f"/do {prompt}", cwd=cwd, publish_when_empty=True)


async def continue_session_job(card_id: int, reply: str, *, cwd: str | None) -> None:
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    still_grilling = await _run_grilling_turn(card_id, conn, row, reply, cwd=cwd)
    if still_grilling is not None:
        return

    row = db.get_session(conn, card_id)
    if row["error_text"]:
        return  # grilling turn itself failed; nothing to hand off

    await advance_past_grilling(card_id, cwd)


async def retry_session_job(card_id: int, cwd: str | None) -> None:
    """Resume a session left in `creating_prd` or `creating_issues` -- either
    because that phase errored (e.g. GitHub auth) or because the app process
    restarted mid-phase. Resumes from that phase and completes the rest live,
    matching what a fresh run through the chain would have done."""
    conn = db.get_connection()
    row = db.get_session(conn, card_id)

    if row["phase"] == "creating_prd":
        await advance_past_grilling(card_id, cwd)
    elif row["phase"] == "creating_issues":
        ok, claude_session_id = await _run_chain_step(
            card_id, conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd
        )
        if ok:
            await _finish_chain(card_id, conn, claude_session_id, cwd)
