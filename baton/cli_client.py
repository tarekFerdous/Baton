"""Thin wrapper around the `claude` CLI, run as a subprocess.

Deliberately does not use the Claude Agent SDK: the SDK requires an API key
and always bills pay-per-token, while spawning the CLI directly can run
under the user's Claude subscription login instead. See CLAUDE.md.
"""

import json
import os
import platform
import subprocess
from collections.abc import Iterator
from pathlib import Path


class ClaudeCLIError(RuntimeError):
    pass


# Baton's own private, plugin-scoped skill set (do/grilling/to-prd/to-issues/
# implement/qa/etc, namespaced as /baton:*) -- shipped inside the `baton`
# package itself, never inside a target project's repo. Passed via
# `--plugin-dir` on every subprocess call so Baton-driven sessions never see
# (and can never accidentally invoke) the user's global ~/.claude/skills.
_PLUGIN_DIR = str(Path(__file__).parent / "claude_plugin")


def _plugin_args() -> list[str]:
    return ["--plugin-dir", _PLUGIN_DIR]


def _effort_args(effort: str | None) -> list[str]:
    """"auto" (Baton's default) and `None` both omit `--effort` entirely,
    letting the model's own built-in default apply -- "auto" is not a valid
    `--effort` flag value (only low/medium/high/xhigh/max are), so there is
    no flag that means "auto" here, only the absence of one."""
    if not effort or effort == "auto":
        return []
    return ["--effort", effort]


# One long-lived `claude` process per Baton session (keyed by the session's
# DB row id, stable across turns even before a claude_session_id exists),
# used by `stream_prompt`'s `card_id` path so the /do chain's prompt-cache
# prefix survives across grilling -> /to-prd -> /to-issues instead of being
# rebuilt by a fresh subprocess on every turn. Process-lifetime only --
# cleared implicitly on a server restart, which is exactly the case
# `stream_prompt`'s fallback (respawn + `--resume`) exists to handle.
_persistent_processes: dict[int, subprocess.Popen] = {}


def _clean_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def _isolated_process_group() -> dict:
    """On Windows, spawn the child in its own process group so it's immune to
    CTRL_C_EVENT.

    `claude` runs under `cmd.exe` here (shell=True). uvicorn's `--reload`
    restarts its server process on a file change by sending it CTRL_C_EVENT,
    which Windows broadcasts to every process sharing the console -- including
    an in-flight `cmd.exe`/`claude` child, whose default reaction to that is
    to print "Terminate batch job (Y/N)?" to stdout and hang waiting for an
    answer nobody gives it. A totally unrelated dev-server reload (e.g. from
    an unrelated file edit while a `/implement` turn is mid-flight) would
    otherwise tear down that turn. CREATE_NEW_PROCESS_GROUP makes Windows
    exempt the child from CTRL_C_EVENT entirely; `Popen.kill()`/`terminate()`
    still work regardless, since those target the process by handle."""
    if platform.system() == "Windows":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


def run_prompt(
    prompt: str,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
) -> dict:
    """Run a single non-interactive `claude -p` call and return the parsed JSON result.

    Pass `session_id` (from a previous call's result) to continue that
    conversation instead of starting a fresh one.

    The prompt is piped via stdin rather than passed as a CLI argument: on
    Windows, `claude` is invoked through `cmd.exe` (it's an npm .cmd shim),
    and a multi-line prompt passed as an argument gets its embedded newlines
    treated as command separators, silently truncating the call.
    """
    env = _clean_env()

    args = ["claude", "-p", "--output-format", "json", "--dangerously-skip-permissions"]
    args += _plugin_args()
    if session_id:
        args += ["--resume", session_id]
    if model:
        args += ["--model", model]
    args += _effort_args(effort)

    result = subprocess.run(
        args,
        input=prompt,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=platform.system() == "Windows",
        **_isolated_process_group(),
    )

    if result.returncode != 0:
        raise ClaudeCLIError(f"claude exited with {result.returncode}: {result.stderr.strip()}")

    return json.loads(result.stdout)


def stream_prompt(
    prompt: str,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    card_id: int | None = None,
) -> Iterator[dict]:
    """Run `claude -p` with streaming NDJSON output, yielding each raw event dict as it arrives.

    Same auth/env handling and stdin-piped-prompt approach as `run_prompt`, but
    delivers the turn incrementally instead of buffering the whole result. This
    is a generator: nothing runs until it's iterated.

    `card_id=None` (the default, used by /implement and /qa) keeps this
    exact one-shot-per-call behavior: a fresh subprocess is spawned, the
    prompt is piped to stdin, stdin is closed, and the process exits when
    the turn is done.

    Passing `card_id` routes the turn through `_stream_prompt_persistent`
    instead: a `claude` process is kept alive across every call sharing
    that `card_id` (started on the first such call, reused on later ones)
    so the prompt-cache prefix built on turn one survives for the rest of
    the conversation, the way an interactive terminal session does. Used by
    the /do chain (grilling, /to-prd, /to-issues) via `session_runner`.
    """
    if card_id is not None:
        yield from _stream_prompt_persistent(
            card_id, prompt, session_id=session_id, cwd=cwd, model=model, effort=effort
        )
        return

    env = _clean_env()

    args = [
        "claude",
        "-p",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    args += _plugin_args()
    if session_id:
        args += ["--resume", session_id]
    if model:
        args += ["--model", model]
    args += _effort_args(effort)

    process = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        shell=platform.system() == "Windows",
        **_isolated_process_group(),
    )

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(prompt)
    process.stdin.close()

    # Set instead of raised immediately: raising inside the `try` below would
    # just get clobbered by the `finally` block's own `ClaudeCLIError` once
    # `process.kill()` makes `returncode` non-zero. Recording it and `break`ing
    # keeps the `try` exception-free so `finally` can raise this one instead.
    parse_error: ClaudeCLIError | None = None

    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                # Not a transient parse hiccup -- something (a killed/orphaned
                # process, a CLI update banner, etc.) put non-JSON text on
                # stdout. Kill the process so `wait()` below returns promptly
                # instead of blocking on a process that may never exit on its
                # own, and surface the offending text so this is debuggable
                # instead of a bare "char 0" JSON error.
                parse_error = ClaudeCLIError(f"claude produced non-JSON output: {line!r}")
                try:
                    process.kill()
                except OSError:
                    pass
                break
    finally:
        process.stdout.close()
        returncode = process.wait()
        stderr = process.stderr.read()
        process.stderr.close()
        if parse_error is not None:
            raise parse_error
        if returncode != 0:
            raise ClaudeCLIError(f"claude exited with {returncode}: {stderr.strip()}")


