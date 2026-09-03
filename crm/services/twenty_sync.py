"""Read what Twenty synced from the mailbox, analyse it, write back.

Twenty owns the mailbox, the people and the records. This adds the judgement:
what the conversation was about, what the customer needs, what they said about
themselves, and what it changes in the shop's other systems.

Everything written is additive - a note, a task, a link. It never edits a
message, never deletes, and never sends mail.
"""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session

from crm.ai import enrich
from crm.ai.client import AIUnavailable
from crm.ai.enrich import TranscriptTooLong
from crm.config import settings
from crm.db import get_state, set_state
from crm.integrations import twenty
from crm.integrations.twenty import TwentyError, TwentyNotConfigured
from crm.models import NO_PROFILE_CATEGORIES, WORK_ORDER_CATEGORIES, CallCategory

log = logging.getLogger(__name__)

WATERMARK_KEY = "twenty:last_message_receivedAt"
SEEN_KEY = "twenty:recent_message_ids"
# Messages can share a receivedAt to the second, so the timestamp alone would
# either re-analyse them or skip them. A short id memory closes that gap.
SEEN_MEMORY = 200

ISO = "%Y-%m-%dT%H:%M:%S.%fZ"


def _parse(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed


def _seen_ids(s: Session) -> list[str]:
    return [x for x in (get_state(s, SEEN_KEY, "") or "").split(",") if x]


def _remember(s: Session, seen: list[str], message_id: str) -> list[str]:
    seen = [x for x in seen if x != message_id][-SEEN_MEMORY + 1 :]
    seen.append(message_id)
    set_state(s, SEEN_KEY, ",".join(seen))
    return seen


# --------------------------------------------------------------------------- #
def _participants(message: dict) -> tuple[dict | None, list[dict]]:
    """(sender, recipients) off the message's participant records."""
    people = message.get("messageParticipants") or []
    sender = next((p for p in people if p.get("role") == "from"), None)
    recipients = [p for p in people if p.get("role") in ("to", "cc")]
    return sender, recipients


def _direction(message: dict) -> str:
    """Outbound when the sender is one of the operator's own addresses."""
    from crm.services.identity import is_operator_email

    sender, _ = _participants(message)
    return "outbound" if sender and is_operator_email(sender.get("handle")) else "inbound"


def _counterparty(message: dict) -> dict | None:
    """The participant who is not us, with their linked person if Twenty
    already matched one."""
    from crm.services.identity import is_operator_email

    sender, recipients = _participants(message)
    if _direction(message) == "outbound":
        return next((r for r in recipients if not is_operator_email(r.get("handle"))), None)
    return sender


def _person_id(participant: dict | None) -> str | None:
    """Twenty links a participant to a Person once it recognises the address."""
    if not participant:
        return None
    person = participant.get("person") or {}
    return person.get("id") or participant.get("personId")


def _resolve_person(client, message: dict) -> tuple[str | None, str | None]:
    """(person id, email). Falls back to a lookup when Twenty has not linked."""
    participant = _counterparty(message)
    if not participant:
        return None, None
    address = participant.get("handle")
    person_id = _person_id(participant)
    if person_id:
        return person_id, address
    if address:
        try:
            found = client.find_person_by_email(address)
        except TwentyError as exc:
            log.debug("person lookup failed for %s: %s", address, exc)
            found = None
        if found:
            return found.get("id"), address
    return None, address


def _context_for(s: Session, client, person_id: str | None, address: str | None) -> str:
    """What the shop's other systems already know about this person."""
    from crm.services.tickets import tickets_context_block_for_identifiers

    identifiers: list[str] = [address] if address else []
    label = None
    if person_id:
        try:
            person = client.get("people", person_id, depth=1)
        except TwentyError:
            person = {}
        name = person.get("name") or {}
        label = " ".join(
            x for x in (name.get("firstName"), name.get("lastName")) if x
        ).strip() or None
        primary = (person.get("phones") or {}).get("primaryPhoneNumber")
        if primary:
            identifiers.append(primary)

    parts = []
    if label:
        parts.append(f"On file in Twenty as: {label}")
    block = tickets_context_block_for_identifiers(s, identifiers)
    if block:
        parts.append(block)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
def analyse_message(s: Session, client, message: dict) -> dict:
    body = (message.get("text") or "").strip()
    if not body:
        return {"skipped": "empty body"}

    person_id, address = _resolve_person(client, message)
    occurred = _parse(message.get("receivedAt")) or dt.datetime.utcnow()

    analysis = enrich.analyze_conversation(
        body=body,
        kind="email",
        direction=_direction(message),
        occurred_at=occurred,
        subject=message.get("subject"),
        known_context=_context_for(s, client, person_id, address),
    )

    category = CallCategory(analysis.category)
    result = {
        "message_id": message.get("id"),
        "category": category.value,
        "person": person_id,
        "note": False,
        "tasks": 0,
        "work_notes": 0,
    }

    # A robocall or an internal note teaches nothing, and putting one on
    # somebody's timeline is noise.
    if category in NO_PROFILE_CATEGORIES:
        return result

    if person_id:
        client.post_note(
            _note_title(message, analysis),
            _note_body(analysis),
            person_id=person_id,
        )
        result["note"] = True

    for task in analysis.tasks:
        if task.owner != "me":
            continue  # the customer committed to it, not us
        client.create_task(
            task.title,
            body_markdown=_task_body(task, message),
            person_id=person_id,
            due_at=_task_due(task),
        )
        result["tasks"] += 1

    if category in WORK_ORDER_CATEGORIES and analysis.ticket_updates:
        result["work_notes"] = _stage_work_notes(s, message, analysis)

    return result


def _note_title(message: dict, analysis) -> str:
    subject = (message.get("subject") or "").strip() or "Email"
    return f"{subject} — {analysis.category}"[:255]


def _note_body(analysis) -> str:
    """Markdown, because that is the half of Twenty's rich text that round
    trips cleanly through the API."""
    lines = [analysis.summary.strip(), ""]
    if analysis.outcome:
        lines += [f"**Where it stands:** {analysis.outcome}", ""]
    if analysis.needs:
        lines.append("**What they need**")
        lines += [f"- {n.title} — {n.description}" for n in analysis.needs]
        lines.append("")
    if analysis.facts:
        lines.append("**Worth remembering**")
        lines += [f"- {f.text}" for f in analysis.facts]
        lines.append("")
    if analysis.appointments:
        lines.append("**Dates discussed**")
        lines += [
            f"- {a.title} — {a.starts_at_iso or 'no time settled'}"
            + (" _(tentative)_" if a.tentative else "")
            for a in analysis.appointments
        ]
        lines.append("")
    if analysis.missed_opportunity:
        lines += [f"**Worth asking next time:** {analysis.missed_opportunity}", ""]

    tag = analysis.category
    if analysis.product_line and analysis.product_line != "none":
        tag += f" · {analysis.product_line}"
    lines.append(f"_[{tag}] — read off the email by the assistant._")
    return "\n".join(lines).strip()


def _task_body(task, message: dict) -> str:
    parts = [task.detail or ""]
    if task.due_hint:
        parts.append(f"Timing as stated: {task.due_hint}")
    parts.append(f"From the email “{message.get('subject') or '(no subject)'}”.")
    return "\n\n".join(p for p in parts if p)


def _task_due(task) -> str | None:
    parsed = _parse(task.due_at_iso) if task.due_at_iso else None
    return parsed.strftime(ISO)[:-4] + "Z" if parsed else None


def _stage_work_notes(s: Session, message: dict, analysis) -> int:
    from crm.models import Direction, Interaction, InteractionKind
    from crm.services.tickets import stage_notes

    interaction = Interaction(
        kind=InteractionKind.email,
        direction=(
            Direction.outbound if _direction(message) == "outbound" else Direction.inbound
        ),
        occurred_at=_parse(message.get("receivedAt")) or dt.datetime.utcnow(),
        subject=message.get("subject"),
        source="twenty",
        source_ref=str(message.get("id")),
        category=CallCategory(analysis.category),
        processed=True,
    )
    s.add(interaction)
    s.flush()
    return len(stage_notes(s, interaction, analysis))


# --------------------------------------------------------------------------- #
def sync_messages(s: Session, *, limit: int | None = None) -> dict[str, int]:
    client = twenty.get_client()
    limit = limit or settings.twenty_max_per_sync

    since = get_state(s, WATERMARK_KEY)
    if not since:
        since = (
            dt.datetime.utcnow() - dt.timedelta(days=settings.twenty_backfill_days)
        ).strftime(ISO)[:-4] + "Z"

    messages = client.recent_messages(since=since, limit=limit)
    seen = _seen_ids(s)
    stats = {"seen": len(messages), "analysed": 0, "skipped": 0, "failed": 0}

    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id or message_id in seen:
            stats["skipped"] += 1
            continue
        try:
            outcome = analyse_message(s, client, message)
            if outcome.get("skipped"):
                stats["skipped"] += 1
            else:
                stats["analysed"] += 1
                log.info("analysed message %s: %s", message_id, outcome)
        except (AIUnavailable, TranscriptTooLong) as exc:
            # Hold the watermark so a later run retries rather than skipping.
            log.warning("message %s not analysed: %s", message_id, exc)
            stats["failed"] += 1
            s.commit()
            return stats
        except TwentyError as exc:
            log.warning("message %s failed against Twenty: %s", message_id, exc)
            stats["failed"] += 1
            continue
        except Exception:
            log.exception("unexpected error on message %s", message_id)
            stats["failed"] += 1
            continue

        seen = _remember(s, seen, message_id)
        if message.get("receivedAt"):
            set_state(s, WATERMARK_KEY, message["receivedAt"])
        s.commit()

    return stats


def check(s: Session) -> dict:
    client = twenty.get_client()
    if not client.configured:
        raise TwentyNotConfigured(
            "Set TWENTY_URL and TWENTY_API_KEY in .env — see docs/twenty-setup.md."
        )
    client.health()
    workspace = client.whoami()
    counts = {}
    for obj in ("people", "companies", "messages", "notes", "tasks"):
        try:
            page = client.list(obj, limit=1)
            counts[obj] = len(client._records(page, obj)) and "reachable" or "empty"
        except TwentyError as exc:
            counts[obj] = f"unavailable ({exc})"
    return {
        "url": client.base_url,
        "workspace": workspace.get("displayName"),
        "watermark": get_state(s, WATERMARK_KEY) or "(none — first sync back-fills)",
        "objects": counts,
    }
