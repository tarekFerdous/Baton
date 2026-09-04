import io
import json

import pytest

from baton import cli_client


def test_run_prompt_skips_permission_checks(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "abc"})
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(cli_client.subprocess, "run", fake_run)

    cli_client.run_prompt("hello")

    assert "--dangerously-skip-permissions" in captured["args"]


def test_run_prompt_loads_baton_own_plugin(monkeypatch):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "abc"})
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(cli_client.subprocess, "run", fake_run)

    cli_client.run_prompt("hello")

    assert "--plugin-dir" in captured["args"]
    plugin_dir = captured["args"][captured["args"].index("--plugin-dir") + 1]
    assert plugin_dir == cli_client._PLUGIN_DIR


@pytest.mark.parametrize("effort", ["high", "medium", "low"])
def test_run_prompt_passes_effort_flag_for_high_medium_low(monkeypatch, effort):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "abc"})
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(cli_client.subprocess, "run", fake_run)

    cli_client.run_prompt("hello", effort=effort)

    assert "--effort" in captured["args"]
    assert captured["args"][captured["args"].index("--effort") + 1] == effort


@pytest.mark.parametrize("effort", ["auto", None])
def test_run_prompt_omits_effort_flag_for_auto_or_unset(monkeypatch, effort):
    captured = {}

    class FakeResult:
        returncode = 0
        stdout = json.dumps({"session_id": "abc"})
        stderr = ""

    def fake_run(args, **kwargs):
        captured["args"] = args
        return FakeResult()

    monkeypatch.setattr(cli_client.subprocess, "run", fake_run)

    cli_client.run_prompt("hello", effort=effort)

    assert "--effort" not in captured["args"]


class FakePopen:
    def __init__(self, args, **kwargs):
        self.args = args
        self.stdin = io.StringIO()
        self.stdout = io.StringIO(
            '{"type": "system", "subtype": "init"}\n{"type": "result", "result": "done"}\n'
        )
        self.stderr = io.StringIO("")
        self._returncode = 0

    def wait(self):
        return self._returncode

    def kill(self):
        self._returncode = -9


