"""Not every call is a service call.

The shop sells boats, kayaks, sailboats and e-bikes, runs a club, stores and
winterizes, and takes vendor and staff calls. These tests pin down that only
the conversations that actually concern a work order can touch one.
"""
from __future__ import annotations

import datetime as dt

import pytest

from crm.ai.schemas import (
    ConversationAnalysis,
    ExtractedFact,
    ExtractedNeed,
    ExtractedPerson,
    ExtractedTask,
    TicketUpdate,
)
from crm.models import (
    WORK_ORDER_CATEGORIES,
    CallCategory,
    ContactKind,
    Direction,
    Fact,
    Interaction,
    InteractionKind,
    Need,
    ProductLine,
    Task,
    TicketNote,
)
from crm.services import tickets as ticket_service
from crm.services.identity import resolve_contact
from crm.services.profile import apply_analysis
from tests.test_tickets import JOB, FakeClient


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient([dict(JOB)])
    monkeypatch.setattr(ticket_service, "get_client", lambda: client)
    return client


def make_interaction(session, contact):
    i = Interaction(
        contact_id=contact.id,
        kind=InteractionKind.call,
        direction=Direction.inbound,
        occurred_at=dt.datetime(2024, 9, 3, 14, 30),
        source="test",
        source_ref=f"t{dt.datetime.utcnow().timestamp()}",
    )
    session.add(i)
    session.flush()
    return i


def analysis(**overrides) -> ConversationAnalysis:
    base = dict(
        category="service",
        category_confidence=0.9,
        product_line="powerboat",
        contact_kind="customer",
        summary="s",
        outcome="o",
        person=ExtractedPerson(name_confidence=0.9),
    )
    base.update(overrides)
    return ConversationAnalysis(**base)


# --------------------------------------------------------------------------- #
# the gate
# --------------------------------------------------------------------------- #
def test_only_four_categories_can_touch_a_work_order():
    assert {c.value for c in WORK_ORDER_CATEGORIES} == {
        "service", "parts", "storage", "billing"
    }


@pytest.mark.parametrize("category", ["service", "parts", "storage", "billing"])
def test_work_order_categories_stage_a_note(session, fake_client, category):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    staged = ticket_service.stage_notes(
        session, interaction,
        analysis(
            category=category,
            ticket_updates=[TicketUpdate(ticket_id="WO-10432", note="Relevant")],
        ),
    )
    assert len(staged) == 1


@pytest.mark.parametrize(
    "category",
    ["sales", "boat_club", "rental", "vendor", "internal", "personal", "spam", "other"],
)
def test_other_categories_never_touch_a_work_order(session, fake_client, category):
    """The kayak-enquiry case: a customer with a boat in the shop calls about
    something else entirely. The model may still hand back a ticket update;
    the gate is what drops it."""
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)

    staged = ticket_service.stage_notes(
        session, interaction,
        analysis(
            category=category,
            ticket_updates=[TicketUpdate(ticket_id="WO-10432", note="Wrong job")],
        ),
    )
    assert staged == []
    assert session.query(TicketNote).count() == 0
    assert interaction.ticket_id is None


def test_a_kayak_sale_from_a_service_customer_stays_separate(session, fake_client):
    ticket_service.sync_tickets(session)
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)

    apply_analysis(
        session, contact, interaction,
        analysis(
            category="sales",
            product_line="kayak",
            summary="Dave asked what kayaks are left in stock.",
            needs=[ExtractedNeed(title="Wants a touring kayak", description="14ft-ish")],
        ),
    )
    ticket_service.stage_notes(session, interaction, analysis(category="sales"))

    assert interaction.category == CallCategory.sales
    assert interaction.product_line == ProductLine.kayak
    assert session.query(TicketNote).count() == 0
    # The need is still captured - it just belongs to the person, not the job.
    assert session.query(Need).filter_by(contact_id=contact.id).count() == 1


# --------------------------------------------------------------------------- #
# routing is recorded
# --------------------------------------------------------------------------- #
def test_category_and_product_line_land_on_the_interaction(session):
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    apply_analysis(
        session, contact, interaction,
        analysis(category="boat_club", product_line="none", category_confidence=0.8),
    )
    assert interaction.category == CallCategory.boat_club
    assert interaction.product_line == ProductLine.none
    assert interaction.category_confidence == 0.8


def test_vendor_call_marks_the_contact_as_a_vendor(session):
    contact = resolve_contact(session, phone="8005551000", name="Yamaha Rep")
    assert contact.kind == ContactKind.customer
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(category="vendor", contact_kind="vendor", product_line="engine"),
    )
    assert contact.kind == ContactKind.vendor


def test_an_unsure_read_does_not_reclassify_a_contact(session):
    """Misfiling a customer as a vendor drops them off the lists that matter."""
    contact = resolve_contact(session, phone="8155551234", name="Dave")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(category="vendor", contact_kind="vendor", category_confidence=0.4),
    )
    assert contact.kind == ContactKind.customer


# --------------------------------------------------------------------------- #
# noise
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("category", ["spam", "internal"])
def test_spam_and_internal_calls_build_no_profile(session, category):
    contact = resolve_contact(session, phone="8005559999")
    interaction = make_interaction(session, contact)
    counts = apply_analysis(
        session, contact, interaction,
        analysis(
            category=category,
            contact_kind="other" if category == "spam" else "staff",
            facts=[ExtractedFact(category="personal", text="Sells extended warranties")],
            needs=[ExtractedNeed(title="Wants to sell us something", description="x")],
            tasks=[ExtractedTask(title="Call them back")],
        ),
    )
    assert counts == {
        "facts": 0, "facts_confirmed": 0, "needs": 0, "tasks": 0, "appointments": 0
    }
    assert session.query(Fact).count() == 0
    assert session.query(Need).count() == 0
    assert session.query(Task).count() == 0
    # It is still recorded - you can see the robocall happened.
    assert interaction.category == CallCategory(category)
    assert interaction.processed is True


def test_a_real_customer_call_still_builds_everything(session):
    contact = resolve_contact(session, phone="8155551234")
    interaction = make_interaction(session, contact)
    counts = apply_analysis(
        session, contact, interaction,
        analysis(
            facts=[ExtractedFact(category="personal", text="Runs it on the Illinois River")],
            needs=[ExtractedNeed(title="Impeller replacement", description="Overheating")],
            tasks=[ExtractedTask(title="Quote the impeller job")],
        ),
    )
    assert counts["facts"] == 1 and counts["needs"] == 1 and counts["tasks"] == 1


def test_staff_contacts_get_no_profile_rewrite(session, monkeypatch):
    from crm.services import profile

    contact = resolve_contact(session, phone="8155550199", name="Shop iPad")
    contact.kind = ContactKind.staff
    session.flush()

    called = []
    monkeypatch.setattr(
        profile.enrich, "write_profile", lambda **kw: called.append(1)
    )
    assert profile.refresh_profile(session, contact) is False
    assert called == []
