"""Fold a ConversationAnalysis into the CRM, then rewrite the contact's profile."""
from __future__ import annotations

import datetime as dt
import logging
import re
from difflib import SequenceMatcher
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.ai import enrich
from crm.ai.client import AIUnavailable
from crm.ai.schemas import ConversationAnalysis
from crm.config import settings
from crm.models import (
    Appointment,
    Contact,
    Fact,
    FactCategory,
    Interaction,
    Need,
    NeedStatus,
    Task,
    TaskStatus,
    to_naive_utc,
    utcnow,
)
from crm.services.identity import (
    attach_email,
    attach_phone,
    normalize_email,
    normalize_phone,
)

log = logging.getLogger(__name__)

FACT_MATCH_RATIO = 0.86
NEED_MATCH_RATIO = 0.68
TASK_MATCH_RATIO = 0.85
HISTORY_WINDOW = 20


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _similar(a: str, b: str) -> float:
    """How likely two strings describe the same thing.

    Character similarity alone misses reordering - "furnace replacement quote"
    and "quote for furnace replacement" score 0.70 and would become two needs.
    Token overlap alone misses rewording. Take whichever is more confident;
    the thresholds above are calibrated against that combined score.
    """
    left, right = _norm(a), _norm(b)
    sequence = SequenceMatcher(None, left, right).ratio()

    a_tokens, b_tokens = set(left.split()), set(right.split())
    if not a_tokens or not b_tokens:
        return sequence
    dice = 2 * len(a_tokens & b_tokens) / (len(a_tokens) + len(b_tokens))
    return max(sequence, dice)


