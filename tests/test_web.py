"""The app boots, every page renders, and the webhook enforces its secret."""
from __future__ import annotations

from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from crm.db import engine
from crm.models import Base
from crm.services.identity import resolve_contact


@asynccontextmanager
async def _no_lifespan(_app):
    yield


@pytest.fixture(scope="module")
def client():
    Base.metadata.create_all(engine)
    from crm.web import app as web

    # Don't spawn the scheduler's timers inside the test process.
    monkey = pytest.MonkeyPatch()
    monkey.setattr(web, "lifespan", None, raising=False)
    web.app.router.lifespan_context = _no_lifespan
    with TestClient(web.app) as c:
        yield c
    monkey.undo()


@pytest.mark.parametrize(
    "path", ["/", "/contacts", "/calls", "/tasks", "/calendar", "/api/health"]
)
def test_pages_render(client, path):
    resp = client.get(path)
    assert resp.status_code == 200, resp.text[:400]


def test_contact_page_renders(client):
    from crm.db import session_scope

    with session_scope() as s:
        contact = resolve_contact(s, phone="6135554321", name="Test Person")
        contact_id = contact.id

    resp = client.get(f"/contacts/{contact_id}")
    assert resp.status_code == 200
    assert "Test Person" in resp.text


def test_missing_contact_is_404(client):
    assert client.get("/contacts/999999").status_code == 404


def test_search_does_not_break_on_odd_input(client):
    assert client.get("/contacts", params={"q": "%_'"}).status_code == 200


def test_webhook_rejects_a_bad_secret(client, monkeypatch):
    from crm.config import settings

    monkeypatch.setattr(settings, "sangoma_webhook_secret", "s3cret")
    assert client.post("/api/sangoma/recording", json={}).status_code == 401
    assert (
        client.post(
            "/api/sangoma/recording", json={}, headers={"X-Webhook-Secret": "wrong"}
        ).status_code
        == 401
    )


def test_webhook_needs_an_actual_recording(client, monkeypatch):
    from crm.config import settings

    monkeypatch.setattr(settings, "sangoma_webhook_secret", "")
    resp = client.post("/api/sangoma/recording", json={"src": "6135551234"})
    assert resp.status_code == 400
    assert "recording" in resp.json()["detail"]
