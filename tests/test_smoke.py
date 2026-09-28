"""Smoke test: the application imports and a fresh client starts."""

from fastapi.testclient import TestClient

from app.main import app
from app.store import get_store


def test_app_starts():
    get_store().reset()
    client = TestClient(app)
    resp = client.get("/openapi.json")
    assert resp.status_code == 200
