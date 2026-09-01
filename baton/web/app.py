from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from baton.cli_client import ClaudeCLIError, get_auth_status, run_prompt
from baton.qa_parser import parse_grilling_response
from baton.terminal import open_terminal_running

BASE_DIR = Path(__file__).parent

app = FastAPI()
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


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


@app.post("/api/session/start")
def start_session(body: dict):
    skill = body.get("skill", "do")
    try:
        result = run_prompt(f"/{skill} {body['prompt']}")
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
        result = run_prompt(body["reply"], session_id=body["session_id"])
    except ClaudeCLIError as e:
        return {"error": str(e)}

    return {
        "session_id": result["session_id"],
        "raw": result["result"],
        **parse_grilling_response(result["result"]),
    }
