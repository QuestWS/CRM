"""Google Calendar: push appointments the AI found, and read the day back."""
from __future__ import annotations

import datetime as dt
import logging
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.ingest.google_auth import calendar_service
from crm.models import Appointment, Contact, to_naive_utc

log = logging.getLogger(__name__)


def _local(value: dt.datetime) -> dt.datetime:
    """Naive UTC (how we store) -> aware local (how Calendar wants it)."""
    return value.replace(tzinfo=dt.UTC).astimezone(ZoneInfo(settings.timezone))


def _describe(s: Session, appt: Appointment) -> str:
    lines = [appt.description or ""]
    if appt.contact_id:
        contact = s.get(Contact, appt.contact_id)
        if contact:
            lines.append(f"Contact: {contact.display_name}")
            if contact.primary_phone:
                lines.append(f"Phone: {contact.primary_phone}")
            if contact.ai_profile:
                lines.append("\n--- CRM profile ---\n" + contact.ai_profile)
    lines.append("\nCreated by your CRM assistant.")
    return "\n".join(x for x in lines if x).strip()


def push_appointment(s: Session, appt: Appointment) -> Appointment:
    """Create or update the Google Calendar event for one appointment."""
    service = calendar_service()
    ends = appt.ends_at or appt.starts_at + dt.timedelta(hours=1)
    body = {
        "summary": appt.title,
        "description": _describe(s, appt),
        "location": appt.location or "",
        "start": {
            "dateTime": _local(appt.starts_at).isoformat(),
            "timeZone": settings.timezone,
        },
        "end": {
            "dateTime": _local(ends).isoformat(),
            "timeZone": settings.timezone,
        },
    }

    try:
        if appt.google_event_id:
            event = (
                service.events()
                .update(
                    calendarId=settings.calendar_id,
                    eventId=appt.google_event_id,
                    body=body,
                )
                .execute()
            )
        else:
            event = (
                service.events()
                .insert(calendarId=settings.calendar_id, body=body)
                .execute()
            )
    except Exception as exc:
        appt.sync_state = "error"
        appt.sync_error = str(exc)
        s.flush()
        log.exception("calendar push failed for appointment %s", appt.id)
        raise

    appt.google_event_id = event.get("id")
    appt.sync_state = "synced"
    appt.sync_error = None
    s.flush()
    log.info("calendar event %s for appointment %s", appt.google_event_id, appt.id)
    return appt


def push_unsynced(s: Session, limit: int = 25) -> dict[str, int]:
    """Sync appointments that are still local-only. Never raises."""
    stats = {"synced": 0, "failed": 0}
    rows = s.scalars(
        select(Appointment)
        .where(
            Appointment.sync_state == "local",
            Appointment.starts_at >= dt.datetime.utcnow() - dt.timedelta(days=1),
        )
        .order_by(Appointment.starts_at)
        .limit(limit)
    ).all()
    for appt in rows:
        try:
            push_appointment(s, appt)
            stats["synced"] += 1
        except Exception:
            stats["failed"] += 1
        s.commit()
    return stats


def upcoming_events(days: int = 1, max_results: int = 25) -> list[dict]:
    """Read the calendar back, so the brief reflects everything - not just ours."""
    service = calendar_service()
    now = dt.datetime.now(dt.UTC)
    events = (
        service.events()
        .list(
            calendarId=settings.calendar_id,
            timeMin=now.isoformat(),
            timeMax=(now + dt.timedelta(days=days)).isoformat(),
            singleEvents=True,
            orderBy="startTime",
            maxResults=max_results,
        )
        .execute()
    )
    out = []
    for event in events.get("items", []) or []:
        start = event.get("start", {})
        raw = start.get("dateTime") or start.get("date")
        parsed = None
        if raw:
            try:
                parsed = to_naive_utc(dt.datetime.fromisoformat(raw.replace("Z", "+00:00")))
            except ValueError:
                parsed = None
        out.append(
            {
                "id": event.get("id"),
                "title": event.get("summary") or "(untitled)",
                "starts_at": parsed,
                "all_day": "date" in start,
                "location": event.get("location"),
            }
        )
    return out
