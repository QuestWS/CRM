"""Applying a model's analysis to the database, with the model itself stubbed."""
from __future__ import annotations

import datetime as dt

from crm.ai.schemas import (
    ConversationAnalysis,
    ExtractedAppointment,
    ExtractedFact,
    ExtractedNeed,
    ExtractedPerson,
    ExtractedTask,
)
from crm.models import (
    Appointment,
    Direction,
    Fact,
    Interaction,
    InteractionKind,
    Need,
    NeedStatus,
    Task,
)
from crm.services.identity import resolve_contact
from crm.services.profile import apply_analysis


def make_interaction(session, contact, when=None):
    i = Interaction(
        contact_id=contact.id,
        kind=InteractionKind.call,
        direction=Direction.inbound,
        occurred_at=when or dt.datetime(2024, 1, 15, 14, 30),
        source="test",
        source_ref=f"t{dt.datetime.utcnow().timestamp()}",
    )
    session.add(i)
    session.flush()
    return i


def analysis(**overrides) -> ConversationAnalysis:
    base = dict(
        summary="Dave called about a furnace quote.",
        outcome="Quote owed by Friday.",
        sentiment="neutral",
        person=ExtractedPerson(full_name="Dave Mercer", name_confidence=0.9),
        facts=[],
        needs=[],
        tasks=[],
        appointments=[],
    )
    base.update(overrides)
    return ConversationAnalysis(**base)


# --------------------------------------------------------------------------- #
def test_summary_and_name_land_on_the_records(session):
    contact = resolve_contact(session, phone="6135551234")
    interaction = make_interaction(session, contact)
    apply_analysis(session, contact, interaction, analysis())

    assert contact.display_name == "Dave Mercer"
    assert interaction.summary.startswith("Dave called")
    assert interaction.outcome == "Quote owed by Friday."
    assert interaction.processed is True


def test_low_confidence_name_does_not_rename_the_contact(session):
    contact = resolve_contact(session, phone="6135551234")
    interaction = make_interaction(session, contact)
    apply_analysis(
        session, contact, interaction,
        analysis(person=ExtractedPerson(full_name="Dev Mercy", name_confidence=0.3)),
    )
    assert contact.display_name.startswith("Unknown")


def test_repeated_fact_is_confirmed_not_duplicated(session):
    contact = resolve_contact(session, phone="6135551234")
    fact = ExtractedFact(
        category="personal", text="Has a daughter starting at Carleton in September",
        confidence=0.8,
    )
    apply_analysis(session, contact, make_interaction(session, contact),
                   analysis(facts=[fact]))
    counts = apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(facts=[ExtractedFact(
            category="personal",
            text="Has a daughter starting at Carleton in september",
            confidence=0.9,
        )]),
    )
    rows = session.query(Fact).filter_by(contact_id=contact.id).all()
    assert len(rows) == 1
    assert counts["facts"] == 0 and counts["facts_confirmed"] == 1
    assert rows[0].confidence == 0.9  # confidence is upgraded, not averaged


def test_distinct_facts_both_stored(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(facts=[
            ExtractedFact(category="personal", text="Restores a 1972 Datsun"),
            ExtractedFact(category="preference", text="Prefers texts over email"),
        ]),
    )
    assert session.query(Fact).filter_by(contact_id=contact.id).count() == 2


def test_need_is_updated_in_place_and_can_resolve(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(needs=[ExtractedNeed(
            title="Furnace replacement quote",
            description="Wants a quote for a new furnace.", urgency="high",
        )]),
    )
    need = session.query(Need).filter_by(contact_id=contact.id).one()
    assert need.status == NeedStatus.open

    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(needs=[ExtractedNeed(
            title="Quote for furnace replacement",
            description="Quote sent and accepted.",
            resolved_in_this_conversation=True,
        )]),
    )
    assert session.query(Need).filter_by(contact_id=contact.id).count() == 1
    session.refresh(need)
    assert need.status == NeedStatus.resolved and need.resolved_at is not None


def test_task_owned_by_customer_is_flagged(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(tasks=[ExtractedTask(
            title="Send us the old furnace model number", owner="them",
        )]),
    )
    task = session.query(Task).filter_by(contact_id=contact.id).one()
    assert "customer committed" in task.detail


def test_duplicate_task_is_not_added_twice(session):
    contact = resolve_contact(session, phone="6135551234")
    for _ in range(2):
        apply_analysis(
            session, contact, make_interaction(session, contact),
            analysis(tasks=[ExtractedTask(title="Email the furnace quote")]),
        )
    assert session.query(Task).filter_by(contact_id=contact.id).count() == 1


def test_appointment_with_a_time_is_scheduled(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(appointments=[ExtractedAppointment(
            title="Site visit", starts_at_iso="2024-01-19T14:00:00",
            location="12 Elm St", tentative=False,
        )]),
    )
    appt = session.query(Appointment).filter_by(contact_id=contact.id).one()
    # 14:00 in America/Toronto is 19:00 UTC in January.
    assert appt.starts_at == dt.datetime(2024, 1, 19, 19, 0)
    assert appt.ends_at == dt.datetime(2024, 1, 19, 20, 0)
    assert appt.location == "12 Elm St"


def test_vague_appointment_becomes_a_task_instead(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(appointments=[ExtractedAppointment(title="Site visit sometime next week")]),
    )
    assert session.query(Appointment).filter_by(contact_id=contact.id).count() == 0
    task = session.query(Task).filter_by(contact_id=contact.id).one()
    assert task.title.startswith("Pin down a time")


def test_followup_suggestion_only_when_nothing_was_committed(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(followup_suggestion="Check whether the rebate deadline still applies"),
    )
    task = session.query(Task).filter_by(contact_id=contact.id).one()
    assert task.priority == "low"

    contact2 = resolve_contact(session, phone="6135559999")
    apply_analysis(
        session, contact2, make_interaction(session, contact2),
        analysis(
            tasks=[ExtractedTask(title="Send the quote")],
            followup_suggestion="Also mention the rebate",
        ),
    )
    titles = [t.title for t in session.query(Task).filter_by(contact_id=contact2.id)]
    assert titles == ["Send the quote"]


def test_contact_details_mentioned_on_the_call_are_attached(session):
    contact = resolve_contact(session, phone="6135551234")
    apply_analysis(
        session, contact, make_interaction(session, contact),
        analysis(person=ExtractedPerson(
            full_name="Dave Mercer", name_confidence=0.9,
            email="dave@mercerplumbing.example", company="Mercer Plumbing",
            address="12 Elm St, Ottawa",
        )),
    )
    assert "dave@mercerplumbing.example" in [e.address for e in contact.emails]
    assert contact.company == "Mercer Plumbing"
    assert contact.address == "12 Elm St, Ottawa"


def test_last_contacted_tracks_the_most_recent_conversation(session):
    contact = resolve_contact(session, phone="6135551234")
    old = make_interaction(session, contact, dt.datetime(2024, 1, 1, 9, 0))
    new = make_interaction(session, contact, dt.datetime(2024, 3, 1, 9, 0))
    apply_analysis(session, contact, new, analysis())
    apply_analysis(session, contact, old, analysis())
    assert contact.last_contacted_at == dt.datetime(2024, 3, 1, 9, 0)
