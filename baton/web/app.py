import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from baton import db
from baton.cli_client import ClaudeCLIError, clear_session, get_auth_status, run_prompt
from baton.folder_picker import pick_folder
from baton.projects import scan_projects
from baton.qa_parser import parse_grilling_response
from baton.terminal import open_terminal_running

_AUTH_FAILURE_RE = re.compile(r"auth|login|not logged in|permission denied|401|403", re.IGNORECASE)
_DETAIL_RE = re.compile(r"\b(PRD|Issue)\s*#(\d+)\s*[:\-]\s*(.+)", re.IGNORECASE)

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def _lifespan(app: FastAPI):
    yield
    db.cleanup_sessions_on_shutdown(db.get_connection())


app = FastAPI(lifespan=_lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# Active project is process-global: Baton is a local, single-user desktop-
# oriented app (one browser tab talking to one backend process), not a
# multi-tenant server, so there's exactly one "current project" at a time.
_active_project_id: int | None = None


def _project_to_dict(row) -> dict:
    return {
        "id": row["id"],
        "path": row["path"],
        "name": row["name"],
        "branch": row["branch"],
        "last_opened": row["last_opened"],
    }


def _rescan_and_cache(conn, root_dir: str) -> None:
    for found in scan_projects(root_dir):
        db.upsert_project(conn, found["path"], found["name"], found["branch"])


@app.get("/")
def index(request: Request):
    try:
        status = get_auth_status()
    except ClaudeCLIError:
        status = {"loggedIn": False}

    if status.get("loggedIn"):
        return RedirectResponse(url="/prompt")

    return templates.TemplateResponse(request, "login.html")


@app.post("/login")
def login():
    open_terminal_running("claude auth login")
    return {"opened": True}


@app.get("/api/auth-status")
def auth_status():
    try:
        return get_auth_status()
    except ClaudeCLIError:
        return {"loggedIn": False}


@app.get("/prompt")
def prompt_page(request: Request):
    status = get_auth_status()
    if not status.get("loggedIn"):
        return RedirectResponse(url="/")

    return templates.TemplateResponse(request, "prompt.html", {"email": status.get("email", "")})


@app.get("/api/app-state")
def app_state():
    conn = db.get_connection()
    root_dir = db.get_root_dir(conn)

    active_project = None
    if _active_project_id is not None:
        row = db.get_project(conn, _active_project_id)
        if row is not None:
            active_project = _project_to_dict(row)

    projects = []
    if root_dir and active_project is None:
        _rescan_and_cache(conn, root_dir)
        projects = [_project_to_dict(r) for r in db.list_projects(conn)]

    return {"root_dir": root_dir, "active_project": active_project, "projects": projects}


@app.post("/api/settings/pick-folder")
def pick_folder_endpoint():
    return {"path": pick_folder()}


@app.post("/api/settings/root-dir")
def set_root_dir(body: dict):
    conn = db.get_connection()
    current = db.get_root_dir(conn)
    new_root = body["root_dir"]
    confirm = bool(body.get("confirm"))

    if current and current != new_root and not confirm:
        return {"needs_confirmation": True}

    if current and current != new_root:
        db.clear_projects(conn)
        global _active_project_id
        _active_project_id = None

    db.set_root_dir(conn, new_root)
    _rescan_and_cache(conn, new_root)
    projects = [_project_to_dict(r) for r in db.list_projects(conn)]

    return {"root_dir": new_root, "projects": projects}


@app.post("/api/projects/{project_id}/open")
def open_project(project_id: int):
    global _active_project_id
    conn = db.get_connection()
    row = db.get_project(conn, project_id)
    if row is None:
        return {"error": "Project not found"}

    db.mark_opened(conn, project_id)
    _active_project_id = project_id
    row = db.get_project(conn, project_id)

    return {
        "project": _project_to_dict(row),
        "session_state": db.load_session_state(row),
    }


@app.post("/api/projects/{project_id}/close")
def close_project(project_id: int, body: dict):
    global _active_project_id
    conn = db.get_connection()
    db.save_session_state(conn, project_id, body.get("session_state", {}))
    if _active_project_id == project_id:
        _active_project_id = None
    return {"closed": True}


def _active_project_cwd() -> str | None:
    if _active_project_id is None:
        return None
    conn = db.get_connection()
    row = db.get_project(conn, _active_project_id)
    return row["path"] if row is not None else None


def _session_to_dict(row) -> dict:
    return {
        "card_id": row["id"],
        "project_id": row["project_id"],
        "phase": row["phase"],
        "console_text": row["console_text"],
        "interview": json.loads(row["interview_json"]) if row["interview_json"] else None,
        "details": json.loads(row["details_json"]) if row["details_json"] else None,
        "error": row["error_text"],
        "needs_github_login": bool(row["needs_github_login"]),
    }


def _parse_details(text: str) -> dict:
    prd = None
    issues = []
    for match in _DETAIL_RE.finditer(text):
        kind, number, title = match.group(1).lower(), int(match.group(2)), match.group(3).strip()
        if kind == "prd" and prd is None:
            prd = {"number": number, "title": title}
        elif kind == "issue":
            issues.append({"number": number, "title": title})
    return {"prd": prd, "issues": issues, "raw": text}


def _run_phase_step(conn, row, *, phase: str, prompt: str, cwd: str) -> tuple[bool, str | None]:
    """Run one /to-prd or /to-issues step, updating the session row. Returns (ok, claude_session_id)."""
    db.update_session(conn, row["id"], phase=phase, error_text=None, needs_github_login=0)
    try:
        result = run_prompt(prompt, session_id=row["claude_session_id"], cwd=cwd)
    except ClaudeCLIError as e:
        message = str(e)
        db.update_session(
            conn,
            row["id"],
            error_text=message,
            needs_github_login=1 if _AUTH_FAILURE_RE.search(message) else 0,
        )
        return False, None

    claude_session_id = result["session_id"]
    db.update_session(
        conn,
        row["id"],
        claude_session_id=claude_session_id,
        console_text=row["console_text"] + "\n\n" + result["result"],
    )
    return True, claude_session_id


def _advance_past_grilling(conn, row, cwd: str) -> None:
    """Grilling just finished: run /to-prd, /to-issues, then auto-/clear and pool the session."""
    ok, claude_session_id = _run_phase_step(
        conn, row, phase="creating_prd", prompt="/to-prd", cwd=cwd
    )
    if not ok:
        return

    row = db.get_session(conn, row["id"])
    ok, claude_session_id = _run_phase_step(
        conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd
    )
    if not ok:
        return

    row = db.get_session(conn, row["id"])
    details = _parse_details(row["console_text"])
    db.update_session(conn, row["id"], phase="details", details_json=json.dumps(details))

    new_session_id = clear_session(claude_session_id, cwd=cwd)
    db.mark_session_available(conn, row["id"], new_session_id)


@app.get("/api/projects/{project_id}/sessions")
def list_sessions(project_id: int):
    conn = db.get_connection()
    return {"sessions": [_session_to_dict(r) for r in db.list_sessions_for_project(conn, project_id)]}


@app.post("/api/session/start")
def start_session(body: dict):
    project_id = _active_project_id
    cwd = _active_project_cwd()
    if project_id is None:
        return {"error": "No active project"}

    conn = db.get_connection()
    reused = db.claim_available_session(conn, project_id)
    resume_id = reused["claude_session_id"] if reused is not None else None

    row_id = db.create_session(conn, project_id, claude_session_id=resume_id)

    try:
        result = run_prompt(f"/do {body['prompt']}", session_id=resume_id, cwd=cwd)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, row_id, error_text=message, needs_github_login=needs_login)
        return {"card_id": row_id, "error": message, "needs_github_login": bool(needs_login)}

    parsed = parse_grilling_response(result["result"])
    db.update_session(
        conn,
        row_id,
        claude_session_id=result["session_id"],
        console_text=result["result"],
        interview_json=json.dumps(parsed),
    )

    return {
        "card_id": row_id,
        "session_id": result["session_id"],
        "raw": result["result"],
        **parsed,
    }