def test_stream_prompt_uses_streaming_flags_and_skips_permission_checks(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakePopen(args, **kwargs)

    monkeypatch.setattr(cli_client.subprocess, "Popen", fake_popen)

    list(cli_client.stream_prompt("hello"))

    assert "--dangerously-skip-permissions" in captured["args"]
    assert "stream-json" in captured["args"]
    assert "--include-partial-messages" in captured["args"]
    assert "--plugin-dir" in captured["args"]
    assert captured["args"][captured["args"].index("--plugin-dir") + 1] == cli_client._PLUGIN_DIR


def test_stream_prompt_passes_effort_flag(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakePopen(args, **kwargs)

    monkeypatch.setattr(cli_client.subprocess, "Popen", fake_popen)

    list(cli_client.stream_prompt("hello", effort="low"))

    assert "--effort" in captured["args"]
    assert captured["args"][captured["args"].index("--effort") + 1] == "low"


def test_stream_prompt_yields_each_parsed_ndjson_line(monkeypatch):
    monkeypatch.setattr(cli_client.subprocess, "Popen", lambda args, **kw: FakePopen(args, **kw))

    events = list(cli_client.stream_prompt("hello"))

    assert events == [{"type": "system", "subtype": "init"}, {"type": "result", "result": "done"}]


def test_stream_prompt_raises_on_nonzero_exit(monkeypatch):
    class FailingFakePopen(FakePopen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.stdout = io.StringIO("")
            self.stderr = io.StringIO("boom")
            self._returncode = 1

    monkeypatch.setattr(cli_client.subprocess, "Popen", lambda args, **kw: FailingFakePopen(args, **kw))

    with pytest.raises(cli_client.ClaudeCLIError):
        list(cli_client.stream_prompt("hello"))


def test_stream_prompt_raises_claude_cli_error_on_non_json_line(monkeypatch):
    """A stray non-JSON line on stdout (e.g. from a killed/orphaned process)
    must surface as a debuggable ClaudeCLIError, not a bare JSONDecodeError,
    and must not hang waiting on a process that may never exit on its own."""

    class GarbledFakePopen(FakePopen):
        def __init__(self, args, **kwargs):
            super().__init__(args, **kwargs)
            self.stdout = io.StringIO('{"type": "system", "subtype": "init"}\n\x00\n')

    monkeypatch.setattr(cli_client.subprocess, "Popen", lambda args, **kw: GarbledFakePopen(args, **kw))

    with pytest.raises(cli_client.ClaudeCLIError, match="non-JSON"):
        list(cli_client.stream_prompt("hello"))


# ---------------------------------------------------------------------------
# Persistent-process path (`card_id`) -- issue #61
# ---------------------------------------------------------------------------


class _FakeStdin:
    """Records each turn's write and, in response, arms the next queued
    NDJSON block on the fake process's stdout -- mimicking a real
    `claude --input-format stream-json` process where writing a turn makes
    that turn's output become readable."""

    def __init__(self, process):
        self._process = process
        self.closed = False
        self.writes = []

    def write(self, data):
        self.writes.append(data)
        if self._process.responses:
            self._process.stdout = io.StringIO(self._process.responses.pop(0))
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True


class FakePersistentPopen:
    """Test double for a long-lived `claude --input-format stream-json`
    process: each `responses` entry is the NDJSON block that becomes
    available on stdout after the corresponding stdin write (one per turn)."""

    instances: list["FakePersistentPopen"] = []

    def __init__(self, args, responses=(), **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.responses = list(responses)
        self.stdin = _FakeStdin(self)
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self._returncode = None
        self.killed = False
        FakePersistentPopen.instances.append(self)

    def poll(self):
        return self._returncode

    def wait(self, timeout=None):
        if self._returncode is None:
            self._returncode = 0
        return self._returncode

    def kill(self):
        self.killed = True
        self._returncode = -9


@pytest.fixture(autouse=True)
def _reset_persistent_processes():
    cli_client._persistent_processes.clear()
    FakePersistentPopen.instances.clear()
    yield
    cli_client._persistent_processes.clear()
    FakePersistentPopen.instances.clear()


def _result_line(session_id="p1", result="ok"):
    return json.dumps({"type": "result", "result": result, "session_id": session_id}) + "\n"


def test_stream_prompt_with_card_id_spawns_a_stream_json_input_process(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakePersistentPopen(args, responses=[_result_line()], **kwargs)

    monkeypatch.setattr(cli_client.subprocess, "Popen", fake_popen)

    events = list(cli_client.stream_prompt("hello", card_id=1, effort="max"))

    assert "--input-format" in captured["args"]
    assert "stream-json" in captured["args"]
    assert "--output-format" in captured["args"]
    assert "--dangerously-skip-permissions" in captured["args"]
    assert "--plugin-dir" in captured["args"]
    assert captured["args"][captured["args"].index("--plugin-dir") + 1] == cli_client._PLUGIN_DIR
    assert "--effort" in captured["args"]
    assert captured["args"][captured["args"].index("--effort") + 1] == "max"
    assert events == [{"type": "result", "result": "ok", "session_id": "p1"}]


def test_stream_prompt_with_card_id_reuses_the_same_process_across_calls(monkeypatch):
    monkeypatch.setattr(
        cli_client.subprocess,
        "Popen",
        lambda args, **kw: FakePersistentPopen(args, responses=[_result_line("p1"), _result_line("p1")], **kw),
    )

    list(cli_client.stream_prompt("turn one", card_id=42))
    list(cli_client.stream_prompt("turn two", session_id="p1", card_id=42))

    # Only one OS process was spawned for card_id=42 across both turns.
    assert len(FakePersistentPopen.instances) == 1
    process = FakePersistentPopen.instances[0]
    assert len(process.stdin.writes) == 2
    # stdin was never closed between turns (that would end the process).
    assert process.stdin.closed is False


def test_stream_prompt_with_card_id_does_not_close_stdin_between_turns_content(monkeypatch):
    monkeypatch.setattr(
        cli_client.subprocess,
        "Popen",
        lambda args, **kw: FakePersistentPopen(args, responses=[_result_line("p1")], **kw),
    )

    list(cli_client.stream_prompt("hi there", card_id=7))

    process = FakePersistentPopen.instances[0]
    written = json.loads(process.stdin.writes[0])
    assert written["type"] == "user"
    assert written["message"]["content"] == "hi there"


def test_stream_prompt_with_card_id_falls_back_to_resume_after_process_is_lost(monkeypatch):
    """Simulates a Baton server restart: the in-memory process registry is
    empty even though the caller still has a claude_session_id from before
    the restart. The next turn must spawn a fresh process with --resume
    instead of erroring or silently starting a brand-new conversation."""
    captured_args = []

    def fake_popen(args, **kw):
        captured_args.append(args)
        return FakePersistentPopen(args, responses=[_result_line("resumed-1")], **kw)

    monkeypatch.setattr(cli_client.subprocess, "Popen", fake_popen)

    # No process registered for card_id=99 (as if the server just restarted),
    # but the caller passes the claude_session_id it remembers from the DB.
    events = list(cli_client.stream_prompt("continue", session_id="old-session-id", card_id=99))

    assert "--resume" in captured_args[0]
    assert captured_args[0][captured_args[0].index("--resume") + 1] == "old-session-id"
    assert events == [{"type": "result", "result": "ok", "session_id": "resumed-1"}]

    # The resumed process is now registered and stays warm for later turns.
    assert cli_client._persistent_processes.get(99) is not None


def test_stream_prompt_without_card_id_still_spawns_fresh_process_per_call(monkeypatch):
    """Regression guard: /implement and /qa (card_id=None) must keep the
    original one-shot-per-call behavior, untouched by the persistent path."""
    spawned = []

    def fake_popen(args, **kw):
        p = FakePopen(args, **kw)
        spawned.append(p)
        return p

    monkeypatch.setattr(cli_client.subprocess, "Popen", fake_popen)

    list(cli_client.stream_prompt("first"))
    list(cli_client.stream_prompt("second"))

    assert len(spawned) == 2
    assert cli_client._persistent_processes == {}


def test_close_persistent_session_closes_stdin_and_deregisters(monkeypatch):
    monkeypatch.setattr(
        cli_client.subprocess,
        "Popen",
        lambda args, **kw: FakePersistentPopen(args, responses=[_result_line("p1")], **kw),
    )

    list(cli_client.stream_prompt("hello", card_id=5))
    assert 5 in cli_client._persistent_processes

    cli_client.close_persistent_session(5)

    assert 5 not in cli_client._persistent_processes
    process = FakePersistentPopen.instances[0]
    assert process.stdin.closed is True


def test_close_persistent_session_is_a_noop_when_nothing_is_registered():
    cli_client.close_persistent_session(123)  # must not raise


def test_clear_session_routes_through_a_live_persistent_process_then_closes_it(monkeypatch):
    monkeypatch.setattr(
        cli_client.subprocess,
        "Popen",
        lambda args, **kw: FakePersistentPopen(
            args, responses=[_result_line("p1"), _result_line("cleared-1")], **kw
        ),
    )

    list(cli_client.stream_prompt("grilling turn", card_id=10))
    new_id = cli_client.clear_session("p1", card_id=10)

    assert new_id == "cleared-1"
    # The persistent process was torn down after /clear, not left running.
    assert 10 not in cli_client._persistent_processes
    process = FakePersistentPopen.instances[0]
    assert process.stdin.closed is True


def test_clear_session_falls_back_to_run_prompt_when_no_persistent_process(monkeypatch):
    """card_id given, but nothing is registered for it (never persistent, or
    the server restarted and lost the handle) -- must use the original
    one-shot run_prompt path, not try to spawn a persistent process just to
    immediately tear it down."""
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return type("R", (), {"returncode": 0, "stdout": json.dumps({"session_id": "s2"}), "stderr": ""})()

    monkeypatch.setattr(cli_client.subprocess, "run", fake_run)
    popen_calls = []
    monkeypatch.setattr(cli_client.subprocess, "Popen", lambda *a, **kw: popen_calls.append((a, kw)))

    result = cli_client.clear_session("s1", card_id=999)

    assert result == "s2"
    assert popen_calls == []
    assert "--resume" in captured["args"]