def _spawn_persistent_process(
    *, cwd: str | None, model: str | None, effort: str | None, resume_session_id: str | None
) -> subprocess.Popen:
    env = _clean_env()

    args = [
        "claude",
        "-p",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--dangerously-skip-permissions",
    ]
    args += _plugin_args()
    if resume_session_id:
        args += ["--resume", resume_session_id]
    if model:
        args += ["--model", model]
    args += _effort_args(effort)

    return subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=cwd,
        env=env,
        text=True,
        encoding="utf-8",
        shell=platform.system() == "Windows",
        **_isolated_process_group(),
    )


def _stream_prompt_persistent(
    card_id: int, prompt: str, *, session_id: str | None, cwd: str | None, model: str | None, effort: str | None
) -> Iterator[dict]:
    """One turn against the long-lived process for `card_id`, spawning it
    first if this is the first turn for that id or the previously-known
    process is gone (killed, exited, or -- the fallback case -- the Baton
    server itself restarted and lost the in-memory handle entirely).

    In the fallback case `session_id` carries the last known
    claude_session_id, so the new process is started with `--resume`,
    reconstituting the conversation at the cost of one full-price turn --
    the process then stays alive and cached for every turn after that,
    rather than the chain falling back to per-turn spawning for good.

    Unlike `stream_prompt`'s one-shot path, stdin is written but never
    closed here (that would end the process after a single turn), and
    stdout is only read up to the turn's `result` event -- the process and
    its pipes are left open for the next call sharing this `card_id`.
    """
    process = _persistent_processes.get(card_id)
    if process is None or process.poll() is not None:
        process = _spawn_persistent_process(cwd=cwd, model=model, effort=effort, resume_session_id=session_id)
        _persistent_processes[card_id] = process

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    message = {"type": "user", "message": {"role": "user", "content": prompt}}
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()

    parse_error: ClaudeCLIError | None = None
    saw_result = False

    try:
        for line in process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                parse_error = ClaudeCLIError(f"claude produced non-JSON output: {line!r}")
                break
            yield event
            if event.get("type") == "result":
                saw_result = True
                break
    finally:
        if parse_error is not None or not saw_result:
            # The process is unusable for a future turn either way -- tear
            # it down and drop it from the registry so the next call for
            # this card_id spawns a fresh one instead of writing into a
            # broken pipe.
            _persistent_processes.pop(card_id, None)
            try:
                process.kill()
            except OSError:
                pass
            process.stdout.close()
            stderr = process.stderr.read()
            process.stderr.close()
            returncode = process.wait()
            if parse_error is not None:
                raise parse_error
            raise ClaudeCLIError(f"claude exited with {returncode} before completing the turn: {stderr.strip()}")


def close_persistent_session(card_id: int) -> None:
    """Signal a card's persistent process (if any) to exit by closing its
    stdin, then reap it. Safe to call when no persistent process is running
    for this `card_id`."""
    process = _persistent_processes.pop(card_id, None)
    if process is None:
        return
    try:
        if process.stdin is not None:
            process.stdin.close()
    except OSError:
        pass
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def clear_session(
    session_id: str,
    *,
    cwd: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    card_id: int | None = None,
) -> str:
    """Clear a session's context and return the new session_id it continues as.

    When `card_id` names a still-live persistent process, the /clear turn
    is sent through it (staying on the warm cache for this last turn) and
    the process is then closed -- the session is about to be pooled for
    reuse by a non-persistent path (e.g. /implement), so there's no reason
    to keep the OS process around past this point. Otherwise (no `card_id`,
    or its process is already gone -- e.g. after a server restart) this
    falls back to the original one-shot `run_prompt` call.
    """
    if card_id is not None and card_id in _persistent_processes:
        events = list(
            stream_prompt(prompt="/clear", session_id=session_id, cwd=cwd, model=model, effort=effort, card_id=card_id)
        )
        close_persistent_session(card_id)
        result_event = next(e for e in events if e.get("type") == "result")
        return result_event["session_id"]

    response = run_prompt("/clear", session_id=session_id, cwd=cwd, model=model, effort=effort)
    return response["session_id"]


def get_auth_status() -> dict:
    """Return the parsed output of `claude auth status`."""
    env = _clean_env()

    result = subprocess.run(
        ["claude", "auth", "status", "--json"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=platform.system() == "Windows",
        **_isolated_process_group(),
    )

    if result.returncode != 0:
        raise ClaudeCLIError(f"claude auth status exited with {result.returncode}: {result.stderr.strip()}")

    return json.loads(result.stdout)
