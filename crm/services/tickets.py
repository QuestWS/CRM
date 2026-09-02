"""The bridge between conversations and open work orders.

Three jobs: mirror the shop's open tickets locally, work out which ticket a
call or email was about, and stage a note for the job log that a person then
approves.

Nothing here creates a job. A job id is a BiT invoice number, and BiT is never
integrated with - that rule belongs to the service tracker and it holds on this
side of the wire too.
"""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.integrations.servicetracker import (
    NotConfigured,
    ServiceTrackerError,
    get_client,
)
from crm.models import (
    Contact,
    Interaction,
    NoteStatus,
    ServiceTicket,
    TicketNote,
    utcnow,
)
from crm.services.identity import normalize_email, normalize_phone, resolve_contact

log = logging.getLogger(__name__)

OPEN_STATUSES = ("received", "work_underway", "work_finished")


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def sync_tickets(s: Session, *, status: str = "all") -> dict[str, int]:
    """Mirror the service tracker's jobs locally and link them to contacts."""
    client = get_client()
    jobs = client.list_jobs(status=status)

    seen: set[str] = set()
    created = updated = linked = 0

    for job in jobs:
        job_id = str(job.get("id") or "").strip()
        if not job_id:
            continue
        seen.add(job_id)

        phone = normalize_phone(job.get("customerPhone"))
        email = normalize_email(job.get("customerEmail"))
        name = (job.get("customerName") or "").strip() or None

        ticket = s.get(ServiceTicket, job_id)
        if ticket is None:
            ticket = ServiceTicket(id=job_id)
            s.add(ticket)
            created += 1
        else:
            updated += 1

        ticket.customer_name = name
        ticket.customer_phone = phone
        ticket.customer_email = email
        ticket.boat_info = job.get("boatInfo") or None
        ticket.work_requested = job.get("workRequested") or None
        ticket.status = job.get("status") or "received"
        ticket.status_label = job.get("statusLabel") or None
        ticket.is_open = ticket.status in OPEN_STATUSES or not job.get("paidAt")
        ticket.alert = job.get("alert") or None
        ticket.amount_due = job.get("amountDue")
        ticket.entry_count = int(job.get("entryCount") or 0)
        ticket.minutes_total = int(job.get("minutes") or job.get("minutesTotal") or 0)
        ticket.remote_created_at = job.get("createdAt")
        ticket.remote_updated_at = job.get("updatedAt")
        ticket.synced_at = utcnow()

        # Only create a contact when there is something to identify them by.
        # A job with a name and nothing else would otherwise spawn a contact
        # that no call or email could ever match.
        if ticket.contact_id is None and (phone or email):
            contact = resolve_contact(s, phone=phone, email=email, name=name)
            if contact:
                ticket.contact_id = contact.id
                linked += 1

    # A job that dropped out of an "all" listing is gone over there.
    if status == "all" and seen:
        for stale in s.scalars(select(ServiceTicket).where(ServiceTicket.id.notin_(seen))):
            stale.is_open = False

    s.flush()
    log.info(
        "service tracker sync: %d new, %d updated, %d newly linked", created, updated, linked
    )
    return {"created": created, "updated": updated, "linked": linked}


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def open_tickets_for(s: Session, contact: Contact | None) -> list[ServiceTicket]:
    """Open work orders belonging to this person.

    Matched on contact link first, then on the phone and email recorded against
    the job - a ticket created before the contact existed still finds them.
    """
    if contact is None:
        return []
    phones = [p.e164 for p in contact.phones]
    emails = [e.address for e in contact.emails]

    clauses = [ServiceTicket.contact_id == contact.id]
    if phones:
        clauses.append(ServiceTicket.customer_phone.in_(phones))
    if emails:
        clauses.append(ServiceTicket.customer_email.in_(emails))

    return list(
        s.scalars(
            select(ServiceTicket)
            .where(ServiceTicket.is_open.is_(True), or_(*clauses))
            .order_by(ServiceTicket.remote_updated_at.desc())
        )
    )