@app.post("/api/session/continue")
def continue_session(body: dict):
    conn = db.get_connection()
    row = db.get_session(conn, body["card_id"])
    if row is None:
        return {"error": "Session not found"}

    cwd = _active_project_cwd()
    try:
        result = run_prompt(body["reply"], session_id=row["claude_session_id"], cwd=cwd)
    except ClaudeCLIError as e:
        message = str(e)
        needs_login = 1 if _AUTH_FAILURE_RE.search(message) else 0
        db.update_session(conn, row["id"], error_text=message, needs_github_login=needs_login)
        return {"card_id": row["id"], "error": message, "needs_github_login": bool(needs_login)}

    parsed = parse_grilling_response(result["result"])
    console_text = row["console_text"] + "\n\n" + result["result"]
    db.update_session(
        conn,
        row["id"],
        claude_session_id=result["session_id"],
        console_text=console_text,
        interview_json=json.dumps(parsed),
    )

    if parsed["sections"]:
        return {"card_id": row["id"], "phase": "grilling", "raw": result["result"], **parsed}

    row = db.get_session(conn, row["id"])
    _advance_past_grilling(conn, row, cwd)
    return _session_to_dict(db.get_session(conn, row["id"]))


@app.post("/api/sessions/{card_id}/retry")
def retry_session(card_id: int):
    conn = db.get_connection()
    row = db.get_session(conn, card_id)
    if row is None:
        return {"error": "Session not found"}

    cwd = _active_project_cwd()
    if row["phase"] == "creating_prd":
        _advance_past_grilling(conn, row, cwd)
    elif row["phase"] == "creating_issues":
        ok, claude_session_id = _run_phase_step(
            conn, row, phase="creating_issues", prompt="/to-issues", cwd=cwd
        )
        if ok:
            row = db.get_session(conn, row["id"])
            details = _parse_details(row["console_text"])
            db.update_session(conn, row["id"], phase="details", details_json=json.dumps(details))
            new_session_id = clear_session(claude_session_id, cwd=cwd)
            db.mark_session_available(conn, row["id"], new_session_id)

    return _session_to_dict(db.get_session(conn, card_id))


@app.post("/api/github-login")
def github_login():
    open_terminal_running("gh auth login")
    return {"opened": True}


@app.get("/api/github-auth-status")
def github_auth_status():
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return {"logged_in": result.returncode == 0}
