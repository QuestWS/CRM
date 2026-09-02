"""Winter services integration. The Apps Script console is stubbed.

The rules being pinned down here come from that repo's own CLAUDE.md: never
send a customer email, treat the sheet as read-only except for a named quote,
and never lose what a staff member wrote.
"""
from __future__ import annotations

import datetime as dt

import pytest

from crm.ai.schemas import ConversationAnalysis, ExtractedPerson, TicketUpdate
from crm.integrations import winterquotes
from crm.integrations.winterquotes import WinterQuotesClient, WinterQuotesError
from crm.models import (
    Direction,
    Interaction,
    InteractionKind,
    NoteStatus,
    WorkItem,
    WorkSystem,
)
from crm.services import tickets as ticket_service
from crm.services.identity import resolve_contact

STORAGE_ROWS = [
    {
        "qn": "QW-26-1255",
        "name": "Mercer, Dave",
        "unit": "Wakesetter 22",
        "ymm": "2014 Malibu",
        "dims": "22x8",
        "status": "Signed",
        "keys": "Board",
        "slip": "",
        "seasonDone": None,
        "balance": "$1,240.00",
        "storage": "Inside Heated",
    },
    {
        "qn": "QW-26-1300",
        "name": "Rossi, Ana",
        "unit": "Sea-Doo GTX",
        "ymm": "2021 Sea-Doo",
        "status": "Paid",
        "seasonDone": "2026-04-14",
        "balance": "Paid",
        "storage": "Outside",
    },
]

DETAIL = {
    "QW-26-1255": {
        "ok": 1, "quoteNo": "QW-26-1255", "phone": "(815) 555-1234",
        "email": "Dave@example.com", "rqList": ["Shrink wrap", "Winterize"],
        "staffNote": "",
    },
}


class FakeWinter:
    """Mimics the console API's shapes, including its odd corners."""

    def __init__(self, rows=None, notes=None):
        self.rows = rows if rows is not None else [dict(r) for r in STORAGE_ROWS]
        self.notes = dict(notes or {})
        self.lookups: list[str] = []
        self.note_writes: list[tuple[str, str]] = []

    def storage_view(self):
        return [dict(r) for r in self.rows]

    def lookup(self, quote_no):
        self.lookups.append(quote_no)
        detail = dict(DETAIL.get(quote_no, {"ok": 1, "quoteNo": quote_no}))
        detail["staffNote"] = self.notes.get(quote_no, "")
        return detail

    def get_staff_note(self, quote_no):
        return self.notes.get(quote_no, "")

    def set_staff_note(self, quote_no, note):
        if note == self.notes.get(quote_no, ""):
            return {"ok": 1, "unchanged": True}
        self.notes[quote_no] = note
        self.note_writes.append((quote_no, note))
        return {"ok": 1}


@pytest.fixture
def winter(monkeypatch):
    client = FakeWinter()
    monkeypatch.setattr(winterquotes, "get_client", lambda: client)
    return client


def make_interaction(session, contact):
    i = Interaction(
        contact_id=contact.id,
        kind=InteractionKind.call,
        direction=Direction.inbound,
        occurred_at=dt.datetime(2026, 10, 3, 14, 30),
        source="test",
        source_ref=f"t{dt.datetime.utcnow().timestamp()}",
    )
    session.add(i)
    session.flush()
    return i


def analysis_with(*updates, category="storage") -> ConversationAnalysis:
    return ConversationAnalysis(
        category=category, category_confidence=0.9,
        summary="s", outcome="o",
        person=ExtractedPerson(name_confidence=0.9),
        ticket_updates=list(updates),
    )


# --------------------------------------------------------------------------- #
# the client's guard rails
# --------------------------------------------------------------------------- #
def test_client_exposes_no_way_to_email_a_customer():
    """That repo's first rule. A capability absent cannot be called by mistake."""
    surface = {n for n in dir(WinterQuotesClient) if not n.startswith("_")}
    assert not {n for n in surface if "email" in n.lower() or "send" in n.lower()}
    assert "set_staff_note" in surface   # the one write there is


def test_empty_note_is_refused():
    client = WinterQuotesClient(exec_url="https://x/exec", pin="1234")
    with pytest.raises(WinterQuotesError):
        client.set_staff_note("QW-26-1255", "   ")


def test_unconfigured_client_says_what_is_missing():
    from crm.integrations.winterquotes import NotConfigured

    client = WinterQuotesClient(exec_url="", pin="")
    with pytest.raises(NotConfigured, match="WINTER_EXEC_URL"):
        client.storage_view()


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def test_sync_mirrors_quotes_and_reads_the_name_round(session, winter):
    stats = ticket_service.sync_winter(session)
    assert stats["created"] == 2

    item = session.get(WorkItem, "winter:QW-26-1255")
    assert item.system == WorkSystem.winter
    assert item.remote_id == "QW-26-1255"
    # storageView gives "Last, First"; a CRM contact is "First Last".
    assert item.customer_name == "Dave Mercer"
    assert item.boat_info == "2014 Malibu Wakesetter 22"
    assert item.storage_location == "Inside Heated"
    assert item.is_open


def test_a_closed_out_season_is_not_open(session, winter):
    ticket_service.sync_winter(session)
    done = session.get(WorkItem, "winter:QW-26-1300")
    assert done.season_done == "2026-04-14"
    assert done.is_open is False