def _parse_iso(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.debug("unparseable datetime from model: %r", value)
        return None
    if parsed.tzinfo is not None:
        return to_naive_utc(parsed)
    # The prompt asks for local time; convert to the schema's UTC convention.
    return to_naive_utc(parsed.replace(tzinfo=ZoneInfo(settings.timezone)))


# --------------------------------------------------------------------------- #
def apply_analysis(
    s: Session, contact: Contact, interaction: Interaction, analysis: ConversationAnalysis
) -> dict[str, int]:
    """Write the extracted records. Returns per-type counts of what was new."""
    counts = {"facts": 0, "facts_confirmed": 0, "needs": 0, "tasks": 0, "appointments": 0}

    interaction.summary = analysis.summary
    interaction.outcome = analysis.outcome
    interaction.sentiment = analysis.sentiment

    _apply_person(s, contact, analysis)

    for fact in analysis.facts:
        if _upsert_fact(s, contact, interaction, fact):
            counts["facts"] += 1
        else:
            counts["facts_confirmed"] += 1

    for need in analysis.needs:
        if _upsert_need(s, contact, interaction, need):
            counts["needs"] += 1

    for task in analysis.tasks:
        if _add_task(s, contact, interaction, task):
            counts["tasks"] += 1

    for appt in analysis.appointments:
        if _add_appointment(s, contact, interaction, appt):
            counts["appointments"] += 1

    if analysis.followup_suggestion and not analysis.tasks:
        # Nothing was formally committed, but there is still an obvious next move.
        s.add(
            Task(
                contact_id=contact.id,
                title=analysis.followup_suggestion[:400],
                detail="Suggested follow-up - nobody committed to this on the call.",
                priority="low",
                created_by="ai",
                source_interaction_id=interaction.id,
            )
        )
        counts["tasks"] += 1

    contact.last_contacted_at = max(
        interaction.occurred_at, contact.last_contacted_at or interaction.occurred_at
    )
    interaction.processed = True
    s.flush()
    return counts


def _apply_person(s: Session, contact: Contact, analysis: ConversationAnalysis) -> None:
    person = analysis.person
    from crm.services.identity import _is_placeholder, _split_name

    if person.full_name and person.name_confidence >= 0.5 and _is_placeholder(
        contact.display_name
    ):
        contact.display_name = person.full_name.strip()
        _split_name(contact, person.full_name)
    contact.company = contact.company or person.company
    contact.title = contact.title or person.title
    contact.address = contact.address or person.address

    if extra := normalize_phone(person.phone):
        attach_phone(s, contact, extra, label="mentioned on call")
    if addr := normalize_email(person.email):
        attach_email(s, contact, addr, label="mentioned on call")


def _upsert_fact(s: Session, contact: Contact, interaction: Interaction, extracted) -> bool:
    """True if a new fact row was created, False if it confirmed an existing one."""
    existing = s.scalars(
        select(Fact).where(Fact.contact_id == contact.id, Fact.archived.is_(False))
    ).all()
    for row in existing:
        if _similar(row.text, extracted.text) >= FACT_MATCH_RATIO:
            row.last_confirmed_at = utcnow()
            row.confidence = max(row.confidence, extracted.confidence)
            return False

    s.add(
        Fact(
            contact_id=contact.id,
            category=FactCategory(extracted.category),
            text=extracted.text,
            quote=extracted.quote,
            confidence=extracted.confidence,
            source_interaction_id=interaction.id,
        )
    )
    return True


def _upsert_need(s: Session, contact: Contact, interaction: Interaction, extracted) -> bool:
    open_needs = s.scalars(
        select(Need).where(
            Need.contact_id == contact.id,
            Need.status.in_([NeedStatus.open, NeedStatus.in_progress]),
        )
    ).all()
    for row in open_needs:
        if _similar(row.title, extracted.title) >= NEED_MATCH_RATIO:
            row.description = extracted.description or row.description
            row.urgency = extracted.urgency or row.urgency
            row.blockers = extracted.blockers or row.blockers
            row.value_estimate = row.value_estimate or extracted.value_estimate
            if extracted.resolved_in_this_conversation:
                row.status = NeedStatus.resolved
                row.resolved_at = utcnow()
            elif row.status == NeedStatus.open:
                row.status = NeedStatus.in_progress
            return False

    s.add(
        Need(
            contact_id=contact.id,
            title=extracted.title[:300],
            description=extracted.description,
            urgency=extracted.urgency,
            value_estimate=extracted.value_estimate,
            blockers=extracted.blockers,
            status=(
                NeedStatus.resolved
                if extracted.resolved_in_this_conversation
                else NeedStatus.open
            ),
            resolved_at=utcnow() if extracted.resolved_in_this_conversation else None,
            source_interaction_id=interaction.id,
        )
    )
    return True


def _add_task(s: Session, contact: Contact, interaction: Interaction, extracted) -> bool:
    open_tasks = s.scalars(
        select(Task).where(Task.contact_id == contact.id, Task.status == TaskStatus.open)
    ).all()
    if any(_similar(t.title, extracted.title) >= TASK_MATCH_RATIO for t in open_tasks):
        return False

    detail = extracted.detail
    if extracted.owner == "them":
        note = "The customer committed to this, not you - track it, don't do it."
        detail = f"{detail}\n\n{note}" if detail else note

    s.add(
        Task(
            contact_id=contact.id,
            title=extracted.title[:400],
            detail=detail,
            due_at=_parse_iso(extracted.due_at_iso),
            priority=extracted.priority,
            created_by="ai",
            source_interaction_id=interaction.id,
        )
    )
    return True


def _add_appointment(
    s: Session, contact: Contact, interaction: Interaction, extracted
) -> bool:
    starts = _parse_iso(extracted.starts_at_iso)
    if starts is None:
        # No concrete time was agreed; a task is the honest representation.
        s.add(
            Task(
                contact_id=contact.id,
                title=f"Pin down a time: {extracted.title}"[:400],
                detail="Discussed on the call but no date and time were settled.",
                created_by="ai",
                source_interaction_id=interaction.id,
            )
        )
        return False

    duplicate = s.scalar(
        select(Appointment).where(
            Appointment.contact_id == contact.id, Appointment.starts_at == starts
        )
    )
    if duplicate:
        return False

    s.add(
        Appointment(
            contact_id=contact.id,
            title=extracted.title[:400],
            description=(
                "Tentative - confirm with the customer."
                if extracted.tentative
                else None
            ),
            location=extracted.location,
            starts_at=starts,
            ends_at=_parse_iso(extracted.ends_at_iso) or starts + dt.timedelta(hours=1),
            source_interaction_id=interaction.id,
        )
    )
    return True


# --------------------------------------------------------------------------- #
def build_profile_context(s: Session, contact: Contact) -> tuple[str, str, str, str]:
    facts = s.scalars(
        select(Fact)
        .where(Fact.contact_id == contact.id, Fact.archived.is_(False))
        .order_by(Fact.category, Fact.last_confirmed_at.desc())
    ).all()
    facts_block = "\n".join(
        f"- [{f.category.value}] {f.text}"
        + (f' (they said: "{f.quote}")' if f.quote else "")
        for f in facts
    )

    needs = s.scalars(
        select(Need).where(Need.contact_id == contact.id).order_by(Need.opened_at.desc())
    ).all()
    needs_block = "\n".join(
        f"- [{n.status.value}/{n.urgency or 'normal'}] {n.title}: "
        f"{n.description or ''}"
        + (f" Blocked by: {n.blockers}" if n.blockers else "")
        for n in needs
    )

    history = s.scalars(
        select(Interaction)
        .where(Interaction.contact_id == contact.id)
        .order_by(Interaction.occurred_at.desc())
        .limit(HISTORY_WINDOW)
    ).all()
    history_block = "\n\n".join(
        f"{i.occurred_at:%Y-%m-%d %H:%M} {i.kind.value} "
        f"({i.direction.value})"
        + (f" - {i.subject}" if i.subject else "")
        + f"\n{i.summary or (i.body or '')[:1200]}"
        for i in reversed(history)
    )

    label = contact.display_name + (f" - {contact.company}" if contact.company else "")
    return label, facts_block, needs_block, history_block


def refresh_profile(s: Session, contact: Contact) -> bool:
    """Rewrite the contact's narrative profile. False if the AI was unavailable."""
    label, facts_block, needs_block, history_block = build_profile_context(s, contact)
    try:
        narrative = enrich.write_profile(
            contact_label=label,
            facts_block=facts_block,
            needs_block=needs_block,
            history_block=history_block,
        )
    except AIUnavailable as exc:
        log.warning("profile refresh skipped for contact %s: %s", contact.id, exc)
        return False

    sections = [narrative.profile.strip()]
    if narrative.talking_points:
        sections.append(
            "Worth raising next time:\n"
            + "\n".join(f"- {p}" for p in narrative.talking_points)
        )
    if narrative.watch_outs:
        sections.append(
            "Watch out for:\n" + "\n".join(f"- {w}" for w in narrative.watch_outs)
        )
    contact.ai_profile = "\n\n".join(sections)
    contact.ai_profile_updated_at = utcnow()
    s.flush()
    return True


def known_context_block(s: Session, contact: Contact | None) -> str:
    """The 'already on file' block handed to the analyst so it doesn't re-extract."""
    if contact is None:
        return ""
    _, facts_block, needs_block, _ = build_profile_context(s, contact)
    parts = [f"Name on file: {contact.display_name}"]
    if contact.company:
        parts.append(f"Company: {contact.company}")
    if facts_block:
        parts.append("Known facts:\n" + facts_block)
    if needs_block:
        parts.append("Needs on file:\n" + needs_block)
    if contact.ai_profile:
        parts.append("Current profile:\n" + contact.ai_profile)
    return "\n\n".join(parts)


__all__ = [
    "apply_analysis",
    "build_profile_context",
    "known_context_block",
    "refresh_profile",
]
