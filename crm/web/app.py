"""FastAPI app: the dashboard, the contact pages, and the PBX webhook."""
from __future__ import annotations

import datetime as dt
import hmac
import logging
import shutil
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.db import get_session, init_db, session_scope
from crm.models import (
    Appointment,
    Contact,
    ContactEmail,
    ContactPhone,
    Direction,
    Fact,
    IntakeStatus,
    Interaction,
    Need,
    NeedStatus,
    NoteStatus,
    Recording,
    ServiceTicket,
    Task,
    TaskStatus,
    TicketNote,
    WalkInIntake,
    utcnow,
)
from crm.services import briefing
from crm.services import intake as intake_service
from crm.services import tickets as ticket_service
from crm.services.identity import merge_contacts, pretty_phone

log = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.filters["phone"] = pretty_phone

@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    from crm.worker.scheduler import build_scheduler

    scheduler = build_scheduler()
    scheduler.start()
    log.info("background scheduler started")
    try:
        yield
    finally:
        scheduler.shutdown(wait=False)


app = FastAPI(title="AI CRM", docs_url="/api/docs", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def render(request: Request, template: str, **context) -> HTMLResponse:
    context.setdefault("settings", settings)
    context.setdefault("now", utcnow())
    return templates.TemplateResponse(request, template, context)


# --------------------------------------------------------------------------- #
# dashboard
# --------------------------------------------------------------------------- #
@app.get("/", response_class=HTMLResponse)
def home(request: Request, s: Session = Depends(get_session)):
    return render(request, "dashboard.html", **briefing.dashboard(s))


@app.get("/brief", response_class=HTMLResponse)
def brief_page(request: Request, s: Session = Depends(get_session)):
    return render(request, "brief.html", brief=briefing.generate_brief(s))


# --------------------------------------------------------------------------- #
# contacts
# --------------------------------------------------------------------------- #
@app.get("/contacts", response_class=HTMLResponse)
def contact_list(request: Request, q: str = "", s: Session = Depends(get_session)):
    stmt = select(Contact)
    if q:
        like = f"%{q}%"
        stmt = (
            stmt.outerjoin(ContactPhone)
            .outerjoin(ContactEmail)
            .where(
                or_(
                    Contact.display_name.ilike(like),
                    Contact.company.ilike(like),
                    Contact.ai_profile.ilike(like),
                    ContactPhone.e164.ilike(like),
                    ContactEmail.address.ilike(like),
                )
            )
            .distinct()
        )
    contacts = list(
        s.scalars(stmt.order_by(Contact.last_contacted_at.desc().nulls_last()).limit(200))
    )
    return render(request, "contacts.html", contacts=contacts, q=q)


@app.get("/contacts/{contact_id}", response_class=HTMLResponse)
def contact_detail(contact_id: int, request: Request, s: Session = Depends(get_session)):
    contact = s.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(404, "No such contact")

    facts = list(
        s.scalars(
            select(Fact)
            .where(Fact.contact_id == contact_id, Fact.archived.is_(False))
            .order_by(Fact.category, Fact.last_confirmed_at.desc())
        )
    )
    grouped: dict[str, list[Fact]] = {}
    for fact in facts:
        grouped.setdefault(fact.category.value, []).append(fact)

    return render(
        request,
        "contact.html",
        contact=contact,
        facts_by_category=grouped,
        needs=list(
            s.scalars(
                select(Need)
                .where(Need.contact_id == contact_id)
                .order_by(Need.opened_at.desc())
            )
        ),
        tasks=list(
            s.scalars(
                select(Task)
                .where(Task.contact_id == contact_id, Task.status == TaskStatus.open)
                .order_by(Task.due_at.is_(None), Task.due_at)
            )
        ),
        interactions=list(
            s.scalars(
                select(Interaction)
                .where(Interaction.contact_id == contact_id)
                .order_by(Interaction.occurred_at.desc())
                .limit(50)
            )
        ),
        appointments=list(
            s.scalars(
                select(Appointment)
                .where(Appointment.contact_id == contact_id)
                .order_by(Appointment.starts_at.desc())
            )
        ),
    )


@app.post("/contacts/{contact_id}/refresh-profile")
def refresh_profile_route(contact_id: int, s: Session = Depends(get_session)):
    from crm.services.profile import refresh_profile

    contact = s.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(404, "No such contact")
    refresh_profile(s, contact)
    s.commit()
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


@app.post("/contacts/{contact_id}/note")
def add_note(
    contact_id: int,
    body: str = Form(...),
    s: Session = Depends(get_session),
):
    from crm.models import Direction, InteractionKind

    contact = s.get(Contact, contact_id)
    if contact is None:
        raise HTTPException(404, "No such contact")
    s.add(
        Interaction(
            contact_id=contact_id,
            kind=InteractionKind.note,
            direction=Direction.internal,
            occurred_at=utcnow(),
            subject="Note",
            body=body,
            source="manual",
            source_ref=f"note-{utcnow().timestamp()}",
            processed=True,
        )
    )
    s.commit()
    return RedirectResponse(f"/contacts/{contact_id}", status_code=303)


@app.post("/contacts/{keep_id}/merge/{merge_id}")
def merge_route(keep_id: int, merge_id: int, s: Session = Depends(get_session)):
    try:
        merge_contacts(s, keep_id, merge_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    s.commit()
    return RedirectResponse(f"/contacts/{keep_id}", status_code=303)


# --------------------------------------------------------------------------- #
# tasks & appointments
# --------------------------------------------------------------------------- #
@app.get("/tasks", response_class=HTMLResponse)
def task_list(request: Request, s: Session = Depends(get_session)):
    return render(
        request,
        "tasks.html",
        tasks=briefing.open_tasks(s, limit=200),
        overdue=briefing.overdue_tasks(s),
    )


@app.post("/tasks/{task_id}/done")
def task_done(task_id: int, request: Request, s: Session = Depends(get_session)):
    task = s.get(Task, task_id)
    if task is None:
        raise HTTPException(404, "No such task")
    task.status = TaskStatus.done
    task.completed_at = utcnow()
    s.commit()
    return RedirectResponse(
        request.headers.get("referer", "/tasks"), status_code=303
    )


@app.post("/tasks")
def task_create(
    title: str = Form(...),
    contact_id: int | None = Form(None),
    due_at: str | None = Form(None),
    s: Session = Depends(get_session),
):
    parsed = None
    if due_at:
        try:
            parsed = dt.datetime.fromisoformat(due_at)
        except ValueError:
            raise HTTPException(400, f"Could not read due date {due_at!r}") from None
    s.add(Task(title=title, contact_id=contact_id, due_at=parsed, created_by="user"))
    s.commit()
    return RedirectResponse("/tasks", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request, s: Session = Depends(get_session)):
    upcoming = list(
        s.scalars(
            select(Appointment)
            .where(Appointment.starts_at >= utcnow() - dt.timedelta(days=1))
            .order_by(Appointment.starts_at)
        )
    )
    return render(request, "calendar.html", appointments=upcoming)


@app.post("/appointments/{appointment_id}/sync")
def appointment_sync(appointment_id: int, s: Session = Depends(get_session)):
    from crm.ingest.gcal import push_appointment

    appt = s.get(Appointment, appointment_id)
    if appt is None:
        raise HTTPException(404, "No such appointment")
    try:
        push_appointment(s, appt)
    except Exception as exc:
        raise HTTPException(502, f"Calendar sync failed: {exc}") from exc
    s.commit()
    return RedirectResponse("/calendar", status_code=303)


# --------------------------------------------------------------------------- #
# calls
# --------------------------------------------------------------------------- #
@app.get("/calls", response_class=HTMLResponse)
def call_list(request: Request, s: Session = Depends(get_session)):
    recordings = list(
        s.scalars(select(Recording).order_by(Recording.received_at.desc()).limit(100))
    )
    return render(request, "calls.html", recordings=recordings)


@app.get("/calls/{recording_id}", response_class=HTMLResponse)
def call_detail(recording_id: int, request: Request, s: Session = Depends(get_session)):
    rec = s.get(Recording, recording_id)
    if rec is None:
        raise HTTPException(404, "No such recording")
    interaction = s.get(Interaction, rec.interaction_id) if rec.interaction_id else None
    contact = (
        s.get(Contact, interaction.contact_id)
        if interaction and interaction.contact_id
        else None
    )
    return render(
        request, "call.html", recording=rec, interaction=interaction, contact=contact
    )


def _process_recording_bg(recording_id: int) -> None:
    from crm.services.pipeline import PipelineError, process_recording

    with session_scope() as s:
        rec = s.get(Recording, recording_id)
        if rec is None:
            return
        try:
            process_recording(s, rec)
        except PipelineError as exc:
            log.warning("recording %s stalled: %s", recording_id, exc)
        except Exception:
            log.exception("recording %s failed", recording_id)


@app.post("/upload")
async def upload_recording(
    background: BackgroundTasks,
    file: UploadFile,
    from_number: str | None = Form(None),
    to_number: str | None = Form(None),
    s: Session = Depends(get_session),
):
    from crm.ingest.sangoma import register_recording

    # Keep the original filename: PBX recording names carry the caller, the
    # direction and the timestamp, and that is our metadata of last resort.
    name = Path(file.filename or "upload.wav").name
    with tempfile.TemporaryDirectory(prefix="crm-upload-") as tmpdir:
        staged = Path(tmpdir) / name
        with staged.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        rec, created = register_recording(
            s, staged, from_number=from_number, to_number=to_number
        )
        s.commit()

    if created:
        background.add_task(_process_recording_bg, rec.id)
    return RedirectResponse(f"/calls/{rec.id}", status_code=303)


@app.post("/api/sangoma/recording")
async def sangoma_webhook(
    request: Request,
    background: BackgroundTasks,
    x_webhook_secret: str | None = Header(None),
    s: Session = Depends(get_session),
):
    """Recording notification from the PBX.

    Accepts JSON metadata with either a path we can read or a URL we can fetch.
    See docs/sangoma-setup.md for the shim that posts this.
    """
    if settings.sangoma_webhook_secret:
        if not x_webhook_secret or not hmac.compare_digest(
            x_webhook_secret, settings.sangoma_webhook_secret
        ):
            raise HTTPException(401, "Bad or missing X-Webhook-Secret")

    from crm.ingest.sangoma import normalize_webhook_payload, register_recording

    payload = await request.json()
    meta = normalize_webhook_payload(payload)

    source: Path | None = None
    tmp_path: Path | None = None
    if meta["recording_path"] and Path(meta["recording_path"]).is_file():
        source = Path(meta["recording_path"])
    elif meta["recording_url"]:
        import httpx

        try:
            resp = httpx.get(meta["recording_url"], timeout=300, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"Could not fetch recording: {exc}") from exc
        suffix = Path(meta["recording_url"].split("?")[0]).suffix or ".wav"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            tmp.write(resp.content)
            tmp_path = source = Path(tmp.name)

    if source is None:
        raise HTTPException(
            400, "Payload has neither a readable recording_path nor a recording_url"
        )

    try:
        rec, created = register_recording(
            s,
            source,
            from_number=meta["from_number"],
            to_number=meta["to_number"],
            direction=meta["direction"],
            call_started_at=meta["call_started_at"],
            pbx_call_id=meta["pbx_call_id"],
            consent_announced=meta["consent_announced"],
        )
        if meta["duration_seconds"]:
            try:
                rec.duration_seconds = float(meta["duration_seconds"])
            except (TypeError, ValueError):
                pass
        s.commit()
    finally:
        if tmp_path:
            tmp_path.unlink(missing_ok=True)

    if created:
        background.add_task(_process_recording_bg, rec.id)
    return JSONResponse(
        {"recording_id": rec.id, "created": created, "status": rec.status.value}
    )


@app.get("/api/health")
def health(s: Session = Depends(get_session)):
    return {
        "ok": True,
        "ai": bool(settings.anthropic_api_key),
        "imap": settings.imap_configured,
        "queue": briefing.unprocessed_counts(s),
    }


# --------------------------------------------------------------------------- #
# needs
# --------------------------------------------------------------------------- #
@app.post("/needs/{need_id}/status")
def need_status(
    need_id: int,
    request: Request,
    status: str = Form(...),
    s: Session = Depends(get_session),
):
    need = s.get(Need, need_id)
    if need is None:
        raise HTTPException(404, "No such need")
    try:
        need.status = NeedStatus(status)
    except ValueError:
        raise HTTPException(400, f"Unknown status {status!r}") from None
    if need.status == NeedStatus.resolved:
        need.resolved_at = utcnow()
    s.commit()
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


# --------------------------------------------------------------------------- #
# walk-in intake — the record button at the counter
# --------------------------------------------------------------------------- #
@app.get("/intake", response_class=HTMLResponse)
def intake_page(request: Request, s: Session = Depends(get_session)):
    return render(request, "intake.html", intakes=intake_service.recent_intakes(s))


def _process_intake_bg(intake_id: int) -> None:
    with session_scope() as s:
        intake = s.get(WalkInIntake, intake_id)
        if intake is not None:
            intake_service.process_intake(s, intake)


@app.post("/api/intake")
async def intake_upload(
    background: BackgroundTasks,
    file: UploadFile,
    taken_by: str | None = Form(None),
    s: Session = Depends(get_session),
):
    """Audio straight off the counter recorder."""
    from crm.ingest.sangoma import register_recording

    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    name = Path(file.filename or f"walkin-{stamp}.webm").name
    with tempfile.TemporaryDirectory(prefix="crm-intake-") as tmpdir:
        staged = Path(tmpdir) / name
        with staged.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        if staged.stat().st_size == 0:
            raise HTTPException(400, "The recording came through empty.")
        recording, created = register_recording(
            s, staged, direction=Direction.internal, call_started_at=utcnow()
        )
        if not created and recording.interaction_id:
            raise HTTPException(409, "That recording has already been processed.")
        record = intake_service.create_intake(s, recording, taken_by=taken_by)
        s.commit()
        intake_id = record.id

    background.add_task(_process_intake_bg, intake_id)
    return JSONResponse({"intake_id": intake_id, "status": "transcribing"})


@app.get("/intake/{intake_id}", response_class=HTMLResponse)
def intake_detail(intake_id: int, request: Request, s: Session = Depends(get_session)):
    record = s.get(WalkInIntake, intake_id)
    if record is None:
        raise HTTPException(404, "No such intake")
    contact = s.get(Contact, record.contact_id) if record.contact_id else None
    return render(
        request,
        "intake_detail.html",
        intake=record,
        contact=contact,
        work_order=intake_service.work_order_text(record),
        open_tickets=ticket_service.open_tickets_for(s, contact),
    )


@app.get("/api/intake/{intake_id}")
def intake_status(intake_id: int, s: Session = Depends(get_session)):
    """Polled by the counter page while the draft is being written."""
    record = s.get(WalkInIntake, intake_id)
    if record is None:
        raise HTTPException(404, "No such intake")
    return {
        "id": record.id,
        "status": record.status.value,
        "error": record.error,
        "boat_info": record.boat_info,
        "customer_name": record.customer_name,
    }


@app.post("/intake/{intake_id}/link")
def intake_link(
    intake_id: int,
    ticket_id: str = Form(...),
    s: Session = Depends(get_session),
):
    """Record which work order the writer keyed this draft into."""
    record = s.get(WalkInIntake, intake_id)
    if record is None:
        raise HTTPException(404, "No such intake")
    job_id = ticket_id.strip().upper()
    if not job_id:
        raise HTTPException(400, "Enter the work order number.")
    record.linked_ticket_id = job_id
    record.status = IntakeStatus.linked
    if record.interaction_id and s.get(ServiceTicket, job_id):
        interaction = s.get(Interaction, record.interaction_id)
        if interaction is not None:
            interaction.ticket_id = job_id
    s.commit()
    return RedirectResponse(f"/intake/{intake_id}", status_code=303)


@app.post("/intake/{intake_id}/discard")
def intake_discard(intake_id: int, s: Session = Depends(get_session)):
    record = s.get(WalkInIntake, intake_id)
    if record is None:
        raise HTTPException(404, "No such intake")
    record.status = IntakeStatus.discarded
    s.commit()
    return RedirectResponse("/intake", status_code=303)


# --------------------------------------------------------------------------- #
# service tickets
# --------------------------------------------------------------------------- #
@app.get("/tickets", response_class=HTMLResponse)
def ticket_page(request: Request, s: Session = Depends(get_session)):
    tickets = list(
        s.scalars(
            select(ServiceTicket)
            .where(ServiceTicket.is_open.is_(True))
            .order_by(ServiceTicket.remote_updated_at.desc())
        )
    )
    return render(
        request,
        "tickets.html",
        tickets=tickets,
        notes=ticket_service.pending_notes(s),
        configured=settings.servicetracker_configured,
    )


@app.post("/tickets/sync")
def ticket_sync(s: Session = Depends(get_session)):
    from crm.integrations.servicetracker import ServiceTrackerError

    try:
        ticket_service.sync_tickets(s)
    except ServiceTrackerError as exc:
        raise HTTPException(502, str(exc)) from exc
    s.commit()
    return RedirectResponse("/tickets", status_code=303)


@app.post("/ticket-notes/{note_id}/push")
def ticket_note_push(note_id: int, request: Request, s: Session = Depends(get_session)):
    from crm.integrations.servicetracker import ServiceTrackerError

    note = s.get(TicketNote, note_id)
    if note is None:
        raise HTTPException(404, "No such note")
    try:
        ticket_service.push_note(s, note)
    except ServiceTrackerError as exc:
        s.commit()
        raise HTTPException(502, str(exc)) from exc
    s.commit()
    return RedirectResponse(request.headers.get("referer", "/tickets"), status_code=303)


@app.post("/ticket-notes/{note_id}/dismiss")
def ticket_note_dismiss(note_id: int, request: Request, s: Session = Depends(get_session)):
    note = s.get(TicketNote, note_id)
    if note is None:
        raise HTTPException(404, "No such note")
    note.status = NoteStatus.dismissed
    s.commit()
    return RedirectResponse(request.headers.get("referer", "/tickets"), status_code=303)
