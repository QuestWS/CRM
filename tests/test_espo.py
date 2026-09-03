"""EspoCRM client and the email sync, against a stubbed API."""
from __future__ import annotations

import pytest

from crm.ai.schemas import (
    ConversationAnalysis,
    ExtractedFact,
    ExtractedNeed,
    ExtractedPerson,
    ExtractedTask,
    TicketUpdate,
)
from crm.integrations.espocrm import EspoClient, EspoError, encode_where
from crm.services import espo_sync

EMAIL_STUB = {
    "id": "e1",
    "name": "Winterizing question",
    "status": "Archived",
    "dateSent": "2026-09-01 14:30:00",
    "from": "dave@example.com",
    "to": "chris@questwatersports.com",
    "parentType": None,
    "parentId": None,
    "nameHash": {"dave@example.com": "Dave Mercer"},
}

EMAIL_FULL = dict(
    EMAIL_STUB,
    bodyPlain="Hi Chris — when can you get the Malibu in for winterizing? "
    "Also my daughter's wedding is the 20th so I'd like it done before.",
)

ANALYSIS = ConversationAnalysis(
    category="storage",
    category_confidence=0.9,
    product_line="powerboat",
    contact_kind="customer",
    summary="Dave asked when the Malibu can come in for winterizing.",
    outcome="Needs a date offered.",
    person=ExtractedPerson(full_name="Dave Mercer", name_confidence=0.9),
    facts=[ExtractedFact(category="personal", text="Daughter's wedding is the 20th")],
    needs=[ExtractedNeed(title="Winterizing", description="Wants the Malibu in.")],
    tasks=[
        ExtractedTask(title="Offer Dave a winterizing date", owner="me", priority="high"),
        ExtractedTask(title="Send us the hull number", owner="them"),
    ],
)


class FakeEspo:
    """Mimics the shapes the real API returns, including its list envelope."""

    def __init__(self, emails=None, contacts=None):
        self.emails = emails if emails is not None else [dict(EMAIL_STUB)]
        self.contacts = contacts if contacts is not None else [
            {"id": "c1", "name": "Dave Mercer", "emailAddress": "dave@example.com",
             "phoneNumber": "+18155551234"}
        ]
        self.notes: list[dict] = []
        self.tasks: list[dict] = []
        self.configured = True
        self.base_url = "http://espo.test/"

    def recent_emails(self, since=None, limit=50, folder=None):
        return [dict(e) for e in self.emails]

    def email_body(self, email_id):
        return dict(EMAIL_FULL)

    def get(self, entity, record_id):
        if entity == "Contact":
            for c in self.contacts:
                if c["id"] == record_id:
                    return dict(c)
        raise EspoError("not found")

    def find_by_email(self, entity, address):
        if entity != "Contact":
            return None
        return next((dict(c) for c in self.contacts if c["emailAddress"] == address), None)

    def list(self, entity, **kwargs):
        return {"total": 0, "list": []}

    def post_note(self, parent_type, parent_id, text, internal=False):
        note = {"parentType": parent_type, "parentId": parent_id,
                "post": text, "isInternal": internal}
        self.notes.append(note)
        return note

    def create_task(self, name, **kwargs):
        task = {"name": name, **kwargs}
        self.tasks.append(task)
        return task

    def health(self):
        return {"user": {"userName": "api"}}


@pytest.fixture
def espo(monkeypatch):
    client = FakeEspo()
    monkeypatch.setattr(espo_sync.espocrm, "get_client", lambda: client)
    monkeypatch.setattr(espo_sync.enrich, "analyze_conversation", lambda **kw: ANALYSIS)
    return client


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #
def test_where_clauses_use_the_bracketed_form_espo_parses():
    got = encode_where([
        {"type": "after", "attribute": "dateSent", "value": "2026-09-01 00:00:00"},
        {"type": "in", "attribute": "id", "value": ["a", "b"]},
    ])
    assert got["where[0][type]"] == "after"
    assert got["where[0][attribute]"] == "dateSent"
    assert got["where[1][value][]"] == ["a", "b"]


def test_api_root_is_derived_from_the_site_url():
    assert EspoClient("http://espo.test", "k").api_root == "http://espo.test/api/v1/"
    assert EspoClient("http://espo.test/", "k").api_root == "http://espo.test/api/v1/"


def test_unconfigured_client_says_where_the_key_comes_from():
    from crm.integrations.espocrm import EspoNotConfigured

    with pytest.raises(EspoNotConfigured, match="API Users"):
        EspoClient("", "").health()


def test_empty_note_is_refused():
    client = EspoClient("http://espo.test", "k")
    with pytest.raises(EspoError, match="empty"):
        client.post_note("Contact", "c1", "   ")


# --------------------------------------------------------------------------- #
# direction and matching
# --------------------------------------------------------------------------- #
def test_sent_status_is_outbound():
    assert espo_sync._direction({"status": "Sent"}) == "outbound"
    assert espo_sync._direction({"status": "Archived"}) == "inbound"


