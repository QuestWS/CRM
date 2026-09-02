from __future__ import annotations

import email

from crm.ingest import mailparse


def build(raw: str):
    return email.message_from_string(raw)


PLAIN = """\
From: Dave Mercer <dave@example.com>
To: dana@questws.example
Subject: Furnace quote
Date: Mon, 15 Jan 2024 09:30:00 -0500
Message-ID: <abc123@example.com>
Content-Type: text/plain; charset="utf-8"

Hi Dana,

Can you send over that quote for the furnace? My wife wants it before Friday.

Thanks,
Dave
-- 
Dave Mercer
Mercer Plumbing | 613-555-1234
"""


def test_plain_body_drops_signature():
    msg = build(PLAIN)
    plain, html = mailparse.extract_bodies(msg)
    body = mailparse.clean_body(plain, html)
    assert "furnace" in body.lower()
    assert "Mercer Plumbing" not in body


def test_headers_and_addresses():
    msg = build(PLAIN)
    assert mailparse.addresses(msg, "From") == [("Dave Mercer", "dave@example.com")]
    assert mailparse.sent_at(msg).hour == 14  # 09:30 EST -> 14:30 UTC
    assert not mailparse.is_bulk(msg)


def test_encoded_subject_is_decoded():
    msg = build(
        "Subject: =?utf-8?B?UsOpc2VydmF0aW9u?=\n"
        "From: a@b.com\n\nhi\n"
    )
    assert mailparse.decode_str(msg.get("Subject")) == "Réservation"


def test_quoted_reply_history_is_stripped():
    msg = build(
        'From: dave@example.com\nContent-Type: text/plain\n\n'
        "Friday works for me.\n\n"
        "On Mon, 15 Jan 2024 at 09:30, Dana <dana@questws.example> wrote:\n"
        "> Does Friday at 2 work?\n"
        "> Let me know.\n"
    )
    plain, html = mailparse.extract_bodies(msg)
    body = mailparse.clean_body(plain, html)
    assert body.strip() == "Friday works for me."


def test_short_top_reply_is_not_stripped_to_nothing():
    """A one-word reply above a quote must survive: it is the whole message."""
    msg = build(
        'From: dave@example.com\nContent-Type: text/plain\n\n'
        "Yes.\n\nOn Mon, Dana wrote:\n> Does Friday at 2 work?\n"
    )
    plain, html = mailparse.extract_bodies(msg)
    assert "Yes." in mailparse.clean_body(plain, html)


def test_html_only_message_is_converted():
    msg = build(
        'From: a@b.com\nContent-Type: text/html; charset="utf-8"\n\n'
        "<html><body><p>Call me at <b>613-555-9999</b></p>"
        "<style>p{color:red}</style></body></html>\n"
    )
    plain, html = mailparse.extract_bodies(msg)
    body = mailparse.clean_body(plain, html)
    assert "613-555-9999" in body
    assert "color:red" not in body


def test_bulk_mail_is_detected():
    newsletter = build(
        "From: news@vendor.com\nList-Unsubscribe: <https://x/u>\n\nBuy things\n"
    )
    noreply = build("From: no-reply@vendor.com\n\nYour receipt\n")
    auto = build("From: a@b.com\nAuto-Submitted: auto-replied\n\nOut of office\n")
    assert mailparse.is_bulk(newsletter)
    assert mailparse.is_bulk(noreply)
    assert mailparse.is_bulk(auto)


def test_attachments_are_skipped():
    msg = build(
        'From: a@b.com\nContent-Type: multipart/mixed; boundary="B"\n\n'
        "--B\nContent-Type: text/plain\n\nSee attached quote.\n"
        "--B\nContent-Type: application/pdf\n"
        'Content-Disposition: attachment; filename="q.pdf"\n\n'
        "%PDF-1.4 binary junk\n--B--\n"
    )
    plain, html = mailparse.extract_bodies(msg)
    body = mailparse.clean_body(plain, html)
    assert "See attached quote." in body
    assert "%PDF" not in body
