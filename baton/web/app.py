from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from baton import db
from baton.cli_client import ClaudeCLIError, get_auth_status, run_prompt
from baton.folder_picker import pick_folder
from baton.projects import scan_projects
from baton.qa_parser import parse_grilling_response
from baton.terminal import open_terminal_running

BASE_DIR = Path(__file__).parent

app = FastAPI()
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


@app.post("/api/session/start")
def start_session(body: dict):
    skill = body.get("skill", "do")
    try:
        result = run_prompt(f"/{skill} {body['prompt']}", cwd=_active_project_cwd())
    except ClaudeCLIError as e:
        return {"error": str(e)}

    return {
        "session_id": result["session_id"],
        "raw": result["result"],
        **parse_grilling_response(result["result"]),
    }


@app.post("/api/session/continue")
def continue_session(body: dict):
    try:
        result = run_prompt(body["reply"], session_id=body["session_id"], cwd=_active_project_cwd())
    except ClaudeCLIError as e:
        return {"error": str(e)}

    return {
        "session_id": result["session_id"],
        "raw": result["result"],
        **parse_grilling_response(result["result"]),
    }
