"""Turning a raw RFC-822 message into something worth handing to the model."""
from __future__ import annotations

import datetime as dt
import logging
import re
from email.header import decode_header, make_header
from email.message import Message
from email.utils import getaddresses, parsedate_to_datetime

from crm.models import to_naive_utc
from crm.services.identity import normalize_email

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 60_000

# Quoted history and signatures push the actual message out of view and invite
# the model to extract stale facts from the bottom of a long thread.
_QUOTE_MARKERS = (
    re.compile(r"^\s*On .{0,200}wrote:\s*$", re.M),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.M | re.I),
    re.compile(r"^\s*_{10,}\s*$", re.M),
    re.compile(r"^\s*From:.*\n\s*Sent:.*$", re.M),
    re.compile(r"^\s*>{1,}.*(\n\s*>{1,}.*){3,}", re.M),  # a real quoted block
)
_SIGNATURE = re.compile(r"^-- ?$", re.M)

# Bulk mail is noise in a CRM: it has no person behind it and no need to track.
_BULK_HEADERS = (
    "list-unsubscribe",
    "list-id",
    "precedence",
    "auto-submitted",
    "x-auto-response-suppress",
)
_NOREPLY = re.compile(
    r"(no[-_.]?reply|do[-_.]?not[-_.]?reply|donotreply|mailer-daemon|postmaster|"
    r"notifications?@|bounce)",
    re.I,
)


def decode_str(value: str | None) -> str:
    """Decode RFC 2047 encoded-words (=?utf-8?q?...?=) into plain text."""
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def addresses(msg: Message, *fields: str) -> list[tuple[str, str]]:
    """[(display name, normalised address)] across the named header fields."""
    raw = []
    for field in fields:
        raw.extend(msg.get_all(field, []) or [])
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, addr in getaddresses(raw):
        clean = normalize_email(addr)
        if clean and clean not in seen:
            seen.add(clean)
            out.append((decode_str(name), clean))
    return out


def sent_at(msg: Message) -> dt.datetime:
    raw = msg.get("Date")
    if raw:
        try:
            return to_naive_utc(parsedate_to_datetime(raw)) or dt.datetime.utcnow()
        except (TypeError, ValueError, OverflowError):
            log.debug("unparseable Date header: %r", raw)
    return dt.datetime.utcnow()


def is_bulk(msg: Message) -> bool:
    """True for newsletters, notifications and auto-replies."""
    for header in _BULK_HEADERS:
        value = (msg.get(header) or "").lower()
        if header == "precedence":
            if value in ("bulk", "list", "junk"):
                return True
        elif value:
            return True
    sender = " ".join(msg.get_all("From", []) or [])
    return bool(_NOREPLY.search(sender))


def _payload_text(part: Message) -> str:
    try:
        payload = part.get_payload(decode=True)
    except (AssertionError, ValueError):
        return ""
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def extract_bodies(msg: Message) -> tuple[str, str]:
    """Collect (text/plain, text/html), skipping attachments."""
    plain, html = "", ""
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disposition = (part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain":
            plain += _payload_text(part)
        elif ctype == "text/html":
            html += _payload_text(part)
    return plain, html


def strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", html)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    replacements = {
        "&nbsp;": " ", "&amp;": "&", "&lt;": "<", "&gt;": ">",
        "&#39;": "'", "&quot;": '"', "&rsquo;": "'", "&mdash;": "-",
    }
    for entity, char in replacements.items():
        text = text.replace(entity, char)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"[ \t]{2,}", " ", text)


def clean_body(plain: str, html: str) -> str:
    """The newest message only, with quoted history and signature removed."""
    text = plain.strip() or strip_html(html)
    cut = len(text)
    for marker in _QUOTE_MARKERS:
        if m := marker.search(text):
            cut = min(cut, m.start())
    # Never strip the whole message: a bare forward, or a reply whose quote
    # starts at character zero, would otherwise come out empty. Anything that
    # leaves real content behind is fair to cut - "Yes." is a complete answer.
    if len(text[:cut].strip()) >= 2:
        text = text[:cut]
    if m := _SIGNATURE.search(text):
        if len(text[: m.start()].strip()) >= 2:
            text = text[: m.start()]
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + "\n\n[truncated for length]"
    return text
