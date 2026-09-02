"""Walk-in intake: the record button at the counter.

A customer stands at the desk describing what they want done. Somebody presses
record. What comes out is a draft work order plus everything the customer
mentioned about themselves, filed against their contact.

It stops at a draft on purpose. A job id in the service tracker *is* a BiT
invoice number, so a person creates the job in BiT and keys the draft in. The
CRM never invents a ticket.
"""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.ai import enrich
from crm.ai.client import AIUnavailable
from crm.ai.enrich import TranscriptTooLong
from crm.models import (
    CallCategory,
    Direction,
    IntakeStatus,
    Interaction,
    InteractionKind,
    Recording,
    RecordingStatus,
    WalkInIntake,
    utcnow,
)
from crm.services import profile as profile_service
from crm.services.identity import normalize_email, normalize_phone, resolve_contact
from crm.transcription.registry import transcribe_call

log = logging.getLogger(__name__)


def create_intake(
    s: Session, recording: Recording, taken_by: str | None = None
) -> WalkInIntake:
    intake = WalkInIntake(
        recording_id=recording.id,
        taken_by=(taken_by or "").strip() or None,
        status=IntakeStatus.transcribing,
    )
    s.add(intake)
    s.flush()
    return intake


def process_intake(s: Session, intake: WalkInIntake) -> WalkInIntake:
    """Transcribe the counter recording, then turn it into a draft."""
    recording = s.get(Recording, intake.recording_id) if intake.recording_id else None
    if recording is None:
        intake.status = IntakeStatus.failed
        intake.error = "The recording is missing."
        s.flush()
        return intake

    if not recording.transcript:
        path = Path(recording.path)
        if not path.is_file():
            intake.status = IntakeStatus.failed
            intake.error = f"Audio file missing: {recording.path}"
            recording.status = RecordingStatus.failed
            recording.error = intake.error
            s.flush()
            return intake
        try:
            # No operator/caller split at a counter: one microphone, two or
            # more people around it. Diarisation, not channel splitting.
            transcript = transcribe_call(path, caller_label="Customer")
        except Exception as exc:
            intake.status = IntakeStatus.failed
            intake.error = f"Transcription failed: {exc}"
            recording.status = RecordingStatus.failed
            recording.error = intake.error
            s.flush()
            log.exception("intake %s transcription failed", intake.id)
            return intake

        recording.transcript = transcript.as_dialogue() or transcript.text
        recording.transcript_engine = transcript.engine
        recording.transcript_segments = [seg.as_dict() for seg in transcript.segments]
        recording.duration_seconds = transcript.duration_seconds
        recording.status = RecordingStatus.transcribed
        s.flush()

    intake.transcript = recording.transcript
    if not (intake.transcript or "").strip():
        intake.status = IntakeStatus.failed
        intake.error = "Nothing audible on the recording."
        recording.status = RecordingStatus.skipped
        s.flush()
        return intake

    # The transcript is saved before the model is asked for anything. A
    # customer standing at the counter cannot be replayed.
    s.commit()

    try:
        extraction = enrich.extract_walk_in(
            transcript=intake.transcript,
            taken_at=intake.created_at,
            known_context=_known_context(s, intake),
        )
    except (AIUnavailable, TranscriptTooLong) as exc:
        intake.status = IntakeStatus.failed
        intake.error = str(exc)
        s.flush()
        log.warning("intake %s not extracted: %s", intake.id, exc)
        return intake

    _apply(s, intake, extraction, recording)
    return intake


def _known_context(s: Session, intake: WalkInIntake) -> str:
    if intake.contact_id is None:
        return ""
    from crm.models import Contact

    return profile_service.known_context_block(s, s.get(Contact, intake.contact_id))


