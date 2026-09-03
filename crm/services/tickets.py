"""The bridge between conversations and open work.

Two shop apps own work items, and this mirrors both:

* **servicetracker** - repair jobs. Notes go on the job log as `writer_note`.
* **winter-quotes** - winter services quotes. Notes go on the quote's staff
  note, which is staff-side only.

Three jobs either way: mirror what is open, work out which item a call or email
was about, and stage a note that a person then approves.

Nothing here creates work. A service-tracker job id is a BiT invoice number and
BiT is never integrated with; a winter quote is created by the customer on the
quote page. Both rules belong to those repos and hold on this side of the wire.
"""
from __future__ import annotations

import logging

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.integrations import servicetracker, winterquotes
from crm.integrations.servicetracker import NotConfigured, ServiceTrackerError
from crm.integrations.winterquotes import WinterQuotesError
from crm.models import (
    WORK_ORDER_CATEGORIES,
    CallCategory,
    Contact,
    Interaction,
    NoteStatus,
    TicketNote,
    WorkItem,
    WorkSystem,
    utcnow,
)
from crm.services.identity import normalize_email, normalize_phone, resolve_contact

log = logging.getLogger(__name__)

OPEN_STATUSES = ("received", "work_underway", "work_finished")

# Filling in a winter quote's phone and email costs one call each, so a first
# sync spreads the backfill over several runs instead of stalling on it.
WINTER_LOOKUPS_PER_SYNC = 25


def work_item_id(system: WorkSystem, remote_id: str) -> str:
    """Namespaced key. The two apps number independently."""
    return f"{system.value}:{str(remote_id).strip()}"


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def sync_tickets(s: Session, *, status: str = "all") -> dict[str, int]:
    """Mirror the service tracker's jobs locally and link them to contacts."""
    client = servicetracker.get_client()
    jobs = client.list_jobs(status=status)

    seen: set[str] = set()
    created = updated = linked = 0

    for job in jobs:
        remote_id = str(job.get("id") or "").strip()
        if not remote_id:
            continue
        job_id = work_item_id(WorkSystem.servicetracker, remote_id)
        seen.add(job_id)

        phone = normalize_phone(job.get("customerPhone"))
        email = normalize_email(job.get("customerEmail"))
        name = (job.get("customerName") or "").strip() or None

        ticket = s.get(WorkItem, job_id)
        if ticket is None:
            ticket = WorkItem(
                id=job_id, system=WorkSystem.servicetracker, remote_id=remote_id
            )
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

    # A job that dropped out of an "all" listing is gone over there. Scoped to
    # this system so a winter quote is never closed by a service-tracker sync.
    if status == "all" and seen:
        stale_rows = s.scalars(
            select(WorkItem).where(
                WorkItem.system == WorkSystem.servicetracker,
                WorkItem.id.notin_(seen),
            )
        )
        for stale in stale_rows:
            stale.is_open = False

    s.flush()
    log.info(
        "service tracker sync: %d new, %d updated, %d newly linked", created, updated, linked
    )
    return {"created": created, "updated": updated, "linked": linked}


