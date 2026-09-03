"""Twenty CRM client and sync, against a stubbed API.

The shape being pinned down is Twenty's own: records nested under
`data.<object>`, cursor pagination, and notes that carry no parent — the link
is a separate noteTarget record.
"""
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
from crm.integrations.twenty import TwentyClient, TwentyError
from crm.services import twenty_sync

MESSAGE = {
    "id": "m1",
    "subject": "Winterizing question",
    "text": "Hi Chris — when can you get the Malibu in for winterizing? "
            "My daughter's wedding is the 20th so I'd like it done before.",
    "receivedAt": "2026-09-01T14:30:00.000Z",
    "messageParticipants": [
        {"role": "from", "handle": "dave@example.com", "displayName": "Dave Mercer",
         "person": {"id": "p1"}},
        {"role": "to", "handle": "dana@questws.example", "person": None},
    ],
}

ANALYSIS = ConversationAnalysis(
    category="storage", category_confidence=0.9, product_line="powerboat",
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


class FakeTwenty:
    def __init__(self, messages=None):
        self.messages = messages if messages is not None else [dict(MESSAGE)]
        self.notes: list[dict] = []
        self.note_targets: list[dict] = []
        self.tasks: list[dict] = []
        self.task_targets: list[dict] = []
        self.configured = True
        self.base_url = "http://twenty.test:3000"

    def health(self):
        return True

    def whoami(self):
        return {"id": "w1", "displayName": "Quest Watersports"}

    def recent_messages(self, since=None, limit=50):
        return [dict(m) for m in self.messages]

    def get(self, obj, record_id, depth=None):
        if obj == "people" and record_id == "p1":
            return {"id": "p1", "name": {"firstName": "Dave", "lastName": "Mercer"},
                    "phones": {"primaryPhoneNumber": "+18155551234"}}
        return {}

    def find_person_by_email(self, address):
        return {"id": "p1"} if address == "dave@example.com" else None

    def post_note(self, title, body_markdown, person_id=None, company_id=None):
        note = {"id": f"n{len(self.notes)+1}", "title": title, "body": body_markdown}
        self.notes.append(note)
        if person_id:
            self.note_targets.append({"noteId": note["id"], "personId": person_id})
        return note

    def create_task(self, title, body_markdown=None, person_id=None,
                    company_id=None, due_at=None, status="TODO"):
        task = {"id": f"t{len(self.tasks)+1}", "title": title, "dueAt": due_at}
        self.tasks.append(task)
        if person_id:
            self.task_targets.append({"taskId": task["id"], "personId": person_id})
        return task

    @staticmethod
    def _records(payload, obj):
        return TwentyClient._records(payload, obj)

    def list(self, obj, **kwargs):
        return {"data": {obj: []}}


@pytest.fixture
def tw(monkeypatch):
    client = FakeTwenty()
    monkeypatch.setattr(twenty_sync.twenty, "get_client", lambda: client)
    monkeypatch.setattr(twenty_sync.enrich, "analyze_conversation", lambda **kw: ANALYSIS)
    return client


# --------------------------------------------------------------------------- #
# the client's shape
# --------------------------------------------------------------------------- #
def test_records_come_out_of_the_data_envelope():
    payload = {"data": {"people": [{"id": "p1"}, {"id": "p2"}]}}
    assert [r["id"] for r in TwentyClient._records(payload, "people")] == ["p1", "p2"]


def test_records_tolerate_a_differently_keyed_envelope():
    """A single-key data object is unwrapped even if the key is not the one
    we asked for — the alternative is silently returning nothing."""
    assert TwentyClient._records({"data": {"companies": [{"id": "c1"}]}}, "people") \
        == [{"id": "c1"}]


def test_missing_data_is_an_empty_list_not_a_crash():
    assert TwentyClient._records({}, "people") == []


def test_trailing_slash_in_the_url_does_not_double_up():
    assert TwentyClient("http://x:3000/", "k").base_url == "http://x:3000"


def test_unconfigured_client_says_where_the_key_comes_from():
    from crm.integrations.twenty import TwentyNotConfigured

    with pytest.raises(TwentyNotConfigured, match="Settings"):
        TwentyClient("", "").whoami()


def test_a_note_target_needs_something_to_attach_to():
    client = TwentyClient("http://x:3000", "k")
    with pytest.raises(TwentyError, match="person, company or opportunity"):
        client.attach_note("n1")


# --------------------------------------------------------------------------- #
# direction and matching
# --------------------------------------------------------------------------- #
def test_direction_is_read_off_the_participants():
    assert twenty_sync._direction(MESSAGE) == "inbound"
    flipped = {
        **MESSAGE,
        "messageParticipants": [
            {"role": "from", "handle": "dana@questws.example"},
            {"role": "to", "handle": "dave@example.com", "person": {"id": "p1"}},
        ],
    }
    assert twenty_sync._direction(flipped) == "outbound"


def test_counterparty_is_the_far_end_in_each_direction():
    assert twenty_sync._counterparty(MESSAGE)["handle"] == "dave@example.com"
    outbound = {
        **MESSAGE,
        "messageParticipants": [
            {"role": "from", "handle": "dana@questws.example"},
            {"role": "to", "handle": "dave@example.com", "person": {"id": "p1"}},
        ],
    }
    assert twenty_sync._counterparty(outbound)["handle"] == "dave@example.com"


def test_a_person_twenty_already_linked_is_used_as_is(tw):
    assert twenty_sync._resolve_person(tw, MESSAGE) == ("p1", "dave@example.com")


def test_an_unlinked_participant_falls_back_to_a_lookup(tw):
    unlinked = {
        **MESSAGE,
        "messageParticipants": [
            {"role": "from", "handle": "dave@example.com", "person": None},
        ],
    }
    assert twenty_sync._resolve_person(tw, unlinked) == ("p1", "dave@example.com")


# --------------------------------------------------------------------------- #
# the sync
# --------------------------------------------------------------------------- #
def test_sync_writes_a_note_and_links_it_to_the_person(session, tw):
    stats = twenty_sync.sync_messages(session)
    assert stats["analysed"] == 1

    note = tw.notes[0]
    assert "Dave asked when the Malibu" in note["body"]
    assert "Daughter's wedding is the 20th" in note["body"]
    assert "[storage · powerboat]" in note["body"]
    # The link is a separate record — Twenty's notes carry no parent.
    assert tw.note_targets == [{"noteId": note["id"], "personId": "p1"}]


def test_tasks_are_only_for_what_we_committed_to(session, tw):
    twenty_sync.sync_messages(session)
    assert [t["title"] for t in tw.tasks] == ["Offer Dave a winterizing date"]
    assert tw.task_targets[0]["personId"] == "p1"


def test_the_watermark_stops_a_second_pass_redoing_the_work(session, tw):
    twenty_sync.sync_messages(session)
    tw.notes.clear()
    stats = twenty_sync.sync_messages(session)
    assert stats["analysed"] == 0 and stats["skipped"] == 1
    assert tw.notes == []


def test_an_empty_message_is_skipped(session, tw):
    tw.messages = [dict(MESSAGE, text="")]
    stats = twenty_sync.sync_messages(session)
    assert stats["analysed"] == 0 and stats["skipped"] == 1


@pytest.mark.parametrize("category", ["spam", "internal"])
def test_noise_gets_no_note_and_no_task(session, tw, monkeypatch, category):
    quiet = ANALYSIS.model_copy(update={"category": category})
    monkeypatch.setattr(twenty_sync.enrich, "analyze_conversation", lambda **kw: quiet)
    assert twenty_sync.sync_messages(session)["analysed"] == 1
    assert tw.notes == [] and tw.tasks == []


def test_the_watermark_holds_when_the_ai_is_unavailable(session, tw, monkeypatch):
    from crm.ai.client import AIUnavailable

    def boom(**kwargs):
        raise AIUnavailable("no API key")

    monkeypatch.setattr(twenty_sync.enrich, "analyze_conversation", boom)
    assert twenty_sync.sync_messages(session)["failed"] == 1

    monkeypatch.setattr(twenty_sync.enrich, "analyze_conversation", lambda **kw: ANALYSIS)
    assert twenty_sync.sync_messages(session)["analysed"] == 1


def test_a_storage_email_stages_a_shop_note(session, tw, monkeypatch):
    from crm.models import TicketNote, WorkItem, WorkSystem

    session.add(
        WorkItem(id="winter:QW-26-1255", system=WorkSystem.winter, remote_id="QW-26-1255",
                 customer_email="dave@example.com", is_open=True)
    )
    session.flush()
    with_update = ANALYSIS.model_copy(update={
        "ticket_updates": [TicketUpdate(ticket_id="winter:QW-26-1255",
                                        note="Wants it in before the 20th.")]
    })
    monkeypatch.setattr(twenty_sync.enrich, "analyze_conversation", lambda **kw: with_update)

    twenty_sync.sync_messages(session)
    staged = session.query(TicketNote).all()
    assert len(staged) == 1 and "before the 20th" in staged[0].body


def test_open_shop_work_reaches_the_prompt_as_context(session, tw):
    from crm.models import WorkItem, WorkSystem

    session.add(
        WorkItem(id="winter:QW-26-1255", system=WorkSystem.winter, remote_id="QW-26-1255",
                 customer_phone="+18155551234", boat_info="2014 Malibu",
                 storage_location="Inside Heated", is_open=True)
    )
    session.flush()
    block = twenty_sync._context_for(session, tw, "p1", "dave@example.com")
    assert "winter:QW-26-1255" in block and "Dave Mercer" in block
