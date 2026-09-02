"""Daily brief, follow-up detection, and the dashboard's data."""
from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from crm.ai import enrich
from crm.ai.client import AIUnavailable
from crm.ai.schemas import DailyBrief
from crm.models import (
    Appointment,
    Contact,
    Interaction,
    InteractionKind,
    Need,
    NeedStatus,
    Recording,
    RecordingStatus,
    Task,
    TaskStatus,
)

log = logging.getLogger(__name__)

GOING_COLD_DAYS = 14
STALE_NEED_DAYS = 7


def _now() -> dt.datetime:
    return dt.datetime.utcnow()


# --------------------------------------------------------------------------- #
def open_tasks(s: Session, limit: int = 100) -> list[Task]:
    return list(
        s.scalars(
            select(Task)
            .where(Task.status == TaskStatus.open)
            .order_by(Task.due_at.is_(None), Task.due_at, Task.created_at.desc())
            .limit(limit)
        )
    )


def overdue_tasks(s: Session) -> list[Task]:
    return list(
        s.scalars(
            select(Task)
            .where(
                Task.status == TaskStatus.open,
                Task.due_at.is_not(None),
                Task.due_at < _now(),
            )
            .order_by(Task.due_at)
        )
    )


def todays_appointments(s: Session, days: int = 1) -> list[Appointment]:
    start = _now()
    return list(
        s.scalars(
            select(Appointment)
            .where(
                Appointment.starts_at >= start - dt.timedelta(hours=12),
                Appointment.starts_at <= start + dt.timedelta(days=days),
            )
            .order_by(Appointment.starts_at)
        )
    )


def going_cold(s: Session, days: int = GOING_COLD_DAYS, limit: int = 20) -> list[Contact]:
    """Contacts with an open need who haven't been touched in a while."""
    cutoff = _now() - dt.timedelta(days=days)
    return list(
        s.scalars(
            select(Contact)
            .join(Need, Need.contact_id == Contact.id)
            .where(
                Need.status.in_([NeedStatus.open, NeedStatus.in_progress]),
                or_(
                    Contact.last_contacted_at.is_(None),
                    Contact.last_contacted_at < cutoff,
                ),
            )
            .group_by(Contact.id)
            .order_by(Contact.last_contacted_at.asc().nulls_first())
            .limit(limit)
        )
    )


def unprocessed_counts(s: Session) -> dict[str, int]:
    return {
        "recordings_queued": s.scalar(
            select(func.count(Recording.id)).where(
                Recording.status.in_(
                    [RecordingStatus.pending, RecordingStatus.transcribed]
                )
            )
        )
        or 0,
        "recordings_failed": s.scalar(
            select(func.count(Recording.id)).where(
                Recording.status == RecordingStatus.failed
            )
        )
        or 0,
        "emails_unanalysed": s.scalar(
            select(func.count(Interaction.id)).where(
                Interaction.kind == InteractionKind.email,
                Interaction.processed.is_(False),
            )
        )
        or 0,
    }


def recent_interactions(s: Session, limit: int = 15) -> list[Interaction]:
    return list(
        s.scalars(
            select(Interaction).order_by(Interaction.occurred_at.desc()).limit(limit)
        )
    )


# --------------------------------------------------------------------------- #
def build_brief_context(s: Session) -> str:
    now = _now()
    lines = [f"Today is {now:%A, %B %d %Y} (times below are UTC)."]

    appts = todays_appointments(s)
    lines.append("\n<appointments_next_24h>")
    if appts:
        for a in appts:
            who = s.get(Contact, a.contact_id) if a.contact_id else None
            lines.append(
                f"- {a.starts_at:%a %H:%M} {a.title}"
                + (f" with {who.display_name}" if who else "")
                + (f" at {a.location}" if a.location else "")
                + (" [TENTATIVE - not confirmed]" if a.sync_state == "local" else "")
            )
    else:
        lines.append("(none)")
    lines.append("</appointments_next_24h>")

    lines.append("\n<open_tasks>")
    tasks = open_tasks(s, limit=40)
    if tasks:
        for t in tasks:
            who = s.get(Contact, t.contact_id) if t.contact_id else None
            due = f" due {t.due_at:%a %d %b}" if t.due_at else " (no due date)"
            overdue = " [OVERDUE]" if t.due_at and t.due_at < now else ""
            lines.append(
                f"- [{t.priority}]{overdue} {t.title}"
                + (f" - {who.display_name}" if who else "")
                + due
            )
    else:
        lines.append("(none)")
    lines.append("</open_tasks>")

    lines.append("\n<open_needs>")
    needs = s.scalars(
        select(Need)
        .where(Need.status.in_([NeedStatus.open, NeedStatus.in_progress]))
        .order_by(Need.updated_at.desc())
        .limit(30)
    ).all()
    if needs:
        for n in needs:
            who = s.get(Contact, n.contact_id)
            age = (now - n.opened_at).days
            lines.append(
                f"- {who.display_name if who else '?'}: {n.title} "
                f"[{n.status.value}, {n.urgency or 'normal'}, opened {age}d ago]"
                + (f" blocked: {n.blockers}" if n.blockers else "")
            )
    else:
        lines.append("(none)")
    lines.append("</open_needs>")

    lines.append("\n<recent_conversations>")
    for i in recent_interactions(s, limit=12):
        who = s.get(Contact, i.contact_id) if i.contact_id else None
        lines.append(
            f"- {i.occurred_at:%d %b %H:%M} {i.kind.value} "
            f"with {who.display_name if who else 'unknown'}: "
            f"{i.summary or i.outcome or (i.subject or '')}"
        )
    lines.append("</recent_conversations>")

    lines.append("\n<going_cold>")
    cold = going_cold(s)
    if cold:
        for c in cold:
            last = (
                f"{(now - c.last_contacted_at).days}d ago"
                if c.last_contacted_at
                else "never"
            )
            lines.append(f"- {c.display_name}: last contact {last}, has an open need")
    else:
        lines.append("(nobody drifting)")
    lines.append("</going_cold>")

    counts = unprocessed_counts(s)
    if any(counts.values()):
        lines.append(
            f"\n<system_backlog>{counts['recordings_queued']} recordings waiting, "
            f"{counts['recordings_failed']} failed, "
            f"{counts['emails_unanalysed']} emails not yet analysed."
            "</system_backlog>"
        )
    return "\n".join(lines)


def generate_brief(s: Session) -> DailyBrief | None:
    """The morning brief. None when the AI is unreachable."""
    try:
        return enrich.write_brief(context_block=build_brief_context(s))
    except AIUnavailable as exc:
        log.warning("daily brief skipped: %s", exc)
        return None


def dashboard(s: Session) -> dict:
    """Everything the home page needs, in one pass."""
    return {
        "appointments": todays_appointments(s, days=7),
        "tasks": open_tasks(s, limit=25),
        "overdue": overdue_tasks(s),
        "recent": recent_interactions(s, limit=12),
        "cold": going_cold(s, limit=8),
        "counts": unprocessed_counts(s),
        "contact_count": s.scalar(select(func.count(Contact.id))) or 0,
        "open_needs": s.scalar(
            select(func.count(Need.id)).where(
                Need.status.in_([NeedStatus.open, NeedStatus.in_progress])
            )
        )
        or 0,
    }
