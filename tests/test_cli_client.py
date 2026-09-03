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
