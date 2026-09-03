"""Background jobs. Runs in-process with the web app, or standalone.

Deliberately a single-process scheduler with no broker: this is one person's
CRM, and a Redis dependency would cost more than it buys.
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from crm.config import settings
from crm.db import session_scope

log = logging.getLogger(__name__)


def job_process_calls() -> None:
    """Import new recordings from the watch folder and run them through the pipeline."""
    from crm.services.pipeline import import_and_process

    with session_scope() as s:
        stats = import_and_process(s)
    if any(stats.values()):
        log.info("call pipeline: %s", stats)


def job_sync_mail() -> None:
    from crm.ingest.imap_mail import ImapNotConfigured, sync_mail
    from crm.services.pipeline import run_unprocessed_emails

    if settings.twenty_configured or settings.espo_configured or not settings.imap_configured:
        # EspoCRM fetches the mailbox itself. Running both would import every
        # message twice, once into each system.
        return
    try:
        with session_scope() as s:
            added = sync_mail(s)
    except ImapNotConfigured as exc:
        log.warning("mail sync skipped: %s", exc)
        return
    except Exception:
        log.exception("mail sync failed")
        return

    if any(added.values()):
        log.info("imap sync: %s", added)
    with session_scope() as s:
        stats = run_unprocessed_emails(s)
    if any(stats.values()):
        log.info("email analysis: %s", stats)


def job_sync_calendar() -> None:
    from crm.ingest.gcal import push_unsynced
    from crm.ingest.google_auth import is_configured

    if not is_configured():
        return
    try:
        with session_scope() as s:
            stats = push_unsynced(s)
    except Exception:
        log.exception("calendar sync failed")
        return
    if any(stats.values()):
        log.info("calendar sync: %s", stats)


def job_walk_in_intakes() -> None:
    """Pick up counter recordings orphaned by a restart mid-transcription."""
    from crm.services.intake import run_pending

    with session_scope() as s:
        stats = run_pending(s)
    if any(stats.values()):
        log.info("walk-in intake: %s", stats)


def job_twenty_messages() -> None:
    """Analyse whatever Twenty has synced from the mailbox since last pass."""
    from crm.integrations.twenty import TwentyError, TwentyNotConfigured
    from crm.services.twenty_sync import sync_messages

    if not settings.twenty_configured:
        return
    try:
        with session_scope() as s:
            stats = sync_messages(s)
    except (TwentyError, TwentyNotConfigured) as exc:
        log.warning("twenty sync skipped: %s", exc)
        return
    except Exception:
        log.exception("twenty sync failed")
        return
    if stats["analysed"] or stats["failed"]:
        log.info("twenty message sync: %s", stats)


def job_espo_emails() -> None:
    """Analyse whatever EspoCRM has fetched since the last pass."""
    from crm.integrations.espocrm import EspoError, EspoNotConfigured
    from crm.services.espo_sync import sync_emails

    if settings.twenty_configured or not settings.espo_configured:
        return
    try:
        with session_scope() as s:
            stats = sync_emails(s)
    except (EspoError, EspoNotConfigured) as exc:
        log.warning("espo sync skipped: %s", exc)
        return
    except Exception:
        log.exception("espo sync failed")
        return
    if stats["analysed"] or stats["failed"]:
        log.info("espo email sync: %s", stats)


def job_sync_work() -> None:
    """Mirror open work from both shop apps. Either being down is survivable."""
    from crm.services.tickets import push_all_pending, sync_all

    if not (settings.servicetracker_configured or settings.winter_configured):
        return
    try:
        with session_scope() as s:
            stats = sync_all(s)
        with session_scope() as s:
            pushed = push_all_pending(s)
    except Exception:
        log.exception("work item sync failed")
        return
    if stats or any(pushed.values()):
        log.info("work sync: %s, notes %s", stats, pushed)


def job_daily_brief() -> None:
    from crm.services.briefing import generate_brief

    with session_scope() as s:
        brief = generate_brief(s)
    if brief:
        log.info("daily brief: %s", brief.headline)


def build_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(
        timezone=settings.timezone,
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 600},
    )
    scheduler.add_job(
        job_process_calls,
        IntervalTrigger(seconds=settings.pipeline_poll_seconds),
        id="process_calls",
        replace_existing=True,
    )
    scheduler.add_job(
        job_twenty_messages,
        IntervalTrigger(minutes=settings.twenty_sync_minutes),
        id="twenty_messages",
        replace_existing=True,
    )
    scheduler.add_job(
        job_espo_emails,
        IntervalTrigger(minutes=settings.espo_sync_minutes),
        id="espo_emails",
        replace_existing=True,
    )
    scheduler.add_job(
        job_sync_mail,
        IntervalTrigger(minutes=settings.email_poll_minutes),
        id="sync_mail",
        replace_existing=True,
    )
    scheduler.add_job(
        job_sync_calendar,
        IntervalTrigger(minutes=15),
        id="sync_calendar",
        replace_existing=True,
    )
    scheduler.add_job(
        job_walk_in_intakes,
        IntervalTrigger(seconds=max(settings.pipeline_poll_seconds, 30)),
        id="walk_in_intakes",
        replace_existing=True,
    )
    scheduler.add_job(
        job_sync_work,
        IntervalTrigger(minutes=settings.servicetracker_sync_minutes),
        id="sync_work",
        replace_existing=True,
    )
    scheduler.add_job(
        job_daily_brief,
        CronTrigger(hour=settings.briefing_hour, minute=0),
        id="daily_brief",
        replace_existing=True,
    )
    return scheduler