def test_counterparty_is_the_far_end_in_each_direction():
    inbound = dict(EMAIL_STUB)
    outbound = dict(EMAIL_STUB, status="Sent")
    assert espo_sync._counterparty(inbound)[1] == "dave@example.com"
    assert espo_sync._counterparty(outbound)[1] == "chris@questwatersports.com"


def test_an_email_espo_already_linked_keeps_that_parent(espo):
    linked = dict(EMAIL_STUB, parentType="Account", parentId="a9")
    assert espo_sync._resolve_parent(espo, linked) == ("Account", "a9")


def test_an_unlinked_email_is_matched_on_address(espo):
    assert espo_sync._resolve_parent(espo, dict(EMAIL_STUB)) == ("Contact", "c1")


def test_an_unknown_address_resolves_to_nothing(espo):
    stranger = dict(EMAIL_STUB, **{"from": "nobody@nowhere.test"}, nameHash={})
    assert espo_sync._resolve_parent(espo, stranger) == (None, None)


# --------------------------------------------------------------------------- #
# the sync
# --------------------------------------------------------------------------- #
def test_sync_posts_a_summary_note_to_the_contact(session, espo):
    stats = espo_sync.sync_emails(session)
    assert stats["analysed"] == 1

    note = espo.notes[0]
    assert (note["parentType"], note["parentId"]) == ("Contact", "c1")
    assert "Dave asked when the Malibu" in note["post"]
    assert "Daughter's wedding is the 20th" in note["post"]
    assert "[storage · powerboat]" in note["post"]
    assert note["isInternal"] is True


def test_sync_creates_tasks_only_for_what_we_committed_to(session, espo):
    espo_sync.sync_emails(session)
    assert [t["name"] for t in espo.tasks] == ["Offer Dave a winterizing date"]
    assert espo.tasks[0]["priority"] == "High"
    assert espo.tasks[0]["parent_id"] == "c1"


def test_the_watermark_stops_a_second_pass_redoing_the_work(session, espo):
    espo_sync.sync_emails(session)
    espo.notes.clear()
    stats = espo_sync.sync_emails(session)
    assert stats["analysed"] == 0 and stats["skipped"] == 1
    assert espo.notes == []


def test_an_empty_email_is_skipped_not_analysed(session, espo, monkeypatch):
    monkeypatch.setattr(espo, "email_body", lambda _id: dict(EMAIL_STUB, bodyPlain=""))
    stats = espo_sync.sync_emails(session)
    assert stats["analysed"] == 0 and stats["skipped"] == 1
    assert espo.notes == []


@pytest.mark.parametrize("category", ["spam", "internal"])
def test_noise_is_not_posted_into_somebodys_stream(session, espo, monkeypatch, category):
    quiet = ANALYSIS.model_copy(update={"category": category})
    monkeypatch.setattr(espo_sync.enrich, "analyze_conversation", lambda **kw: quiet)
    stats = espo_sync.sync_emails(session)
    assert stats["analysed"] == 1
    assert espo.notes == [] and espo.tasks == []


def test_the_watermark_holds_when_the_ai_is_unavailable(session, espo, monkeypatch):
    """A later run must retry, not skip past."""
    from crm.ai.client import AIUnavailable

    def boom(**kwargs):
        raise AIUnavailable("no API key")

    monkeypatch.setattr(espo_sync.enrich, "analyze_conversation", boom)
    stats = espo_sync.sync_emails(session)
    assert stats["failed"] == 1 and stats["analysed"] == 0

    monkeypatch.setattr(espo_sync.enrich, "analyze_conversation", lambda **kw: ANALYSIS)
    assert espo_sync.sync_emails(session)["analysed"] == 1


def test_a_storage_email_can_stage_a_shop_note(session, espo, monkeypatch):
    from crm.models import TicketNote, WorkItem, WorkSystem

    session.add(
        WorkItem(
            id="winter:QW-26-1255", system=WorkSystem.winter, remote_id="QW-26-1255",
            customer_email="dave@example.com", is_open=True,
        )
    )
    session.flush()

    with_update = ANALYSIS.model_copy(update={
        "ticket_updates": [TicketUpdate(ticket_id="winter:QW-26-1255",
                                        note="Wants it in before the 20th.")]
    })
    monkeypatch.setattr(espo_sync.enrich, "analyze_conversation", lambda **kw: with_update)

    espo_sync.sync_emails(session)
    staged = session.query(TicketNote).all()
    assert len(staged) == 1
    assert "Wants it in before the 20th." in staged[0].body


def test_open_shop_work_is_offered_as_context(session, espo):
    from crm.models import WorkItem, WorkSystem

    session.add(
        WorkItem(
            id="winter:QW-26-1255", system=WorkSystem.winter, remote_id="QW-26-1255",
            customer_email="dave@example.com", boat_info="2014 Malibu",
            storage_location="Inside Heated", is_open=True,
        )
    )
    session.flush()
    block = espo_sync._context_for(session, espo, "Contact", "c1")
    assert "winter:QW-26-1255" in block
    assert "Dave Mercer" in block
