"""Read what EspoCRM has fetched, analyse it, write the result back.

EspoCRM owns the mailbox, the contacts and the records. This service adds the
judgement: what was this email about, does it concern open work in the shop,
what did the customer say about themselves, and what needs doing.

Everything it writes is additive - a stream note, a task, a link. It never
edits an email, never deletes anything, and never sends mail. Espo's own
outbound is left entirely alone.
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
from crm.integrations import espocrm
from crm.integrations.espocrm import EspoError, EspoNotConfigured
from crm.models import WORK_ORDER_CATEGORIES, CallCategory

log = logging.getLogger(__name__)

WATERMARK_KEY = "espo:last_email_dateSent"
SEEN_KEY = "espo:recent_email_ids"
# Several emails can share a dateSent to the second, so the watermark alone
# would either re-analyse them or skip them. A short id memory closes that gap.
SEEN_MEMORY = 200

ESPO_DATETIME = "%Y-%m-%d %H:%M:%S"


def _now_stamp() -> str:
    return dt.datetime.utcnow().strftime(ESPO_DATETIME)


def _parse_stamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    for fmt in (ESPO_DATETIME, "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            parsed = dt.datetime.strptime(value.replace("Z", "+0000"), fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    return None


def _seen_ids(s: Session) -> list[str]:
    raw = get_state(s, SEEN_KEY, "") or ""
    return [x for x in raw.split(",") if x]


def _remember(s: Session, seen: list[str], email_id: str) -> list[str]:
    seen = [x for x in seen if x != email_id]
    seen.append(email_id)
    seen = seen[-SEEN_MEMORY:]
    set_state(s, SEEN_KEY, ",".join(seen))
    return seen


# --------------------------------------------------------------------------- #
def _direction(email: dict) -> str:
    """Espo marks a sent message with status 'Sent'; everything else came in."""
    return "outbound" if str(email.get("status") or "") == "Sent" else "inbound"


def _counterparty(email: dict) -> tuple[str | None, str | None]:
    """(name, address) of the person who is not us, best effort."""
    if _direction(email) == "outbound":
        raw_to = str(email.get("to") or "")
        address = raw_to.split(";")[0].strip() or None
    else:
        address = str(email.get("from") or "").strip() or None
    name = None
    name_hash = email.get("nameHash") or {}
    if address and isinstance(name_hash, dict):
        name = name_hash.get(address)
    return name, address


def _resolve_parent(client, email: dict) -> tuple[str | None, str | None]:
    """Which record the analysis should hang off.

    Espo may already have linked the email to a parent. If not, the
    counterparty's address is matched against Contacts, then Leads, then
    Accounts - the same order Espo itself prefers.
    """
    if email.get("parentType") and email.get("parentId"):
        return email["parentType"], email["parentId"]

    _, address = _counterparty(email)
    if not address:
        return None, None
    for entity in ("Contact", "Lead", "Account"):
        try:
            found = client.find_by_email(entity, address)
        except EspoError as exc:
            log.debug("lookup of %s in %s failed: %s", address, entity, exc)
            continue
        if found:
            return entity, found["id"]
    return None, None


def _context_for(s: Session, client, parent_type: str | None, parent_id: str | None) -> str:
    """What the CRM and the shop apps already know about this person."""
    if not parent_type or not parent_id:
        return ""

    parts: list[str] = []
    try:
        record = client.get(parent_type, parent_id)
    except EspoError:
        return ""

    label = record.get("name") or ""
    if label:
        parts.append(f"On file in EspoCRM as: {label} ({parent_type})")
    if record.get("description"):
        parts.append(f"Description: {record['description']}")

    # Open work in the two shop apps, matched on the addresses Espo holds.
    from crm.services.tickets import tickets_context_block_for_identifiers

    identifiers = [
        record.get("emailAddress"),
        record.get("phoneNumber"),
    ]
    block = tickets_context_block_for_identifiers(
        s, [i for i in identifiers if i]
    )
    if block:
        parts.append(block)
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
def analyse_email(s: Session, client, email_stub: dict) -> dict:
    """Analyse one email and write the result back. Returns what it did."""
    email = client.email_body(email_stub["id"])
    body = (email.get("bodyPlain") or "").strip()
    if not body and email.get("body"):
        from crm.ingest.mailparse import strip_html

        body = strip_html(email["body"]).strip()
    if not body:
        return {"skipped": "empty body"}

    parent_type, parent_id = _resolve_parent(client, email)
    occurred = _parse_stamp(email.get("dateSent")) or dt.datetime.utcnow()

    analysis = enrich.analyze_conversation(
        body=body,
        kind="email",
        direction=_direction(email),
        occurred_at=occurred,
        subject=email.get("name") or email.get("subject"),
        known_context=_context_for(s, client, parent_type, parent_id),
    )

    category = CallCategory(analysis.category)
    result = {
        "email_id": email["id"],
        "category": category.value,
        "parent": f"{parent_type}:{parent_id}" if parent_id else None,
        "note": False,
        "tasks": 0,
        "work_notes": 0,
    }

    # Nothing is learned from a robocall or an internal note, and posting one
    # into somebody's stream is pure noise.
    from crm.models import NO_PROFILE_CATEGORIES

    if category in NO_PROFILE_CATEGORIES:
        return result

    if parent_type and parent_id:
        client.post_note(
            parent_type,
            parent_id,
            _summary_note(email, analysis),
            internal=settings.espo_notes_internal,
        )
        result["note"] = True

    for task in analysis.tasks:
        if task.owner != "me":
            continue  # the customer committed to it, not us
        client.create_task(
            task.title,
            description=_task_description(task, email),
            parent_type=parent_type,
            parent_id=parent_id,
            date_end=(task.due_at_iso or "").replace("T", " ")[:19] or None,
            priority="High" if task.priority in ("high", "urgent") else "Normal",
        )
        result["tasks"] += 1

    if category in WORK_ORDER_CATEGORIES and analysis.ticket_updates:
        result["work_notes"] = _stage_work_notes(s, email, analysis)

    return result


def _summary_note(email: dict, analysis) -> str:
    """The stream post. Written for somebody who did not read the email."""
    lines = [analysis.summary.strip(), ""]
    if analysis.outcome:
        lines += [f"Where it stands: {analysis.outcome}", ""]

    if analysis.needs:
        lines.append("What they need:")
        lines += [f"• {n.title} — {n.description}" for n in analysis.needs]
        lines.append("")
    if analysis.facts:
        lines.append("Worth remembering:")
        lines += [f"• {f.text}" for f in analysis.facts]
        lines.append("")
    if analysis.appointments:
        lines.append("Dates discussed:")
        lines += [
            f"• {a.title} — {a.starts_at_iso or 'no time settled'}"
            + (" (tentative)" if a.tentative else "")
            for a in analysis.appointments
        ]
        lines.append("")
    if analysis.missed_opportunity:
        lines.append(f"Worth asking next time: {analysis.missed_opportunity}")

    tag = f"[{analysis.category}"
    if analysis.product_line and analysis.product_line != "none":
        tag += f" · {analysis.product_line}"
    tag += "]"
    lines.append("")
    lines.append(f"{tag} — read off the email by the assistant.")
    return "\n".join(lines).strip()


def _task_description(task, email: dict) -> str:
    parts = [task.detail or ""]
    if task.due_hint:
        parts.append(f"Timing as stated: {task.due_hint}")
    parts.append(f"From the email “{email.get('name') or '(no subject)'}”.")
    return "\n\n".join(p for p in parts if p)


def _stage_work_notes(s: Session, email: dict, analysis) -> int:
    """Stage notes for the shop apps, exactly as the call path does."""
    from crm.models import Direction, Interaction, InteractionKind
    from crm.services.tickets import stage_notes

    # A lightweight local Interaction carries provenance into the note text.
    # It is the record of "this came from an email", not a second inbox.
    interaction = Interaction(
        kind=InteractionKind.email,
        direction=Direction.outbound if _direction(email) == "outbound" else Direction.inbound,
        occurred_at=_parse_stamp(email.get("dateSent")) or dt.datetime.utcnow(),
        subject=email.get("name"),
        source="espocrm",
        source_ref=email["id"],
        category=CallCategory(analysis.category),
        processed=True,
    )
    s.add(interaction)
    s.flush()
    return len(stage_notes(s, interaction, analysis))


# --------------------------------------------------------------------------- #
def sync_emails(s: Session, *, limit: int | None = None) -> dict[str, int]:
    """One pass: everything Espo has fetched since the watermark."""
    client = espocrm.get_client()
    limit = limit or settings.espo_max_per_sync

    since = get_state(s, WATERMARK_KEY)
    if not since:
        since = (
            dt.datetime.utcnow() - dt.timedelta(days=settings.espo_backfill_days)
        ).strftime(ESPO_DATETIME)

    emails = client.recent_emails(since=since, limit=limit)
    seen = _seen_ids(s)
    stats = {"seen": len(emails), "analysed": 0, "skipped": 0, "failed": 0}

    for email in emails:
        if email["id"] in seen:
            stats["skipped"] += 1
            continue
        try:
            outcome = analyse_email(s, client, email)
            if outcome.get("skipped"):
                stats["skipped"] += 1
            else:
                stats["analysed"] += 1
                log.info("analysed email %s: %s", email["id"], outcome)
        except (AIUnavailable, TranscriptTooLong) as exc:
            # Leave the watermark where it is so a later run retries this one.
            log.warning("email %s not analysed: %s", email["id"], exc)
            stats["failed"] += 1
            s.commit()
            return stats
        except EspoError as exc:
            log.warning("email %s failed against Espo: %s", email["id"], exc)
            stats["failed"] += 1
            continue
        except Exception:
            log.exception("unexpected error on email %s", email["id"])
            stats["failed"] += 1
            continue

        seen = _remember(s, seen, email["id"])
        if email.get("dateSent"):
            set_state(s, WATERMARK_KEY, email["dateSent"])
        s.commit()

    return stats


def check(s: Session) -> dict:
    """What `espo-check` reports: can we reach it, and what is waiting."""
    client = espocrm.get_client()
    if not client.configured:
        raise EspoNotConfigured(
            "Set ESPO_URL and ESPO_API_KEY in .env — see docs/espocrm-setup.md."
        )
    user = client.health()
    since = get_state(s, WATERMARK_KEY)
    counts = {}
    for entity in ("Contact", "Account", "Lead", "Email", "Task", "Case"):
        try:
            counts[entity] = int(client.list(entity, max_size=1).get("total") or 0)
        except EspoError as exc:
            counts[entity] = f"unavailable ({exc})"
    return {
        "url": client.base_url,
        "user": user.get("user", {}).get("userName") if isinstance(user, dict) else None,
        "watermark": since or "(none — a first sync will back-fill)",
        "counts": counts,
    }
