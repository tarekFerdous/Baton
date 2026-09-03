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


class ClaudeCLIError(RuntimeError):
    pass


def _clean_env() -> dict:
    env = os.environ.copy()
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    return env


def run_prompt(
    prompt: str,
    *,
    session_id: str | None = None,
    cwd: str | None = None,
    model: str | None = None,
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
    if session_id:
        args += ["--resume", session_id]
    if model:
        args += ["--model", model]

    result = subprocess.run(
        args,
        input=prompt,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=platform.system() == "Windows",
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
) -> Iterator[dict]:
    """Run `claude -p` with streaming NDJSON output, yielding each raw event dict as it arrives.

    Same auth/env handling and stdin-piped-prompt approach as `run_prompt`, but
    delivers the turn incrementally instead of buffering the whole result. This
    is a generator: nothing runs until it's iterated.
    """
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
    if session_id:
        args += ["--resume", session_id]
    if model:
        args += ["--model", model]

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
    )

    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    process.stdin.write(prompt)
    process.stdin.close()

    try:
        for line in process.stdout:
            line = line.strip()
            if line:
                yield json.loads(line)
    finally:
        process.stdout.close()
        returncode = process.wait()
        stderr = process.stderr.read()
        process.stderr.close()
        if returncode != 0:
            raise ClaudeCLIError(f"claude exited with {returncode}: {stderr.strip()}")


def clear_session(session_id: str, *, cwd: str | None = None) -> str:
    """Clear a session's context and return the new session_id it continues as."""
    response = run_prompt("/clear", session_id=session_id, cwd=cwd)
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
    )

    if result.returncode != 0:
        raise ClaudeCLIError(f"claude auth status exited with {result.returncode}: {result.stderr.strip()}")

    return json.loads(result.stdout)
