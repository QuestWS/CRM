"""Command line entry points: python -m crm.cli <command>."""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from crm.config import settings
from crm.db import init_db, session_scope


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


# --------------------------------------------------------------------------- #
def cmd_init(_args) -> int:
    init_db()
    print(f"Database ready at {settings.database_url}")
    print(f"Recordings will be stored under {settings.recordings_dir}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    uvicorn.run(
        "crm.web.app:app",
        host=args.host or settings.host,
        port=args.port or settings.port,
        reload=args.reload,
    )
    return 0


def cmd_worker(_args) -> int:
    """Run the background jobs without the web server."""
    import time

    from crm.worker.scheduler import build_scheduler

    scheduler = build_scheduler()
    scheduler.start()
    print("Worker running. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        scheduler.shutdown()
    return 0


def cmd_import_calls(args) -> int:
    from crm.services.pipeline import import_and_process

    directory = Path(args.directory) if args.directory else None
    with session_scope() as s:
        stats = import_and_process(s, directory)
    print(f"Imported {stats.get('imported', 0)}, processed {stats['processed']}, "
          f"failed {stats['failed']}")
    return 0


def cmd_add_call(args) -> int:
    """Register and process one recording file by hand."""
    from crm.ingest.sangoma import register_recording
    from crm.services.pipeline import PipelineError, process_recording

    with session_scope() as s:
        rec, created = register_recording(
            s, Path(args.path), from_number=args.from_number, to_number=args.to_number
        )
        if not created:
            print(f"Already imported as recording {rec.id} (status: {rec.status.value})")
        s.commit()
        try:
            interaction = process_recording(s, rec)
        except PipelineError as exc:
            print(f"Stopped: {exc}", file=sys.stderr)
            return 1
    print(f"Recording {rec.id} -> interaction {interaction.id}")
    print(f"\n{interaction.summary}")
    return 0


def cmd_sync_mail(_args) -> int:
    from crm.ingest.imap_mail import ImapNotConfigured, sync_mail
    from crm.services.pipeline import run_unprocessed_emails

    try:
        with session_scope() as s:
            added = sync_mail(s)
    except ImapNotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 1
    for folder, count in added.items():
        print(f"{folder}: {count} new")
    with session_scope() as s:
        stats = run_unprocessed_emails(s)
    print(f"Analysed {stats['processed']} email(s), {stats['failed']} failed")
    return 0


def cmd_check_mail(_args) -> int:
    """Connect to IMAP and report what will be synced. Reads nothing."""
    from crm.ingest.imap_mail import (
        ImapNotConfigured,
        connect,
        discover_folders,
        list_folders,
    )

    try:
        with connect() as conn:
            print(f"Connected to {settings.imap_host} as {settings.imap_username}\n")
            print("Folders on the server:")
            for name, flags in list_folders(conn):
                marker = " <- SENT" if "\\sent" in flags else ""
                print(f"  {name}{marker}")
            chosen = discover_folders(conn)
            print(f"\nWill sync: {', '.join(chosen)}")
            if len(chosen) < 2:
                print(
                    "\nWarning: no Sent folder detected. Set IMAP_FOLDERS to include "
                    "it, or the CRM will only ever see one side of your conversations."
                )
    except ImapNotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 1
    mine = ", ".join(settings.operator_email_list) or "(none set - set OPERATOR_EMAIL)"
    print(f"\nTreating these addresses as you: {mine}")
    return 0


def cmd_sync_tickets(_args) -> int:
    from crm.services.tickets import pending_notes, sync_all

    if not (settings.servicetracker_configured or settings.winter_configured):
        print(
            "Neither shop app is configured. Set SERVICETRACKER_* and/or WINTER_* "
            "in .env — see docs/servicetracker-integration.md.",
            file=sys.stderr,
        )
        return 1

    with session_scope() as s:
        stats = sync_all(s)
        waiting = len(pending_notes(s))

    failed = False
    for system, result in stats.items():
        if "error" in result:
            print(f"{system}: FAILED — {result['error']}", file=sys.stderr)
            failed = True
            continue
        extra = f", {result['lookups']} lookup(s)" if result.get("lookups") else ""
        print(
            f"{system}: {result['created']} new, {result['updated']} updated, "
            f"{result['linked']} linked{extra}"
        )
    if waiting:
        print(f"\n{waiting} note(s) staged — review them at /tickets")
    return 1 if failed else 0


def cmd_intake(args) -> int:
    """Process a counter recording from a file, for testing without a browser."""
    from crm.ingest.sangoma import register_recording
    from crm.models import Direction
    from crm.services.intake import create_intake, process_intake, work_order_text

    with session_scope() as s:
        recording, _ = register_recording(
            s, Path(args.path), direction=Direction.internal
        )
        record = create_intake(s, recording, taken_by=args.taken_by)
        s.commit()
        result = process_intake(s, record)
        if result.error:
            print(f"Stopped: {result.error}", file=sys.stderr)
            return 1
        print(work_order_text(result))
        if result.open_questions:
            print("\nStill to ask:")
            for question in result.open_questions:
                print(f"  - {question}")
    return 0


def cmd_google_auth(_args) -> int:
    from crm.ingest.google_auth import GoogleNotConfigured, authorize_interactive

    try:
        path = authorize_interactive()
    except GoogleNotConfigured as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"Calendar access authorised. Token stored at {path}")
    return 0


