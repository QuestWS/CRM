from __future__ import annotations

import pytest

from crm.models import (
    Contact,
    Direction,
    Fact,
    FactCategory,
    Interaction,
    InteractionKind,
)
from crm.services.identity import (
    is_operator_email,
    is_operator_number,
    merge_contacts,
    normalize_email,
    normalize_phone,
    resolve_contact,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("6135551234", "+16135551234"),
        ("(613) 555-1234", "+16135551234"),
        ("+1 613 555 1234", "+16135551234"),
        ("613-555-1234", "+16135551234"),
        ("96135551234", "+16135551234"),           # outbound dialling prefix
        ('"Dave" <sip:6135551234@pbx.local>', "+16135551234"),
        ("sip:+16135551234@10.0.0.5;transport=udp", "+16135551234"),
        ("", None),
        (None, None),
        ("anonymous", None),
        ("201", None),                              # internal extension, not a number
    ],
)
def test_normalize_phone(raw, expected):
    assert normalize_phone(raw) == expected


def test_normalize_email():
    assert normalize_email("  Dave Mercer <Dave@Example.COM> ") == "dave@example.com"
    assert normalize_email("not an address") is None


def test_operator_identity():
    assert is_operator_number("+16135550100")
    assert not is_operator_number("+16135559999")
    assert is_operator_email("DANA@questws.example")
    assert is_operator_email("info@questws.example")  # alias
    assert not is_operator_email("dave@example.com")


def test_resolve_contact_dedupes_by_phone(session):
    a = resolve_contact(session, phone="(613) 555-1234")
    b = resolve_contact(session, phone="+16135551234", name="Dave Mercer")
    assert a.id == b.id
    assert b.display_name == "Dave Mercer"     # placeholder got upgraded
    assert b.first_name == "Dave" and b.last_name == "Mercer"


def test_resolve_contact_does_not_overwrite_a_real_name(session):
    c = resolve_contact(session, phone="6135551234", name="Dave Mercer")
    resolve_contact(session, phone="6135551234", name="Dav Mercer")  # ASR mangled it
    assert c.display_name == "Dave Mercer"


def test_resolve_contact_links_phone_and_email(session):
    c = resolve_contact(session, phone="6135551234", name="Dave Mercer")
    same = resolve_contact(session, email="dave@example.com", phone="6135551234")
    assert same.id == c.id
    assert "dave@example.com" in [e.address for e in same.emails]


def test_phone_beats_email_when_they_disagree(session):
    """The number on an inbound call is hard evidence; a claimed email is not."""
    by_phone = resolve_contact(session, phone="6135551234", name="Dave")
    by_email = resolve_contact(session, email="someone@else.com", name="Someone")
    session.flush()
    merged = resolve_contact(session, phone="6135551234", email="someone@else.com")
    assert merged.id == by_phone.id != by_email.id


def test_merge_contacts_moves_everything(session):
    keep = resolve_contact(session, phone="6135551234", name="Dave Mercer")
    dupe = resolve_contact(session, email="dave@work.example", name="D. Mercer")
    session.add(
        Interaction(
            contact_id=dupe.id, kind=InteractionKind.email,
            direction=Direction.inbound, subject="hi", source="imap", source_ref="x1",
        )
    )
    session.add(Fact(contact_id=dupe.id, category=FactCategory.personal, text="Has a boat"))
    session.flush()

    merged = merge_contacts(session, keep.id, dupe.id)
    assert merged.id == keep.id
    assert session.get(Contact, dupe.id) is None
    assert [f.text for f in merged.facts] == ["Has a boat"]
    assert "dave@work.example" in [e.address for e in merged.emails]
    assert merged.ai_profile is None  # stale once histories combine
