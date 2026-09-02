import json

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