def sync_winter(s: Session) -> dict[str, int]:
    """Mirror the winter services quotes and link them to contacts.

    `storageView` is one call for the whole season but carries no phone or
    email, so contact details are filled in with per-quote lookups, bounded per
    run. A first sync therefore links progressively over a few passes rather
    than hammering Apps Script in one.
    """
    client = winterquotes.get_client()
    rows = client.storage_view()

    seen: set[str] = set()
    created = updated = linked = 0
    lookups = 0

    for row in rows:
        remote_id = str(row.get("qn") or "").strip()
        if not remote_id:
            continue
        item_id = work_item_id(WorkSystem.winter, remote_id)
        seen.add(item_id)

        item = s.get(WorkItem, item_id)
        if item is None:
            item = WorkItem(id=item_id, system=WorkSystem.winter, remote_id=remote_id)
            s.add(item)
            created += 1
        else:
            updated += 1

        # storageView gives "Last, First"; the CRM wants it read as a name.
        raw_name = str(row.get("name") or "").strip()
        if "," in raw_name:
            last, _, first = raw_name.partition(",")
            raw_name = f"{first.strip()} {last.strip()}".strip()
        item.customer_name = raw_name or None

        unit = str(row.get("unit") or "").strip()
        ymm = str(row.get("ymm") or "").strip()
        item.boat_info = " ".join(x for x in (ymm, unit) if x) or None
        item.storage_location = str(row.get("storage") or "").strip() or None
        item.season_done = str(row.get("seasonDone") or "").strip() or None
        item.balance = str(row.get("balance") or "").strip() or None
        item.status = str(row.get("status") or "").strip() or "quoted"
        item.status_label = item.status
        # A quote stays open until the season is closed out on it. Balance is
        # not the test: a paid boat still in the yard is very much open.
        item.is_open = not item.season_done
        item.synced_at = utcnow()

        # Contact details need a per-quote lookup. Only for open quotes we
        # cannot already match on, and only a bounded number per run.
        needs_contact = item.is_open and not (item.customer_phone or item.customer_email)
        if needs_contact and lookups < WINTER_LOOKUPS_PER_SYNC:
            lookups += 1
            try:
                detail = client.lookup(remote_id)
            except WinterQuotesError as exc:
                log.warning("winter lookup failed for %s: %s", remote_id, exc)
                detail = {}
            item.customer_phone = normalize_phone(detail.get("phone")) or None
            item.customer_email = normalize_email(detail.get("email")) or None
            requested = detail.get("rqList") or []
            if requested:
                item.work_requested = "; ".join(str(x) for x in requested)

        if item.contact_id is None and (item.customer_phone or item.customer_email):
            contact = resolve_contact(
                s,
                phone=item.customer_phone,
                email=item.customer_email,
                name=item.customer_name,
            )
            if contact:
                item.contact_id = contact.id
                linked += 1

    if seen:
        stale_rows = s.scalars(
            select(WorkItem).where(
                WorkItem.system == WorkSystem.winter, WorkItem.id.notin_(seen)
            )
        )
        for stale in stale_rows:
            stale.is_open = False

    s.flush()
    log.info(
        "winter sync: %d new, %d updated, %d newly linked, %d lookup(s)",
        created, updated, linked, lookups,
    )
    return {"created": created, "updated": updated, "linked": linked, "lookups": lookups}


def sync_all(s: Session) -> dict[str, dict]:
    """Both systems. Neither failing stops the other."""
    out: dict[str, dict] = {}
    if settings.servicetracker_configured:
        try:
            out["servicetracker"] = sync_tickets(s)
        except (ServiceTrackerError, NotConfigured) as exc:
            log.warning("service tracker sync failed: %s", exc)
            out["servicetracker"] = {"error": str(exc)}
    if settings.winter_configured:
        try:
            out["winter"] = sync_winter(s)
        except WinterQuotesError as exc:
            log.warning("winter sync failed: %s", exc)
            out["winter"] = {"error": str(exc)}
    return out


# --------------------------------------------------------------------------- #
# matching
# --------------------------------------------------------------------------- #
def open_tickets_for(s: Session, contact: Contact | None) -> list[WorkItem]:
    """Open work orders belonging to this person.

    Matched on contact link first, then on the phone and email recorded against
    the job - a ticket created before the contact existed still finds them.
    """
    if contact is None:
        return []
    phones = [p.e164 for p in contact.phones]
    emails = [e.address for e in contact.emails]

    clauses = [WorkItem.contact_id == contact.id]
    if phones:
        clauses.append(WorkItem.customer_phone.in_(phones))
    if emails:
        clauses.append(WorkItem.customer_email.in_(emails))

    return list(
        s.scalars(
            select(WorkItem)
            .where(WorkItem.is_open.is_(True), or_(*clauses))
            .order_by(WorkItem.remote_updated_at.desc())
        )
    )


def tickets_context_block_for_identifiers(s: Session, identifiers: list[str]) -> str:
    """Open work matched on raw phone numbers and email addresses.

    The EspoCRM path has no local Contact row to hand in - it has whatever
    addresses Espo holds - so matching goes straight against those.
    """
    phones = [p for p in (normalize_phone(i) for i in identifiers) if p]
    emails = [e for e in (normalize_email(i) for i in identifiers) if e]
    if not phones and not emails:
        return ""

    clauses = []
    if phones:
        clauses.append(WorkItem.customer_phone.in_(phones))
    if emails:
        clauses.append(WorkItem.customer_email.in_(emails))

    items = list(
        s.scalars(
            select(WorkItem)
            .where(WorkItem.is_open.is_(True), or_(*clauses))
            .order_by(WorkItem.remote_updated_at.desc())
        )
    )
    return _format_open_work(items)


def tickets_context_block(s: Session, contact: Contact | None) -> str:
    """The open-tickets list handed to the analyst so it can attribute a call."""
    return _format_open_work(open_tickets_for(s, contact))