def tickets_context_block(s: Session, contact: Contact | None) -> str:
    """The open-tickets list handed to the analyst so it can attribute a call."""
    tickets = open_tickets_for(s, contact)
    if not tickets:
        return ""
    lines = [
        "Open work orders for this person in the shop. If the conversation was "
        "about one of these, reference it by the exact number shown.",
    ]
    for t in tickets:
        parts = [f"- {t.id} [{t.status_label or t.status}]"]
        if t.boat_info:
            parts.append(f"boat: {t.boat_info}")
        if t.work_requested:
            parts.append(f"asked for: {t.work_requested}")
        if t.alert:
            parts.append(f"ALERT on the job: {t.alert}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# staging notes
# --------------------------------------------------------------------------- #
def stage_notes(s: Session, interaction: Interaction, analysis) -> list[TicketNote]:
    """Turn the analysis's ticket updates into notes awaiting approval.

    Only tickets that actually exist and are open are accepted - the model is
    told to copy an id from the list it was given, and this is what enforces it.
    """
    staged: list[TicketNote] = []
    for update in getattr(analysis, "ticket_updates", []) or []:
        ticket = s.get(ServiceTicket, str(update.ticket_id).strip())
        if ticket is None or not ticket.is_open:
            log.info(
                "ignoring note for unknown or closed ticket %r on interaction %s",
                update.ticket_id, interaction.id,
            )
            continue

        existing = s.scalar(
            select(TicketNote).where(
                TicketNote.ticket_id == ticket.id,
                TicketNote.source_interaction_id == interaction.id,
            )
        )
        if existing:
            continue

        note = TicketNote(
            ticket_id=ticket.id,
            contact_id=interaction.contact_id,
            source_interaction_id=interaction.id,
            body=_format_note(interaction, update),
        )
        s.add(note)
        staged.append(note)

        # The first ticket mentioned is what this conversation was about.
        if interaction.ticket_id is None:
            interaction.ticket_id = ticket.id

    s.flush()
    return staged


def _format_note(interaction: Interaction, update) -> str:
    """What lands in the shop's job log.

    Says where it came from, because a mechanic reading it needs to know this
    is the customer's word relayed rather than something seen on the boat.
    """
    when = interaction.occurred_at.strftime("%d %b, %H:%M")
    origin = {
        "call": f"From a phone call on {when}",
        "email": f"From an email on {when}",
        "note": f"From a counter conversation on {when}",
    }.get(interaction.kind.value, f"From a {interaction.kind.value} on {when}")

    lines = [f"{origin}:", "", update.note.strip()]
    if update.changes_the_work:
        lines += ["", "This changes the work — needs re-keying into BiT."]
    if update.needs_writer_attention:
        lines += ["", "Needs someone in the office to action it."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# pushing
# --------------------------------------------------------------------------- #
def push_note(s: Session, note: TicketNote) -> TicketNote:
    """Send one staged note to the shop's job log."""
    client = get_client()
    try:
        client.add_writer_note(note.ticket_id, note.body)
    except ServiceTrackerError as exc:
        note.status = NoteStatus.failed
        note.error = str(exc)
        s.flush()
        raise

    note.status = NoteStatus.pushed
    note.pushed_at = utcnow()
    note.error = None
    s.flush()
    log.info("pushed note %s to ticket %s", note.id, note.ticket_id)
    return note


def pending_notes(s: Session, limit: int = 100) -> list[TicketNote]:
    return list(
        s.scalars(
            select(TicketNote)
            .where(TicketNote.status.in_([NoteStatus.pending, NoteStatus.failed]))
            .order_by(TicketNote.created_at)
            .limit(limit)
        )
    )


def push_all_pending(s: Session, limit: int = 50) -> dict[str, int]:
    """Only runs when SERVICETRACKER_AUTOPUSH is on. Off by default.

    The shop's rule is that nothing leaves on a timer; a staged note waits for
    a click unless somebody has deliberately said otherwise.
    """
    stats = {"pushed": 0, "failed": 0}
    if not settings.servicetracker_autopush:
        return stats
    for note in pending_notes(s, limit):
        try:
            push_note(s, note)
            stats["pushed"] += 1
        except (ServiceTrackerError, NotConfigured):
            stats["failed"] += 1
        s.commit()
    return stats