def cmd_brief(args) -> int:
    from crm.services.briefing import build_brief_context, generate_brief

    with session_scope() as s:
        if args.raw:
            print(build_brief_context(s))
            return 0
        brief = generate_brief(s)
    if brief is None:
        print("Could not generate a brief (is ANTHROPIC_API_KEY set?)", file=sys.stderr)
        return 1
    print(brief.headline + "\n")
    print("Today:")
    for item in brief.priorities:
        print(f"  - {item}")
    if brief.waiting_on_others:
        print("\nWaiting on others:")
        for item in brief.waiting_on_others:
            print(f"  - {item}")
    if brief.at_risk:
        print("\nAt risk:")
        for item in brief.at_risk:
            print(f"  - {item}")
    return 0


def cmd_reprocess(args) -> int:
    """Re-run analysis on one contact's interactions after a prompt change."""
    from crm.models import Contact
    from crm.services.profile import refresh_profile

    with session_scope() as s:
        contact = s.get(Contact, args.contact_id)
        if contact is None:
            print(f"No contact {args.contact_id}", file=sys.stderr)
            return 1
        if refresh_profile(s, contact):
            print(f"Profile rewritten for {contact.display_name}:\n")
            print(contact.ai_profile)
            return 0
    print("Profile refresh unavailable (is ANTHROPIC_API_KEY set?)", file=sys.stderr)
    return 1


def cmd_doctor(_args) -> int:
    """Check that every moving part is configured."""
    from crm.ingest.google_auth import is_configured as google_ready
    from crm.transcription.base import have_ffmpeg

    checks = [
        ("Database", True, settings.database_url),
        ("Anthropic API key", bool(settings.anthropic_api_key),
         "set" if settings.anthropic_api_key else "missing - no summaries or profiles"),
        ("Operator identity", bool(settings.operator_email_list),
         ", ".join(settings.operator_email_list) or "set OPERATOR_EMAIL"),
        ("Operator phone numbers", bool(settings.operator_number_list),
         ", ".join(settings.operator_number_list) or "set OPERATOR_NUMBERS"),
        ("IMAP", settings.imap_configured,
         f"{settings.imap_username}@{settings.imap_host}" if settings.imap_configured
         else "missing - no email ingest"),
        ("Google Calendar", google_ready(),
         "authorised" if google_ready() else "run: python -m crm.cli google-auth"),
        ("ffmpeg", have_ffmpeg(),
         "found" if have_ffmpeg() else "missing - stereo calls won't be split per speaker"),
        ("Service tracker", settings.servicetracker_configured,
         settings.servicetracker_exec_url or "not connected - no work order matching"),
        ("Winter services", settings.winter_configured,
         settings.winter_exec_url or "not connected - no winter quote matching"),
        ("Transcription engine", True,
         f"{settings.transcription_engine}"
         + ("" if settings.transcription_engine != "assemblyai"
            or settings.assemblyai_api_key else " (ASSEMBLYAI_API_KEY missing)")),
        ("Recording watch folder", bool(settings.sangoma_watch_dir),
         str(settings.sangoma_watch_dir) if settings.sangoma_watch_dir
         else "not set - webhook and manual upload still work"),
    ]
    failed = 0
    for name, ok, detail in checks:
        print(f"  {'PASS' if ok else 'WARN'}  {name:<26} {detail}")
        if not ok:
            failed += 1
    print(f"\n{len(checks) - failed}/{len(checks)} checks passing.")
    return 0


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crm", description="AI CRM for calls and email")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database").set_defaults(func=cmd_init)
    sub.add_parser("doctor", help="check configuration").set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="run the web app")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)

    sub.add_parser("worker", help="run background jobs only").set_defaults(func=cmd_worker)

    p = sub.add_parser("import-calls", help="scan the watch folder and process recordings")
    p.add_argument("--directory")
    p.set_defaults(func=cmd_import_calls)

    p = sub.add_parser("add-call", help="process a single recording file")
    p.add_argument("path")
    p.add_argument("--from-number")
    p.add_argument("--to-number")
    p.set_defaults(func=cmd_add_call)

    sub.add_parser("sync-mail", help="pull new mail over IMAP and analyse it").set_defaults(
        func=cmd_sync_mail
    )
    sub.add_parser("check-mail", help="test IMAP and list folders").set_defaults(
        func=cmd_check_mail
    )
    sub.add_parser("google-auth", help="authorise Google Calendar").set_defaults(
        func=cmd_google_auth
    )
    sub.add_parser(
        "sync-work", help="pull open work orders and winter quotes from the shop apps"
    ).set_defaults(func=cmd_sync_tickets)

    p = sub.add_parser("intake", help="process a counter recording into a draft")
    p.add_argument("path")
    p.add_argument("--taken-by")
    p.set_defaults(func=cmd_intake)

    p = sub.add_parser("brief", help="print the daily brief")
    p.add_argument("--raw", action="store_true", help="show the context, not the brief")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("refresh-profile", help="rewrite one contact's profile")
    p.add_argument("contact_id", type=int)
    p.set_defaults(func=cmd_reprocess)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    init_db()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