def test_a_paid_boat_still_in_the_yard_stays_open(session, winter):
    """Balance is not the test - the boat is still there."""
    ticket_service.sync_winter(session)
    item = session.get(WorkItem, "winter:QW-26-1255")
    item_paid = session.get(WorkItem, "winter:QW-26-1300")
    assert item.is_open
    assert item_paid.balance == "Paid"


def test_contact_details_are_looked_up_and_linked(session, winter):
    ticket_service.sync_winter(session)
    item = session.get(WorkItem, "winter:QW-26-1255")
    assert item.customer_phone == "+18155551234"
    assert item.customer_email == "dave@example.com"
    assert item.contact_id is not None
    assert item.work_requested == "Shrink wrap; Winterize"
    # Only the open quote needed a lookup; the closed one did not.
    assert winter.lookups == ["QW-26-1255"]


def test_lookups_are_not_repeated_once_details_are_known(session, winter):
    ticket_service.sync_winter(session)
    winter.lookups.clear()
    ticket_service.sync_winter(session)
    assert winter.lookups == []


def test_lookups_are_bounded_per_run(session, monkeypatch):
    many = [
        {"qn": f"QW-26-{i}", "name": f"Test, Person{i}", "unit": "Boat", "storage": "Out"}
        for i in range(60)
    ]
    client = FakeWinter(rows=many)
    monkeypatch.setattr(winterquotes, "get_client", lambda: client)
    stats = ticket_service.sync_winter(session)
    assert stats["lookups"] == ticket_service.WINTER_LOOKUPS_PER_SYNC
    assert stats["created"] == 60   # every quote mirrored, backfill spread out


def test_winter_sync_does_not_close_service_tracker_jobs(session, winter):
    """Staleness must be scoped per system."""
    session.add(
        WorkItem(
            id="servicetracker:WO-1", system=WorkSystem.servicetracker,
            remote_id="WO-1", is_open=True,
        )
    )
    session.flush()
    ticket_service.sync_winter(session)
    assert session.get(WorkItem, "servicetracker:WO-1").is_open is True


# --------------------------------------------------------------------------- #
# matching and notes
# --------------------------------------------------------------------------- #
def test_a_storage_call_finds_the_winter_quote(session, winter):
    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    block = ticket_service.tickets_context_block(session, contact)
    assert "winter:QW-26-1255" in block
    assert "winter services quote" in block
    assert "stored: Inside Heated" in block


def test_note_is_staged_and_pushed_to_the_quote(session, winter):
    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)

    staged = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(
            ticket_id="winter:QW-26-1255",
            note="Wants the cover off before pickup.",
            changes_the_work=True,
        )),
    )
    assert len(staged) == 1
    ticket_service.push_note(session, staged[0])

    quote_no, body = winter.note_writes[0]
    assert quote_no == "QW-26-1255"
    assert "Wants the cover off before pickup." in body
    # BiT is the service tracker's system, not this one.
    assert "BiT" not in body
    assert "needs re-pricing" in body
    assert staged[0].status == NoteStatus.pushed


def test_pushing_preserves_an_existing_staff_note(session, monkeypatch):
    """The winter staff note REPLACES. Appending is what stops a call
    wiping out what somebody wrote by hand."""
    client = FakeWinter(notes={"QW-26-1255": "Owner wants a call before any teardown."})
    monkeypatch.setattr(winterquotes, "get_client", lambda: client)

    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    note = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(ticket_id="winter:QW-26-1255", note="Adding shrink wrap.")),
    )[0]
    ticket_service.push_note(session, note)

    written = client.notes["QW-26-1255"]
    assert "Owner wants a call before any teardown." in written
    assert "Adding shrink wrap." in written


def test_pushing_the_same_note_twice_does_not_duplicate_it(session, winter):
    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    note = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(ticket_id="winter:QW-26-1255", note="Same words.")),
    )[0]
    ticket_service.push_note(session, note)
    note.status = NoteStatus.pending
    ticket_service.push_note(session, note)
    assert client_note_count(winter.notes["QW-26-1255"], "Same words.") == 1


def client_note_count(text: str, needle: str) -> int:
    return text.count(needle)


def test_a_bare_quote_number_still_resolves(session, winter):
    """The model is asked for the namespaced id, but a bare number is a
    plausible answer and dropping a correct note over formatting would be
    the wrong trade."""
    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    staged = ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(ticket_id="QW-26-1255", note="Bare id")),
    )
    assert len(staged) == 1
    assert staged[0].ticket_id == "winter:QW-26-1255"


def test_a_bare_id_cannot_reach_another_customers_quote(session, winter):
    ticket_service.sync_winter(session)
    stranger = resolve_contact(session, phone="8155559999", name="Someone Else")
    interaction = make_interaction(session, stranger)
    assert ticket_service.stage_notes(
        session, interaction,
        analysis_with(TicketUpdate(ticket_id="QW-26-1255", note="Not theirs")),
    ) == []


def test_a_sales_call_never_touches_a_winter_quote(session, winter):
    ticket_service.sync_winter(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    assert ticket_service.stage_notes(
        session, interaction,
        analysis_with(
            TicketUpdate(ticket_id="winter:QW-26-1255", note="Asked about e-bikes"),
            category="sales",
        ),
    ) == []
