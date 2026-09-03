import pytest
from fastapi.testclient import TestClient

from baton import db, live_stream
from baton.web import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "baton.db")
    monkeypatch.setattr(app_module, "_active_project_id", None)
    return TestClient(app_module.app)


@pytest.fixture(autouse=True)
def _isolated_live_stream(monkeypatch):
    """Session row ids restart at 1 in every test's fresh tmp db, but
    `live_stream`'s buffers are process-global -- reset them per test so
    unrelated tests never share a card_id's event history."""
    monkeypatch.setattr(live_stream, "_buffers", {})
    monkeypatch.setattr(live_stream, "_subscribers", {})
    monkeypatch.setattr(live_stream, "_last_usage", None)
