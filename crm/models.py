"""SQLAlchemy models: the CRM's memory."""
from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> dt.datetime:
    """Naive UTC.

    Every DateTime column in this schema is naive and every value in it is UTC.
    Mixing naive and aware datetimes is the classic way to get a TypeError deep
    in a comparison, so normalisation happens once, at the edges, via
    `to_naive_utc`.
    """
    return dt.datetime.now(dt.UTC).replace(tzinfo=None)


def to_naive_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Coerce any datetime into the naive-UTC convention used by the schema."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(dt.UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class InteractionKind(enum.StrEnum):
    call = "call"
    email = "email"
    note = "note"
    meeting = "meeting"
    sms = "sms"


class Direction(enum.StrEnum):
    inbound = "inbound"
    outbound = "outbound"
    internal = "internal"
    unknown = "unknown"


class RecordingStatus(enum.StrEnum):
    pending = "pending"
    transcribing = "transcribing"
    transcribed = "transcribed"
    analyzing = "analyzing"
    done = "done"
    failed = "failed"
    skipped = "skipped"


class NeedStatus(enum.StrEnum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    lost = "lost"


class TaskStatus(enum.StrEnum):
    open = "open"
    done = "done"
    dismissed = "dismissed"


class CallCategory(enum.StrEnum):
    """What the conversation was actually for.

    The shop is not only a service department: it sells boats, kayaks,
    sailboats and e-bikes, runs a boat club, stores and winterizes, and takes
    as many vendor and staff calls as customer ones. Only a few of these have
    anything to do with a work order, and routing on this is what stops a
    kayak enquiry attaching a note to somebody's open service ticket.
    """

    service = "service"          # work on a unit — the only common work-order path
    sales = "sales"              # wants to buy
    boat_club = "boat_club"      # membership, bookings, club questions
    rental = "rental"
    parts = "parts"              # parts and accessories
    storage = "storage"          # winter storage, shrink wrap, haul and launch
    billing = "billing"          # invoices, deposits, payments
    vendor = "vendor"            # suppliers, reps, freight, contractors
    internal = "internal"        # staff to staff
    personal = "personal"        # not shop business at all
    spam = "spam"                # robocall, telemarketing, scam
    other = "other"


class ProductLine(enum.StrEnum):
    """Which side of the business it concerns. Orthogonal to the category."""

    powerboat = "powerboat"
    sailboat = "sailboat"
    kayak = "kayak"
    paddleboard = "paddleboard"
    ebike = "ebike"
    trailer = "trailer"
    engine = "engine"
    apparel = "apparel"
    multiple = "multiple"
    none = "none"


class ContactKind(enum.StrEnum):
    customer = "customer"
    vendor = "vendor"
    staff = "staff"
    other = "other"


# Only these ever get looked at against an open work order. A boat-club
# booking or a rep chasing an invoice has no business writing to a job log.
WORK_ORDER_CATEGORIES = frozenset(
    {CallCategory.service, CallCategory.parts, CallCategory.storage, CallCategory.billing}
)

# Nothing is learned from these: no profile rewrite, no needs, no facts.
NO_PROFILE_CATEGORIES = frozenset({CallCategory.spam, CallCategory.internal})


class WorkSystem(enum.StrEnum):
    """Which shop app owns a work item."""

    servicetracker = "servicetracker"   # QuestWS/servicetracker — repair jobs
    winter = "winter"                   # QuestWS/winter-quotes — winter services


class NoteStatus(enum.StrEnum):
    pending = "pending"      # staged, waiting for a human to approve the push
    pushed = "pushed"
    dismissed = "dismissed"
    failed = "failed"


class IntakeStatus(enum.StrEnum):
    transcribing = "transcribing"
    draft = "draft"          # transcribed and extracted; not yet keyed into BiT
    linked = "linked"        # a job now exists in the service tracker for it
    discarded = "discarded"
    failed = "failed"


class FactCategory(enum.StrEnum):
    personal = "personal"      # kids' names, hobbies, hometown - the "know the person" half
    business = "business"      # role, company, decision authority, budget
    preference = "preference"  # "call me after 5", "hates email", "prefers texts"
    logistics = "logistics"    # address, gate code, site access, hours
    other = "other"


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #
class Contact(Base):
    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    display_name: Mapped[str] = mapped_column(String(255), default="Unknown caller")
    kind: Mapped[ContactKind] = mapped_column(
        Enum(ContactKind), default=ContactKind.customer, index=True
    )
    first_name: Mapped[str | None] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120))
    company: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list | None] = mapped_column(JSON, default=list)

    # AI-maintained rolling narrative: who this person is, what they care about.
    ai_profile: Mapped[str | None] = mapped_column(Text)
    ai_profile_updated_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    last_contacted_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    phones: Mapped[list[ContactPhone]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )
    emails: Mapped[list[ContactEmail]] = relationship(
        back_populates="contact", cascade="all, delete-orphan", lazy="selectin"
    )
    interactions: Mapped[list[Interaction]] = relationship(
        back_populates="contact", cascade="all, delete-orphan",
        order_by="Interaction.occurred_at.desc()",
    )
    facts: Mapped[list[Fact]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    needs: Mapped[list[Need]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    tasks: Mapped[list[Task]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )

    @property
    def primary_phone(self) -> str | None:
        return self.phones[0].e164 if self.phones else None

    @property
    def primary_email(self) -> str | None:
        return self.emails[0].address if self.emails else None


class ContactPhone(Base):
    __tablename__ = "contact_phones"
    __table_args__ = (UniqueConstraint("e164", name="uq_contact_phone_e164"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    e164: Mapped[str] = mapped_column(String(32), index=True)
    label: Mapped[str | None] = mapped_column(String(40))
    contact: Mapped[Contact] = relationship(back_populates="phones")


class ContactEmail(Base):
    __tablename__ = "contact_emails"
    __table_args__ = (UniqueConstraint("address", name="uq_contact_email_address"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    address: Mapped[str] = mapped_column(String(320), index=True)
    label: Mapped[str | None] = mapped_column(String(40))
    contact: Mapped[Contact] = relationship(back_populates="emails")


# --------------------------------------------------------------------------- #
# things that happened
# --------------------------------------------------------------------------- #
class Interaction(Base):
    """One call, one email thread message, one meeting, one note."""

    __tablename__ = "interactions"
    __table_args__ = (
        UniqueConstraint("source", "source_ref", name="uq_interaction_source_ref"),
        Index("ix_interaction_occurred", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[InteractionKind] = mapped_column(Enum(InteractionKind))
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction), default=Direction.unknown
    )
    occurred_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    duration_seconds: Mapped[int | None] = mapped_column(Integer)

    subject: Mapped[str | None] = mapped_column(String(500))
    body: Mapped[str | None] = mapped_column(Text)          # transcript or email body
    summary: Mapped[str | None] = mapped_column(Text)        # AI summary
    sentiment: Mapped[str | None] = mapped_column(String(40))
    outcome: Mapped[str | None] = mapped_column(Text)

    # provenance so we never double-import the same call/email
    source: Mapped[str] = mapped_column(String(40), default="manual")
    source_ref: Mapped[str | None] = mapped_column(String(255))
    meta: Mapped[dict | None] = mapped_column(JSON, default=dict)

    processed: Mapped[bool] = mapped_column(Boolean, default=False)

    # What this conversation was for, and which side of the business it
    # touched. Set by the analyst; `category` is what gates work-order linking.
    category: Mapped[CallCategory | None] = mapped_column(
        Enum(CallCategory), index=True
    )
    product_line: Mapped[ProductLine | None] = mapped_column(Enum(ProductLine))
    category_confidence: Mapped[float | None] = mapped_column(Float)

    # The open work order this conversation was about, when it could be worked
    # out. A string because service-tracker job ids are BiT invoice numbers.
    ticket_id: Mapped[str | None] = mapped_column(
        ForeignKey("service_tickets.id", ondelete="SET NULL"), index=True
    )

    contact: Mapped[Contact | None] = relationship(back_populates="interactions")
    recording: Mapped[Recording | None] = relationship(
        back_populates="interaction", uselist=False
    )


class Recording(Base):
    """A call recording file moving through transcribe -> analyze."""

    __tablename__ = "recordings"
    __table_args__ = (UniqueConstraint("sha256", name="uq_recording_sha256"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )

    path: Mapped[str] = mapped_column(String(1024))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    bytes: Mapped[int | None] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)

    from_number: Mapped[str | None] = mapped_column(String(32), index=True)
    to_number: Mapped[str | None] = mapped_column(String(32))
    direction: Mapped[Direction] = mapped_column(
        Enum(Direction), default=Direction.unknown
    )
    call_started_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    pbx_call_id: Mapped[str | None] = mapped_column(String(255), index=True)

    consent_announced: Mapped[bool] = mapped_column(Boolean, default=False)

    transcript: Mapped[str | None] = mapped_column(Text)
    transcript_engine: Mapped[str | None] = mapped_column(String(60))
    transcript_segments: Mapped[list | None] = mapped_column(JSON)

    status: Mapped[RecordingStatus] = mapped_column(
        Enum(RecordingStatus), default=RecordingStatus.pending, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)

    received_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    interaction: Mapped[Interaction | None] = relationship(back_populates="recording")


# --------------------------------------------------------------------------- #
# what we learned
# --------------------------------------------------------------------------- #
class Fact(Base):
    """A durable, attributable thing the person told us. The 'don't forget' store."""

    __tablename__ = "facts"
    __table_args__ = (Index("ix_fact_contact_cat", "contact_id", "category"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    category: Mapped[FactCategory] = mapped_column(
        Enum(FactCategory), default=FactCategory.other
    )
    text: Mapped[str] = mapped_column(Text)
    confidence: Mapped[float] = mapped_column(Float, default=0.7)
    quote: Mapped[str | None] = mapped_column(Text)  # what they actually said
    source_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    last_confirmed_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    archived: Mapped[bool] = mapped_column(Boolean, default=False)

    contact: Mapped[Contact] = relationship(back_populates="facts")


class Need(Base):
    """The job the customer is trying to get done. The 'good transaction' half."""

    __tablename__ = "needs"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id", ondelete="CASCADE"))
    title: Mapped[str] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[NeedStatus] = mapped_column(Enum(NeedStatus), default=NeedStatus.open)
    urgency: Mapped[str | None] = mapped_column(String(20))  # low | normal | high | urgent
    value_estimate: Mapped[str | None] = mapped_column(String(80))
    blockers: Mapped[str | None] = mapped_column(Text)
    source_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    opened_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    contact: Mapped[Contact] = relationship(back_populates="needs")


class Task(Base):
    """A commitment. Either you said you'd do it, or the AI thinks you should."""

    __tablename__ = "tasks"
    __table_args__ = (Index("ix_task_status_due", "status", "due_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE")
    )
    need_id: Mapped[int | None] = mapped_column(ForeignKey("needs.id", ondelete="SET NULL"))
    title: Mapped[str] = mapped_column(String(400))
    detail: Mapped[str | None] = mapped_column(Text)
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.open)
    created_by: Mapped[str] = mapped_column(String(20), default="ai")
    source_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)

    contact: Mapped[Contact | None] = relationship(back_populates="tasks")


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    title: Mapped[str] = mapped_column(String(400))
    description: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(String(500))
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False)
    google_event_id: Mapped[str | None] = mapped_column(String(255), index=True)
    sync_state: Mapped[str] = mapped_column(String(20), default="local")  # local|synced|error
    sync_error: Mapped[str | None] = mapped_column(Text)
    source_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class SyncState(Base):
    """Cursors for Gmail history ids, calendar sync tokens, folder watermarks."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# service tracker bridge
# --------------------------------------------------------------------------- #
class WorkItem(Base):
    """A local mirror of something open on a customer: a repair job in the
    service tracker, or a winter services quote.

    Read-only as far as these columns go — the owning app is the source of
    truth and the CRM re-reads on every sync. It exists here so a call can be
    matched without a network round trip, and so open work stays visible on a
    contact page when the shop apps are unreachable.

    The table is still called `service_tickets`: it was named when work orders
    were the only kind, and `interactions.ticket_id` carries a foreign key to
    it. Renaming would buy tidier SQL at the price of a migration that has to
    rebuild that reference, which is not a trade worth making.
    """

    __tablename__ = "service_tickets"
    __table_args__ = (
        UniqueConstraint("system", "remote_id", name="uq_work_item_remote"),
    )

    # "<system>:<remote id>" — the two apps number independently, so the ids
    # are namespaced rather than trusted not to collide.
    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    system: Mapped[WorkSystem] = mapped_column(
        Enum(WorkSystem), default=WorkSystem.servicetracker, index=True
    )
    # What the owning app calls it: a BiT invoice number, or a quote number.
    remote_id: Mapped[str] = mapped_column(String(64), index=True, default="")
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )

    customer_name: Mapped[str | None] = mapped_column(String(255))
    customer_phone: Mapped[str | None] = mapped_column(String(32), index=True)
    customer_email: Mapped[str | None] = mapped_column(String(320), index=True)
    boat_info: Mapped[str | None] = mapped_column(String(500))
    work_requested: Mapped[str | None] = mapped_column(Text)

    # Winter-only: where the unit is stored, and whether the season is closed.
    storage_location: Mapped[str | None] = mapped_column(String(120))
    season_done: Mapped[str | None] = mapped_column(String(40))
    balance: Mapped[str | None] = mapped_column(String(40))

    status: Mapped[str] = mapped_column(String(40), default="received")
    status_label: Mapped[str | None] = mapped_column(String(80))
    is_open: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    alert: Mapped[str | None] = mapped_column(Text)
    amount_due: Mapped[float | None] = mapped_column(Float)
    entry_count: Mapped[int] = mapped_column(Integer, default=0)
    minutes_total: Mapped[int] = mapped_column(Integer, default=0)

    remote_created_at: Mapped[str | None] = mapped_column(String(40))
    remote_updated_at: Mapped[str | None] = mapped_column(String(40))
    synced_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)


class TicketNote(Base):
    """A note staged from a conversation, waiting to be pushed onto a job.

    Nothing reaches the shop's job log without a person pressing the button,
    matching how the service tracker treats every other outbound action.
    """

    __tablename__ = "ticket_notes"
    __table_args__ = (
        UniqueConstraint("ticket_id", "source_interaction_id", name="uq_ticket_note_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ticket_id: Mapped[str] = mapped_column(
        ForeignKey("service_tickets.id", ondelete="CASCADE"), index=True
    )
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    source_interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="CASCADE")
    )

    body: Mapped[str] = mapped_column(Text)
    status: Mapped[NoteStatus] = mapped_column(
        Enum(NoteStatus), default=NoteStatus.pending, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
    pushed_at: Mapped[dt.datetime | None] = mapped_column(DateTime)


class WalkInIntake(Base):
    """A customer standing at the counter, recorded on the way in.

    Produces a draft the service writer keys into BiT - it never becomes a job
    by itself, because a job id *is* a BiT invoice number and BiT is never
    integrated with. Same rule the service tracker keeps.
    """

    __tablename__ = "walk_in_intakes"

    id: Mapped[int] = mapped_column(primary_key=True)
    contact_id: Mapped[int | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL"), index=True
    )
    recording_id: Mapped[int | None] = mapped_column(
        ForeignKey("recordings.id", ondelete="SET NULL")
    )
    interaction_id: Mapped[int | None] = mapped_column(
        ForeignKey("interactions.id", ondelete="SET NULL")
    )

    customer_name: Mapped[str | None] = mapped_column(String(255))
    customer_phone: Mapped[str | None] = mapped_column(String(32))
    customer_email: Mapped[str | None] = mapped_column(String(320))
    boat_info: Mapped[str | None] = mapped_column(String(500))
    work_requested: Mapped[str | None] = mapped_column(Text)
    requested_items: Mapped[list | None] = mapped_column(JSON, default=list)
    urgency: Mapped[str | None] = mapped_column(String(20))
    promised_date: Mapped[str | None] = mapped_column(String(80))
    customer_said: Mapped[str | None] = mapped_column(Text)
    open_questions: Mapped[list | None] = mapped_column(JSON, default=list)

    transcript: Mapped[str | None] = mapped_column(Text)
    status: Mapped[IntakeStatus] = mapped_column(
        Enum(IntakeStatus), default=IntakeStatus.transcribing, index=True
    )
    error: Mapped[str | None] = mapped_column(Text)
    # Set once the writer has created the matching job in BiT + service tracker.
    linked_ticket_id: Mapped[str | None] = mapped_column(String(64))
    taken_by: Mapped[str | None] = mapped_column(String(120))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=utcnow)