def _apply(
    s: Session, intake: WalkInIntake, extraction, recording: Recording
) -> WalkInIntake:
    phone = normalize_phone(extraction.customer_phone)
    email = normalize_email(extraction.customer_email)

    intake.customer_name = extraction.customer_name
    intake.customer_phone = phone
    intake.customer_email = email
    intake.boat_info = extraction.boat_info
    intake.work_requested = extraction.work_requested
    intake.requested_items = [item.model_dump() for item in extraction.requested_items]
    intake.urgency = extraction.urgency
    intake.promised_date = extraction.promised_date
    intake.customer_said = "\n".join(extraction.customer_said) or None
    intake.open_questions = list(extraction.open_questions)

    # A counter conversation is an interaction like any other, so it lands on
    # the timeline and feeds the person's profile.
    contact = None
    if phone or email or extraction.customer_name:
        contact = resolve_contact(
            s, phone=phone, email=email, name=extraction.customer_name
        )

    if contact:
        intake.contact_id = contact.id
        interaction = Interaction(
            contact_id=contact.id,
            kind=InteractionKind.note,
            direction=Direction.internal,
            occurred_at=intake.created_at,
            duration_seconds=int(recording.duration_seconds or 0) or None,
            subject=f"Boat drop-off — {extraction.boat_info or 'walk-in'}",
            body=intake.transcript,
            summary=extraction.work_requested,
            outcome="Drafted at the counter; not yet written up in BiT.",
            category=CallCategory.service,
            category_confidence=1.0,
            source="walk_in",
            source_ref=recording.sha256,
            meta={"intake_id": intake.id, "recording_id": recording.id},
            processed=True,
        )
        s.add(interaction)
        s.flush()
        intake.interaction_id = interaction.id
        recording.interaction_id = interaction.id

        if not contact.last_contacted_at or contact.last_contacted_at < intake.created_at:
            contact.last_contacted_at = intake.created_at

        # Personal details from the counter are worth the same as ones from a
        # call - that is the whole "get to know the person" half.
        for said in extraction.customer_said:
            _add_fact(s, contact, interaction, said)
        profile_service.refresh_profile(s, contact)

    intake.status = IntakeStatus.draft
    intake.error = None
    recording.status = RecordingStatus.done
    s.flush()
    log.info("intake %s drafted (contact=%s)", intake.id, intake.contact_id)
    return intake


def _add_fact(s: Session, contact, interaction: Interaction, text: str) -> None:
    from crm.models import Fact, FactCategory
    from crm.services.profile import FACT_MATCH_RATIO, _similar

    existing = s.scalars(
        select(Fact).where(Fact.contact_id == contact.id, Fact.archived.is_(False))
    ).all()
    for row in existing:
        if _similar(row.text, text) >= FACT_MATCH_RATIO:
            row.last_confirmed_at = utcnow()
            return
    s.add(
        Fact(
            contact_id=contact.id,
            category=FactCategory.personal,
            text=text,
            confidence=0.75,
            source_interaction_id=interaction.id,
        )
    )


def work_order_text(intake: WalkInIntake) -> str:
    """The block a service writer copies into BiT."""
    lines: list[str] = []
    if intake.customer_name:
        lines.append(f"Customer: {intake.customer_name}")
    if intake.customer_phone:
        lines.append(f"Phone: {intake.customer_phone}")
    if intake.customer_email:
        lines.append(f"Email: {intake.customer_email}")
    if intake.boat_info:
        lines.append(f"Boat: {intake.boat_info}")
    if intake.promised_date:
        lines.append(f"Promised: {intake.promised_date}")
    if lines:
        lines.append("")

    lines.append("Work requested:")
    items = intake.requested_items or []
    if items:
        for item in items:
            line = f"- {item.get('description', '')}".rstrip()
            if item.get("urgency") and item["urgency"] != "normal":
                line += f" [{item['urgency']}]"
            lines.append(line)
            if item.get("customer_words"):
                lines.append(f'  Customer: "{item["customer_words"]}"')
    else:
        lines.append(intake.work_requested or "(nothing captured)")
    return "\n".join(lines)


def recent_intakes(s: Session, limit: int = 50) -> list[WalkInIntake]:
    return list(
        s.scalars(
            select(WalkInIntake)
            .where(WalkInIntake.status != IntakeStatus.discarded)
            .order_by(WalkInIntake.created_at.desc())
            .limit(limit)
        )
    )


def pending_intakes(s: Session, limit: int = 20) -> list[WalkInIntake]:
    return list(
        s.scalars(
            select(WalkInIntake)
            .where(WalkInIntake.status == IntakeStatus.transcribing)
            .order_by(WalkInIntake.created_at)
            .limit(limit)
        )
    )


def run_pending(s: Session, limit: int = 10) -> dict[str, int]:
    stats = {"drafted": 0, "failed": 0}
    for intake in pending_intakes(s, limit):
        try:
            result = process_intake(s, intake)
            stats["drafted" if result.status == IntakeStatus.draft else "failed"] += 1
        except Exception:
            stats["failed"] += 1
            log.exception("unexpected error processing intake %s", intake.id)
        s.commit()
    return stats


__all__ = [
    "create_intake",
    "pending_intakes",
    "process_intake",
    "recent_intakes",
    "run_pending",
    "work_order_text",
]
