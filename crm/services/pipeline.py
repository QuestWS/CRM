"""The processing loop: recording -> transcript -> analysis -> CRM records."""
from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.ai import enrich
from crm.ai.client import AIUnavailable
from crm.ai.enrich import TranscriptTooLong
from crm.config import settings
from crm.ingest.sangoma import counterparty
from crm.models import (
    Contact,
    Direction,
    Interaction,
    InteractionKind,
    Recording,
    RecordingStatus,
    utcnow,
)
from crm.services import profile as profile_service
from crm.services import tickets as ticket_service
from crm.services.identity import resolve_contact
from crm.transcription.registry import transcribe_call

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


class PipelineError(RuntimeError):
    pass


def _context_with_tickets(s: Session, contact) -> str:
    """Everything on file, plus the person's open work orders in the shop."""
    parts = [profile_service.known_context_block(s, contact)]
    tickets = ticket_service.tickets_context_block(s, contact)
    if tickets:
        parts.append(tickets)
    return "\n\n".join(p for p in parts if p)


def _stage_ticket_notes(s: Session, interaction: Interaction, analysis) -> None:
    """Never let a service-tracker problem cost us a completed analysis."""
    try:
        ticket_service.stage_notes(s, interaction, analysis)
    except Exception:
        log.exception("could not stage ticket notes for interaction %s", interaction.id)


# --------------------------------------------------------------------------- #
def transcribe_recording(s: Session, rec: Recording) -> Recording:
    """Run ASR for one recording. Safe to call again on a failed row."""
    if rec.transcript:
        return rec

    path = Path(rec.path)
    if not path.is_file():
        rec.status = RecordingStatus.failed
        rec.error = f"Audio file missing: {rec.path}"
        s.flush()
        raise PipelineError(rec.error)

    rec.status = RecordingStatus.transcribing
    rec.attempts += 1
    s.flush()

    try:
        transcript = transcribe_call(path)
    except Exception as exc:  # engine failures are varied; record and move on
        rec.status = RecordingStatus.failed
        rec.error = f"Transcription failed: {exc}"
        s.flush()
        log.exception("transcription failed for recording %s", rec.id)
        raise PipelineError(rec.error) from exc

    rec.transcript = transcript.as_dialogue() or transcript.text
    rec.transcript_engine = transcript.engine
    rec.transcript_segments = [seg.as_dict() for seg in transcript.segments]
    rec.duration_seconds = transcript.duration_seconds or rec.duration_seconds
    rec.status = RecordingStatus.transcribed
    rec.error = None
    s.flush()
    log.info("transcribed recording %s (%s chars)", rec.id, len(rec.transcript or ""))
    return rec


def _ensure_interaction(s: Session, rec: Recording) -> tuple[Interaction, Contact]:
    """Attach the recording to a contact and an interaction row."""
    number = counterparty(rec)
    contact = resolve_contact(s, phone=number)
    assert contact is not None  # create=True always yields a contact

    if rec.interaction_id:
        interaction = s.get(Interaction, rec.interaction_id)
        if interaction is not None:
            interaction.contact_id = contact.id
            return interaction, contact

    interaction = Interaction(
        contact_id=contact.id,
        kind=InteractionKind.call,
        direction=rec.direction or Direction.unknown,
        occurred_at=rec.call_started_at or rec.received_at or utcnow(),
        duration_seconds=int(rec.duration_seconds) if rec.duration_seconds else None,
        subject=f"Call with {contact.display_name}",
        body=rec.transcript,
        source="sangoma",
        source_ref=rec.sha256,
        meta={
            "from": rec.from_number,
            "to": rec.to_number,
            "pbx_call_id": rec.pbx_call_id,
            "recording_id": rec.id,
            "consent_announced": rec.consent_announced,
            "transcript_engine": rec.transcript_engine,
        },
    )
    s.add(interaction)
    s.flush()
    rec.interaction_id = interaction.id
    s.flush()
    return interaction, contact


