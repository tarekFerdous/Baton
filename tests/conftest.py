import pytest
from fastapi.testclient import TestClient

from baton import db
from baton.web import app as app_module


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", tmp_path / "baton.db")
    monkeypatch.setattr(app_module, "_active_project_id", None)
    return TestClient(app_module.app)