def _format_open_work(tickets: list[WorkItem]) -> str:
    if not tickets:
        return ""
    lines = [
        "Open work on this person, across both shop systems. If the "
        "conversation was about one of these, reference it by the exact id "
        "shown in brackets, copied character for character.",
    ]
    for item in tickets:
        kind = (
            "winter services quote"
            if item.system == WorkSystem.winter
            else "repair job"
        )
        parts = [f"- [{item.id}] {kind} {item.remote_id} ({item.status_label or item.status})"]
        if item.boat_info:
            parts.append(f"unit: {item.boat_info}")
        if item.storage_location:
            parts.append(f"stored: {item.storage_location}")
        if item.work_requested:
            parts.append(f"covers: {item.work_requested}")
        if item.balance:
            parts.append(f"balance: {item.balance}")
        if item.alert:
            parts.append(f"ALERT: {item.alert}")
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
    # The prompt says a sales or boat-club conversation never touches a work
    # order. This is what makes that true: most calls to this shop are not
    # service calls, and a kayak enquiry must not staple a note to the
    # caller's open engine job just because they have one.
    category = getattr(analysis, "category", None)
    if category is not None and CallCategory(category) not in WORK_ORDER_CATEGORIES:
        if getattr(analysis, "ticket_updates", None):
            log.info(
                "dropping %d ticket update(s) on a %s conversation (interaction %s)",
                len(analysis.ticket_updates), category, interaction.id,
            )
        return []

    staged: list[TicketNote] = []
    for update in getattr(analysis, "ticket_updates", []) or []:
        ticket = _resolve_item(s, update.ticket_id, interaction.contact_id)
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
            body=_format_note(interaction, update, ticket),
        )
        s.add(note)
        staged.append(note)

        # The first ticket mentioned is what this conversation was about.
        if interaction.ticket_id is None:
            interaction.ticket_id = ticket.id

    s.flush()
    return staged


def _resolve_item(s: Session, raw_id, contact_id: int | None) -> WorkItem | None:
    """Find the work item the model referred to.

    The context block hands it a namespaced id and asks for it back verbatim,
    which is the normal path. A bare number is accepted too - it is a plausible
    thing for a model to return, and refusing it would drop a correct note over
    a formatting detail. The bare form only resolves against *this contact's*
    items, so it cannot reach across to somebody else's job.
    """
    wanted = str(raw_id or "").strip()
    if not wanted:
        return None

    item = s.get(WorkItem, wanted)
    if item is not None:
        return item

    if contact_id is None:
        return None
    candidates = s.scalars(
        select(WorkItem).where(
            WorkItem.contact_id == contact_id,
            WorkItem.remote_id == wanted,
            WorkItem.is_open.is_(True),
        )
    ).all()
    # Ambiguous across systems is not resolvable, so it is refused rather than
    # guessed at.
    return candidates[0] if len(candidates) == 1 else None


def _format_note(interaction: Interaction, update, item: WorkItem) -> str:
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
        # Only the service tracker's work feeds BiT. A winter quote is priced
        # by its own engine, so naming BiT there would send someone to the
        # wrong system.
        lines += [
            "",
            "This changes the work — needs re-keying into BiT."
            if item.system != WorkSystem.winter
            else "This changes the work — the quote needs re-pricing.",
        ]
    if update.needs_writer_attention:
        lines += ["", "Needs someone in the office to action it."]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# pushing
# --------------------------------------------------------------------------- #
def push_note(s: Session, note: TicketNote) -> TicketNote:
    """Send one staged note to whichever system owns the item."""
    item = s.get(WorkItem, note.ticket_id)
    if item is None:
        note.status = NoteStatus.failed
        note.error = "That work item is no longer in the CRM. Sync and try again."
        s.flush()
        raise ServiceTrackerError(note.error)

    try:
        if item.system == WorkSystem.winter:
            _push_winter(s, item, note)
        else:
            servicetracker.get_client().add_writer_note(item.remote_id, note.body)
    except (ServiceTrackerError, WinterQuotesError) as exc:
        note.status = NoteStatus.failed
        note.error = str(exc)
        s.flush()
        raise

    note.status = NoteStatus.pushed
    note.pushed_at = utcnow()
    note.error = None
    s.flush()
    log.info("pushed note %s to %s", note.id, note.ticket_id)
    return note


def _push_winter(s: Session, item: WorkItem, note: TicketNote) -> None:
    """The winter staff note REPLACES rather than appends.

    So the existing note is read first and this one added underneath it. Losing
    what a staff member wrote by hand because a call came in afterwards would
    be a far worse bug than a note that is a little long.
    """
    client = winterquotes.get_client()
    existing = client.get_staff_note(item.remote_id).strip()
    if note.body.strip() in existing:
        return  # already there; nothing to do and nothing to lose
    combined = f"{existing}\n\n{note.body}".strip() if existing else note.body
    client.set_staff_note(item.remote_id, combined)


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
        except (ServiceTrackerError, WinterQuotesError, NotConfigured):
            stats["failed"] += 1
        s.commit()
    return stats