def analyze_recording(s: Session, rec: Recording) -> Interaction:
    """Extract CRM records from an already-transcribed recording."""
    if not rec.transcript or not rec.transcript.strip():
        rec.status = RecordingStatus.skipped
        rec.error = "Empty transcript - the call had no usable speech."
        s.flush()
        raise PipelineError(rec.error)

    interaction, contact = _ensure_interaction(s, rec)
    interaction.body = rec.transcript
    rec.status = RecordingStatus.analyzing
    s.flush()

    try:
        analysis = enrich.analyze_conversation(
            body=rec.transcript,
            kind="phone call",
            direction=(rec.direction or Direction.unknown).value,
            occurred_at=interaction.occurred_at,
            known_context=_context_with_tickets(s, contact),
            from_number=rec.from_number,
            to_number=rec.to_number,
            duration_seconds=interaction.duration_seconds,
        )
    except (AIUnavailable, TranscriptTooLong) as exc:
        # The transcript is the valuable part and it is already saved. Leave the
        # row transcribed so a later run can analyse it once the AI is reachable.
        rec.status = RecordingStatus.transcribed
        rec.error = str(exc)
        s.flush()
        raise PipelineError(str(exc)) from exc

    profile_service.apply_analysis(s, contact, interaction, analysis)
    _stage_ticket_notes(s, interaction, analysis)
    profile_service.refresh_profile(s, contact)

    rec.status = RecordingStatus.done
    rec.error = None
    s.flush()
    log.info("analysed recording %s for contact %s", rec.id, contact.id)
    return interaction


def process_recording(s: Session, rec: Recording) -> Interaction | None:
    transcribe_recording(s, rec)
    return analyze_recording(s, rec)


def pending_recordings(s: Session, limit: int = 20) -> list[Recording]:
    return list(
        s.scalars(
            select(Recording)
            .where(
                Recording.status.in_(
                    [
                        RecordingStatus.pending,
                        RecordingStatus.transcribing,
                        RecordingStatus.transcribed,
                        RecordingStatus.analyzing,
                    ]
                ),
                Recording.attempts < MAX_ATTEMPTS,
            )
            .order_by(Recording.received_at)
            .limit(limit)
        )
    )


def run_pending(s: Session, limit: int = 20) -> dict[str, int]:
    """Drive every queued recording as far as it will go. Never raises."""
    stats = {"processed": 0, "failed": 0}
    for rec in pending_recordings(s, limit):
        try:
            process_recording(s, rec)
            stats["processed"] += 1
        except PipelineError as exc:
            stats["failed"] += 1
            log.warning("recording %s stalled: %s", rec.id, exc)
        except Exception:
            stats["failed"] += 1
            log.exception("unexpected error processing recording %s", rec.id)
        s.commit()
    return stats


# --------------------------------------------------------------------------- #
def process_email_interaction(s: Session, interaction: Interaction) -> Interaction:
    """Run the same extraction over an email that arrived from the Gmail sync."""
    if not interaction.body or not interaction.body.strip():
        interaction.processed = True
        s.flush()
        return interaction

    contact = s.get(Contact, interaction.contact_id) if interaction.contact_id else None
    if contact is None:
        interaction.processed = True
        s.flush()
        return interaction

    analysis = enrich.analyze_conversation(
        body=interaction.body,
        kind="email",
        direction=interaction.direction.value,
        occurred_at=interaction.occurred_at,
        subject=interaction.subject,
        known_context=_context_with_tickets(s, contact),
    )
    profile_service.apply_analysis(s, contact, interaction, analysis)
    _stage_ticket_notes(s, interaction, analysis)
    profile_service.refresh_profile(s, contact)
    return interaction


def run_unprocessed_emails(s: Session, limit: int = 25) -> dict[str, int]:
    stats = {"processed": 0, "failed": 0}
    rows = s.scalars(
        select(Interaction)
        .where(
            Interaction.kind == InteractionKind.email,
            Interaction.processed.is_(False),
            Interaction.contact_id.is_not(None),
        )
        .order_by(Interaction.occurred_at.desc())
        .limit(limit)
    ).all()
    for interaction in rows:
        try:
            process_email_interaction(s, interaction)
            stats["processed"] += 1
        except (AIUnavailable, TranscriptTooLong) as exc:
            stats["failed"] += 1
            log.warning("email %s not analysed: %s", interaction.id, exc)
        except Exception:
            stats["failed"] += 1
            log.exception("unexpected error processing email %s", interaction.id)
        s.commit()
    return stats


def import_and_process(s: Session, directory: Path | None = None) -> dict[str, int]:
    """One pass of the whole call path: scan the folder, then drain the queue."""
    from crm.ingest.sangoma import import_directory

    imported = 0
    if directory or settings.sangoma_watch_dir:
        try:
            imported = len(import_directory(s, directory))
            s.commit()
        except NotADirectoryError as exc:
            log.warning("skipping directory import: %s", exc)

    stats = run_pending(s)
    stats["imported"] = imported
    return stats


__all__ = [
    "PipelineError",
    "analyze_recording",
    "import_and_process",
    "process_email_interaction",
    "process_recording",
    "run_pending",
    "run_unprocessed_emails",
    "transcribe_recording",
]
