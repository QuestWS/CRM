"""Counter intake: recording to draft work order. Model and ASR stubbed."""
from __future__ import annotations

import wave
from pathlib import Path

import pytest

from crm.ai.schemas import RequestedWork, WalkInIntakeExtraction
from crm.models import Contact, Direction, Fact, IntakeStatus, Interaction
from crm.services import intake as intake_service
from crm.transcription.base import Transcript

TRANSCRIPT = (
    "Speaker A: Hi, Dave Mercer, I called yesterday. Dropping the Malibu off.\n"
    "Speaker B: The 2014 Wakesetter?\n"
    "Speaker A: That's it. It's grinding when I put it in reverse, and I want it "
    "winterized while it's here. My daughter's wedding is the 20th so I need it "
    "back before then. You can reach me at 815-555-1234."
)

EXTRACTION = WalkInIntakeExtraction(
    customer_name="Dave Mercer",
    customer_phone="815-555-1234",
    boat_info="2014 Malibu Wakesetter 22",
    requested_items=[
        RequestedWork(
            description="Diagnose grinding noise in reverse",
            customer_words="It's grinding when I put it in reverse",
            urgency="high",
        ),
        RequestedWork(description="Winterize"),
    ],
    work_requested="Diagnose a grinding noise in reverse, and winterize.",
    urgency="high",
    promised_date="Before the 20th",
    customer_said=["His daughter's wedding is on the 20th"],
    open_questions=["No spending limit agreed before teardown"],
)


@pytest.fixture
def wav_file(tmp_path) -> Path:
    path = tmp_path / "walkin-20240903-101500.wav"
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\x00\x00" * 8000)
    return path


@pytest.fixture
def stubbed(monkeypatch):
    from crm.ai import enrich
    from crm.services import profile

    monkeypatch.setattr(
        intake_service, "transcribe_call",
        lambda path, **kw: Transcript(text=TRANSCRIPT, engine="stub", duration_seconds=95.0),
    )
    monkeypatch.setattr(enrich, "extract_walk_in", lambda **kw: EXTRACTION)
    monkeypatch.setattr(profile, "refresh_profile", lambda s, c: True)


def run(session, wav_file, taken_by="Sam"):
    from crm.ingest.sangoma import register_recording

    recording, _ = register_recording(session, wav_file, direction=Direction.internal)
    record = intake_service.create_intake(session, recording, taken_by=taken_by)
    return intake_service.process_intake(session, record)


# --------------------------------------------------------------------------- #
def test_recording_becomes_a_draft(session, wav_file, stubbed):
    record = run(session, wav_file)
    assert record.status == IntakeStatus.draft
    assert record.customer_name == "Dave Mercer"
    assert record.customer_phone == "+18155551234"     # normalised
    assert record.boat_info == "2014 Malibu Wakesetter 22"
    assert record.taken_by == "Sam"
    assert record.open_questions == ["No spending limit agreed before teardown"]


def test_draft_creates_the_contact_and_a_timeline_entry(session, wav_file, stubbed):
    record = run(session, wav_file)
    contact = session.get(Contact, record.contact_id)
    assert contact.display_name == "Dave Mercer"
    assert "+18155551234" in [p.e164 for p in contact.phones]

    interaction = session.get(Interaction, record.interaction_id)
    assert interaction.source == "walk_in"
    assert interaction.direction == Direction.internal
    assert interaction.body == TRANSCRIPT


def test_personal_details_are_kept_as_facts(session, wav_file, stubbed):
    """The 'know the person' half works at the counter too."""
    record = run(session, wav_file)
    facts = session.query(Fact).filter_by(contact_id=record.contact_id).all()
    assert [f.text for f in facts] == ["His daughter's wedding is on the 20th"]


def test_work_order_text_is_copy_pasteable(session, wav_file, stubbed):
    text = intake_service.work_order_text(run(session, wav_file))
    assert "Customer: Dave Mercer" in text
    assert "Boat: 2014 Malibu Wakesetter 22" in text
    assert "- Diagnose grinding noise in reverse [high]" in text
    assert '"It\'s grinding when I put it in reverse"' in text
    assert "- Winterize" in text


def test_it_never_invents_a_work_order_number(session, wav_file, stubbed):
    """A job id is a BiT invoice number. The CRM must not make one up."""
    record = run(session, wav_file)
    assert record.linked_ticket_id is None
    assert record.status == IntakeStatus.draft


def test_transcript_survives_an_extraction_failure(session, wav_file, monkeypatch):
    from crm.ai import enrich
    from crm.ai.client import AIUnavailable
    from crm.models import Recording

    monkeypatch.setattr(
        intake_service, "transcribe_call",
        lambda path, **kw: Transcript(text=TRANSCRIPT, engine="stub"),
    )

    def boom(**kwargs):
        raise AIUnavailable("no API key")

    monkeypatch.setattr(enrich, "extract_walk_in", boom)

    record = run(session, wav_file)
    assert record.status == IntakeStatus.failed
    assert "no API key" in record.error
    # The customer cannot be asked to repeat themselves, so the words are kept.
    assert record.transcript == TRANSCRIPT
    assert session.get(Recording, record.recording_id).transcript == TRANSCRIPT


def test_silent_recording_fails_cleanly(session, wav_file, monkeypatch):
    monkeypatch.setattr(
        intake_service, "transcribe_call",
        lambda path, **kw: Transcript(text="  ", engine="stub"),
    )
    record = run(session, wav_file)
    assert record.status == IntakeStatus.failed
    assert "audible" in record.error
