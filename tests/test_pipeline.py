"""End to end: an audio file becomes a contact with a need, a task and a profile.

Transcription and the model are stubbed - everything between them is real.
"""
from __future__ import annotations

import datetime as dt
import wave
from pathlib import Path

import pytest

from crm.ai.schemas import (
    ConversationAnalysis,
    ExtractedFact,
    ExtractedNeed,
    ExtractedPerson,
    ExtractedTask,
    ProfileNarrative,
)
from crm.models import Contact, Fact, Interaction, Need, RecordingStatus, Task
from crm.transcription.base import Segment, Transcript


@pytest.fixture
def wav_file(tmp_path) -> Path:
    """A real (silent) wav named the way FreePBX names them."""
    path = tmp_path / "in-201-6135551234-20240115-143022-1705345822.14.wav"
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(8000)
        fh.writeframes(b"\x00\x00" * 8000)
    return path


TRANSCRIPT = (
    "Caller: Hi, it's Dave Mercer over at Mercer Plumbing. My furnace is making a "
    "racket and I need someone out to look at it.\n"
    "Dana Quest: I can do Friday at two.\n"
    "Caller: Friday works. My daughter starts at Carleton next month so I'd like it "
    "sorted before then.\n"
    "Dana Quest: I'll email you the quote Thursday."
)

ANALYSIS = ConversationAnalysis(
    summary="Dave Mercer called about a noisy furnace and booked a Friday visit.",
    outcome="Site visit Friday 2pm; quote owed Thursday.",
    sentiment="neutral",
    person=ExtractedPerson(
        full_name="Dave Mercer", first_name="Dave", last_name="Mercer",
        company="Mercer Plumbing", name_confidence=0.95,
    ),
    facts=[
        ExtractedFact(
            category="personal",
            text="His daughter starts at Carleton next month",
            quote="My daughter starts at Carleton next month",
            confidence=0.9,
        )
    ],
    needs=[ExtractedNeed(
        title="Noisy furnace diagnosis",
        description="Furnace making a loud noise; wants it looked at.",
        urgency="high",
    )],
    tasks=[ExtractedTask(
        title="Email Dave the furnace quote", owner="me",
        due_hint="Thursday", due_at_iso="2024-01-18T09:00:00",
    )],
    appointments=[],
)


@pytest.fixture
def stubbed(monkeypatch):
    from crm.ai import enrich
    from crm.services import pipeline, profile

    def fake_transcribe(path, **kwargs):
        # Shaped like a real stereo-split result: raw text per segment, with the
        # speaker carried separately. as_dialogue() reassembles the labels.
        segments = []
        for index, line in enumerate(TRANSCRIPT.split("\n")):
            speaker, _, text = line.partition(": ")
            segments.append(Segment(index * 10.0, index * 10.0 + 9.0, text, speaker))
        return Transcript(
            text=" ".join(s.text for s in segments),
            segments=segments,
            engine="stub",
            duration_seconds=124.0,
        )

    monkeypatch.setattr(pipeline, "transcribe_call", fake_transcribe)
    monkeypatch.setattr(enrich, "analyze_conversation", lambda **kw: ANALYSIS)
    monkeypatch.setattr(
        enrich, "write_profile",
        lambda **kw: ProfileNarrative(
            profile="Dave runs Mercer Plumbing and has a daughter heading to Carleton.",
            talking_points=["Ask how the Carleton move went"],
            watch_outs=[],
        ),
    )
    monkeypatch.setattr(profile.enrich, "write_profile", lambda **kw: ProfileNarrative(
        profile="Dave runs Mercer Plumbing and has a daughter heading to Carleton.",
        talking_points=["Ask how the Carleton move went"],
        watch_outs=[],
    ))


def test_recording_to_profile(session, wav_file, stubbed):
    from crm.ingest.sangoma import register_recording
    from crm.services.pipeline import process_recording

    rec, created = register_recording(session, wav_file)
    assert created
    # Metadata came out of the filename alone.
    assert rec.from_number == "+16135551234"
    assert rec.call_started_at == dt.datetime(2024, 1, 15, 14, 30, 22)

    interaction = process_recording(session, rec)

    assert rec.status == RecordingStatus.done
    assert rec.transcript == TRANSCRIPT
    assert interaction.summary.startswith("Dave Mercer called")

    contact = session.get(Contact, interaction.contact_id)
    assert contact.display_name == "Dave Mercer"
    assert contact.company == "Mercer Plumbing"
    assert "+16135551234" in [p.e164 for p in contact.phones]

    assert session.query(Need).filter_by(contact_id=contact.id).one().urgency == "high"
    fact = session.query(Fact).filter_by(contact_id=contact.id).one()
    assert fact.category.value == "personal"
    task = session.query(Task).filter_by(contact_id=contact.id).one()
    assert task.title.startswith("Email Dave")
    assert "Carleton" in contact.ai_profile
    assert "Ask how the Carleton move went" in contact.ai_profile


def test_reimporting_the_same_file_is_a_no_op(session, wav_file, stubbed):
    from crm.ingest.sangoma import register_recording
    from crm.services.pipeline import process_recording

    rec, created = register_recording(session, wav_file)
    process_recording(session, rec)

    again, created_again = register_recording(session, wav_file)
    assert not created_again and again.id == rec.id
    assert session.query(Interaction).count() == 1


def test_analysis_failure_keeps_the_transcript(session, wav_file, monkeypatch):
    """The transcript is unrepeatable; the analysis is cheap. Never lose the transcript."""
    from crm.ai import enrich
    from crm.ai.client import AIUnavailable
    from crm.ingest.sangoma import register_recording
    from crm.services import pipeline
    from crm.services.pipeline import PipelineError, process_recording

    monkeypatch.setattr(
        pipeline, "transcribe_call",
        lambda path, **kw: Transcript(text=TRANSCRIPT, engine="stub"),
    )

    def boom(**kwargs):
        raise AIUnavailable("no API key")

    monkeypatch.setattr(enrich, "analyze_conversation", boom)

    rec, _ = register_recording(session, wav_file)
    with pytest.raises(PipelineError):
        process_recording(session, rec)

    assert rec.transcript == TRANSCRIPT
    # Left at 'transcribed' so a later run retries the analysis, not the ASR.
    assert rec.status == RecordingStatus.transcribed
    assert "no API key" in rec.error


def test_missing_audio_file_fails_cleanly(session, tmp_path, stubbed):
    from crm.ingest.sangoma import register_recording
    from crm.services.pipeline import PipelineError, process_recording

    path = tmp_path / "gone.wav"
    path.write_bytes(b"RIFF0000WAVE")
    rec, _ = register_recording(session, path, copy=False)
    path.unlink()

    with pytest.raises(PipelineError):
        process_recording(session, rec)
    assert rec.status == RecordingStatus.failed
    assert "missing" in rec.error


def test_empty_transcript_is_skipped_not_analysed(session, wav_file, monkeypatch):
    from crm.ingest.sangoma import register_recording
    from crm.services import pipeline
    from crm.services.pipeline import PipelineError, process_recording

    monkeypatch.setattr(
        pipeline, "transcribe_call", lambda path, **kw: Transcript(text="  ", engine="stub")
    )
    rec, _ = register_recording(session, wav_file)
    with pytest.raises(PipelineError):
        process_recording(session, rec)
    assert rec.status == RecordingStatus.skipped
