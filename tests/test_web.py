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
    "path",
    ["/", "/contacts", "/calls", "/tasks", "/calendar", "/intake", "/tickets",
     "/api/health"],
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


def test_intake_upload_rejects_an_empty_recording(client):
    resp = client.post("/api/intake", files={"file": ("x.webm", b"", "audio/webm")})
    assert resp.status_code == 400


def test_missing_intake_is_404(client):
    assert client.get("/intake/999999").status_code == 404
    assert client.get("/api/intake/999999").status_code == 404


def test_intake_upload_accepts_a_recording(client, monkeypatch):
    """Exercises the real upload path - a NameError here is a 500, not a 400."""
    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\x00\x00" * 4000)

    from crm.web import app as web

    monkeypatch.setattr(web.BackgroundTasks, "add_task", lambda self, *a, **k: None)
    resp = client.post(
        "/api/intake",
        files={"file": ("walkin-test.wav", buf.getvalue(), "audio/wav")},
        data={"taken_by": "Sam"},
    )
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "transcribing"
    assert client.get(f"/api/intake/{body['intake_id']}").status_code == 200


def test_ensure_schema_adds_a_missing_column(tmp_path):
    """create_all never alters an existing table; ensure_schema is what does."""
    import sqlalchemy as sa

    db = tmp_path / "old.db"
    engine = sa.create_engine(f"sqlite:///{db}")
    # A table shaped like an older release: no ticket_id.
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE interactions (id INTEGER PRIMARY KEY, kind VARCHAR)"
        ))

    import crm.db as db_module

    original = db_module.engine
    db_module.engine = engine
    try:
        db_module.ensure_schema()
    finally:
        db_module.engine = original

    columns = {c["name"] for c in sa.inspect(engine).get_columns("interactions")}
    assert "ticket_id" in columns
    assert "summary" in columns
    assert "category" in columns


def test_ensure_schema_backfills_a_not_null_column(tmp_path):
    """contacts.kind is NOT NULL with a Python default; SQLite cannot add that
    directly, so it goes in nullable and existing rows are backfilled."""
    import sqlalchemy as sa

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE contacts (id INTEGER PRIMARY KEY, display_name VARCHAR)"
        ))
        conn.execute(sa.text("INSERT INTO contacts (display_name) VALUES ('Dave')"))

    import crm.db as db_module

    original = db_module.engine
    db_module.engine = engine
    try:
        db_module.ensure_schema()
    finally:
        db_module.engine = original

    with engine.connect() as conn:
        assert conn.execute(sa.text("SELECT kind FROM contacts")).scalar() == "customer"
        # A callable default is not resolved - stamping history with today
        # would be worse than a null.
        assert conn.execute(sa.text("SELECT created_at FROM contacts")).scalar() is None


@pytest.mark.parametrize("path", ["/conversations", "/contacts?kind=vendor"])
def test_filtered_pages_render(client, path):
    assert client.get(path).status_code == 200


@pytest.mark.parametrize(
    "path", ["/conversations?category=sales", "/conversations?category=vendor"]
)
def test_category_filters_render(client, path):
    assert client.get(path).status_code == 200


def test_bad_filter_values_are_rejected_not_ignored(client):
    assert client.get("/conversations", params={"category": "nope"}).status_code == 400
    assert client.get("/contacts", params={"kind": "nope"}).status_code == 400
