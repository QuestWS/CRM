"""Ticket mirroring, matching, and note staging. The network is stubbed."""
from __future__ import annotations

import datetime as dt

import pytest

from crm.ai.schemas import ConversationAnalysis, ExtractedPerson, TicketUpdate
from crm.models import (
    Direction,
    Interaction,
    InteractionKind,
    NoteStatus,
    ServiceTicket,
    TicketNote,
)
from crm.services import tickets as ticket_service
from crm.services.identity import resolve_contact

JOB = {
    "id": "WO-10432",
    "token": "abc",
    "customerName": "Dave Mercer",
    "customerPhone": "(815) 555-1234",
    "customerEmail": "Dave@Example.com",
    "boatInfo": "2014 Malibu Wakesetter 22",
    "workRequested": "Winterize and shrink wrap.",
    "status": "work_underway",
    "statusLabel": "Work underway",
    "alert": None,
    "amountDue": None,
    "entryCount": 3,
    "minutes": 90,
    "createdAt": "2024-09-01T10:00:00Z",
    "updatedAt": "2024-09-02T10:00:00Z",
    "paidAt": None,
}


class FakeClient:
    def __init__(self, jobs):
        self.jobs = jobs
        self.pushed = []

    def list_jobs(self, status="open"):
        return self.jobs

    def add_writer_note(self, job_id, text):
        self.pushed.append((job_id, text))
        return {"ok": True}


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient([dict(JOB)])
    monkeypatch.setattr(ticket_service, "get_client", lambda: client)
    return client


def make_interaction(session, contact, kind=InteractionKind.call):
    i = Interaction(
        contact_id=contact.id if contact else None,
        kind=kind,
        direction=Direction.inbound,
        occurred_at=dt.datetime(2024, 9, 3, 14, 30),
        source="test",
        source_ref=f"t{dt.datetime.utcnow().timestamp()}",
    )
    session.add(i)
    session.flush()
    return i


def analysis_with(*updates) -> ConversationAnalysis:
    return ConversationAnalysis(
        summary="s", outcome="o",
        person=ExtractedPerson(name_confidence=0.9),
        ticket_updates=list(updates),
    )


# --------------------------------------------------------------------------- #
def test_sync_mirrors_and_links_to_a_contact(session, fake_client):
    existing = resolve_contact(session, phone="8155551234", name="Dave Mercer")
    stats = ticket_service.sync_tickets(session)

    assert stats["created"] == 1
    ticket = session.get(ServiceTicket, "WO-10432")
    assert ticket.contact_id == existing.id
    assert ticket.customer_phone == "+18155551234"   # normalised on the way in
    assert ticket.customer_email == "dave@example.com"
    assert ticket.is_open


def test_sync_is_idempotent(session, fake_client):
    ticket_service.sync_tickets(session)
    stats = ticket_service.sync_tickets(session)
    assert stats["created"] == 0 and stats["updated"] == 1
    assert session.query(ServiceTicket).count() == 1


def test_sync_creates_a_contact_when_there_is_an_identifier(session, fake_client):
    ticket_service.sync_tickets(session)
    ticket = session.get(ServiceTicket, "WO-10432")
    assert ticket.contact_id is not None


def test_sync_skips_contact_creation_with_nothing_to_match_on(session, monkeypatch):
    bare = dict(JOB, customerPhone="", customerEmail="", id="WO-1")
    monkeypatch.setattr(ticket_service, "get_client", lambda: FakeClient([bare]))
    ticket_service.sync_tickets(session)
    # A name-only contact could never be matched to a call, so it isn't made.
    assert session.get(ServiceTicket, "WO-1").contact_id is None


def test_closed_job_drops_out_of_open(session, monkeypatch):
    monkeypatch.setattr(ticket_service, "get_client", lambda: FakeClient([dict(JOB)]))
    ticket_service.sync_tickets(session)
    done = dict(JOB, status="done", statusLabel="Done", paidAt="2024-09-05T00:00:00Z")
    monkeypatch.setattr(ticket_service, "get_client", lambda: FakeClient([done]))
    ticket_service.sync_tickets(session)
    assert session.get(ServiceTicket, "WO-10432").is_open is False


def test_open_tickets_match_by_phone_recorded_on_the_job(session, fake_client):
    """A ticket created before the contact existed still finds them."""
    ticket_service.sync_tickets(session)
    ticket = session.get(ServiceTicket, "WO-10432")
    ticket.contact_id = None
    session.flush()

    contact = resolve_contact(session, phone="8155551234", name="Dave Mercer")
    found = ticket_service.open_tickets_for(session, contact)
    assert [t.id for t in found] == ["WO-10432"]


def test_context_block_names_the_ticket(session, fake_client):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    block = ticket_service.tickets_context_block(session, contact)
    assert "WO-10432" in block and "Malibu" in block


def test_context_block_is_empty_without_open_tickets(session):
    contact = resolve_contact(session, phone="8155559999")
    assert ticket_service.tickets_context_block(session, contact) == ""


def test_staging_creates_a_pending_note_and_links_the_interaction(session, fake_client):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)

    staged = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(
            ticket_id="WO-10432",
            note="Wants the cover replaced too while it is in.",
            changes_the_work=True,
        )),
    )
    assert len(staged) == 1
    note = staged[0]
    assert note.status == NoteStatus.pending
    assert "From a phone call on 03 Sep, 14:30" in note.body
    assert "re-keying into BiT" in note.body
    assert interaction.ticket_id == "WO-10432"


def test_unknown_ticket_id_is_ignored(session, fake_client):
    """The model must copy an id from the list it was given, not invent one."""
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    staged = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(ticket_id="WO-99999", note="whatever")),
    )
    assert staged == []
    assert session.query(TicketNote).count() == 0


def test_closed_ticket_is_not_written_to(session, monkeypatch):
    monkeypatch.setattr(ticket_service, "get_client", lambda: FakeClient([dict(JOB)]))
    ticket_service.sync_tickets(session)
    session.get(ServiceTicket, "WO-10432").is_open = False
    session.flush()

    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    assert ticket_service.stage_notes(
        session, interaction, analysis_with(TicketUpdate(ticket_id="WO-10432", note="x"))
    ) == []


def test_reprocessing_the_same_interaction_does_not_double_stage(session, fake_client):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    update = analysis_with(TicketUpdate(ticket_id="WO-10432", note="Same note"))
    ticket_service.stage_notes(session, interaction, update)
    ticket_service.stage_notes(session, interaction, update)
    assert session.query(TicketNote).count() == 1


def test_push_sends_the_note_and_marks_it(session, fake_client):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    note = ticket_service.stage_notes(
        session, interaction, analysis_with(TicketUpdate(ticket_id="WO-10432", note="Hi"))
    )[0]

    ticket_service.push_note(session, note)
    assert note.status == NoteStatus.pushed and note.pushed_at is not None
    assert fake_client.pushed[0][0] == "WO-10432"
    assert "Hi" in fake_client.pushed[0][1]


def test_autopush_is_off_by_default(session, fake_client, monkeypatch):
    from crm.config import settings

    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    ticket_service.stage_notes(
        session, interaction, analysis_with(TicketUpdate(ticket_id="WO-10432", note="Hi"))
    )
    monkeypatch.setattr(settings, "servicetracker_autopush", False)
    assert ticket_service.push_all_pending(session) == {"pushed": 0, "failed": 0}
    assert not fake_client.pushed
