"""IMAP email intake - inbox and sent mail, into the same stream as calls.

Read-only by construction: every fetch uses BODY.PEEK so nothing is marked as
read, and no message is ever moved, flagged or deleted.

Sent mail matters as much as received mail here. Without your replies the CRM
sees a customer's question with no answer and keeps inventing follow-up tasks
for things you already dealt with.
"""
from __future__ import annotations

import datetime as dt
import email
import imaplib
import logging
import re
from contextlib import contextmanager
from email.message import Message

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.db import get_state, set_state
from crm.ingest import mailparse
from crm.models import Direction, Interaction, InteractionKind
from crm.services.identity import is_operator_email, resolve_contact

log = logging.getLogger(__name__)

# Big mailboxes are normal; a single message is not.
imaplib._MAXLINE = max(imaplib._MAXLINE, 10_000_000)

# LIST responses look like: (\HasNoChildren \Sent) "/" "[Gmail]/Sent Mail"
_LIST_RE = re.compile(r'^\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+(?P<name>.+)$')

# Fallbacks for servers that don't advertise RFC 6154 special-use flags.
_SENT_FALLBACKS = (
    "[gmail]/sent mail", "sent items", "sent", "inbox.sent", "sent messages",
    "outbox.sent", "[google mail]/sent mail",
)
_SKIP_FLAGS = {"\\noselect", "\\junk", "\\trash", "\\all", "\\drafts"}


class ImapNotConfigured(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
@contextmanager
def connect():
    """Log in to the configured mailbox. Always closes cleanly."""
    if not settings.imap_configured:
        raise ImapNotConfigured(
            "Set IMAP_HOST, IMAP_USERNAME and IMAP_PASSWORD in .env. "
            "On Gmail, IMAP_PASSWORD must be a 16-character App Password, not "
            "your account password."
        )

    if settings.imap_ssl:
        conn = imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port)
    else:
        conn = imaplib.IMAP4(settings.imap_host, settings.imap_port)
        conn.starttls()

    try:
        conn.login(settings.imap_username, settings.imap_password)
    except imaplib.IMAP4.error as exc:
        conn.logout()
        raise ImapNotConfigured(
            f"IMAP login failed for {settings.imap_username}@{settings.imap_host}: {exc}. "
            "On Gmail this usually means 2FA is off, or you used the account "
            "password instead of an App Password."
        ) from exc

    try:
        yield conn
    finally:
        try:
            conn.close()
        except (imaplib.IMAP4.error, OSError):
            pass
        try:
            conn.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _utf7_decode(text: str) -> str:
    """Minimal modified-UTF-7 decode (RFC 3501 §5.1.3)."""
    import base64 as _b64

    out, i = [], 0
    while i < len(text):
        char = text[i]
        if char != "&":
            out.append(char)
            i += 1
            continue
        end = text.find("-", i + 1)
        if end == -1:
            out.append(char)
            i += 1
            continue
        chunk = text[i + 1 : end]
        if not chunk:
            out.append("&")
        else:
            padded = chunk.replace(",", "/")
            padded += "=" * (-len(padded) % 4)
            out.append(_b64.b64decode(padded).decode("utf-16-be", "replace"))
        i = end + 1
    return "".join(out)


def list_folders(conn) -> list[tuple[str, set[str]]]:
    """[(folder name, lowercased flags)] for every selectable folder."""
    status, lines = conn.list()
    if status != "OK":
        return []
    folders: list[tuple[str, set[str]]] = []
    for line in lines or []:
        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else str(line)
        match = _LIST_RE.match(text.strip())
        if not match:
            continue
        name = match.group("name").strip()
        if name.startswith('"') and name.endswith('"'):
            name = name[1:-1]
        flags = {f.strip().lower() for f in match.group("flags").split() if f.strip()}
        folders.append((_utf7_decode(name), flags))
    return folders


def discover_folders(conn) -> list[str]:
    """Which folders to sync: INBOX plus the server's Sent folder.

    Prefers the RFC 6154 \\Sent special-use flag; falls back to well-known names
    for servers that don't advertise it. An explicit IMAP_FOLDERS wins over both.
    """
    if configured := settings.imap_folder_list:
        return configured

    folders = list_folders(conn)
    chosen = ["INBOX"]

    sent = next(
        (name for name, flags in folders if "\\sent" in flags),
        None,
    )
    if sent is None:
        by_lower = {name.lower(): name for name, flags in folders
                    if not (flags & _SKIP_FLAGS)}
        for candidate in _SENT_FALLBACKS:
            if candidate in by_lower:
                sent = by_lower[candidate]
                break

    if sent:
        chosen.append(sent)
    else:
        log.warning(
            "No Sent folder found on %s. Only received mail will be synced - set "
            "IMAP_FOLDERS explicitly to include it.",
            settings.imap_host,
        )
    return chosen


def _quote(folder: str) -> str:
    escaped = folder.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"' 


# --------------------------------------------------------------------------- #
# sync
# --------------------------------------------------------------------------- #
def _state_key(folder: str, suffix: str) -> str:
    return f"imap:{settings.imap_host}:{settings.imap_username}:{folder}:{suffix}"


def _select(conn, folder: str) -> int | None:
    """SELECT a folder read-only. Returns its UIDVALIDITY."""
    status, _ = conn.select(_quote(folder), readonly=True)
    if status != "OK":
        log.warning("cannot select folder %r", folder)
        return None
    status, data = conn.status(_quote(folder), "(UIDVALIDITY)")
    if status != "OK" or not data:
        return None
    match = re.search(rb"UIDVALIDITY\s+(\d+)", data[0])
    return int(match.group(1)) if match else None


def _search_uids(conn, folder: str, last_uid: int, backfill_days: int) -> list[int]:
    if last_uid:
        status, data = conn.uid("SEARCH", None, f"UID {last_uid + 1}:*")
    else:
        since = (dt.datetime.utcnow() - dt.timedelta(days=backfill_days)).strftime("%d-%b-%Y")
        status, data = conn.uid("SEARCH", None, f"(SINCE {since})")
    if status != "OK" or not data or not data[0]:
        return []
    # `UID n:*` always returns at least one UID even when nothing is new.
    return sorted(uid for uid in (int(x) for x in data[0].split()) if uid > last_uid)


def _fetch(conn, uid: int) -> Message | None:
    """BODY.PEEK keeps the message unread."""
    status, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
    if status != "OK" or not data:
        return None
    for item in data:
        if isinstance(item, tuple) and len(item) > 1 and item[1]:
            return email.message_from_bytes(item[1])
    return None


def _import_message(
    s: Session, msg: Message, *, folder: str, uid: int, uidvalidity: int | None
) -> bool:
    """Create one Interaction. False if skipped or already present."""
    if mailparse.is_bulk(msg):
        return False

    # Message-ID is stable across folders and re-syncs; UID is not.
    message_id = (msg.get("Message-ID") or "").strip().strip("<>")
    source_ref = message_id or f"{folder}:{uidvalidity}:{uid}"

    exists = s.scalar(
        select(Interaction.id).where(
            Interaction.source == "imap", Interaction.source_ref == source_ref
        )
    )
    if exists:
        return False

    from_pairs = mailparse.addresses(msg, "From")
    if not from_pairs:
        return False
    from_name, from_addr = from_pairs[0]
    to_pairs = mailparse.addresses(msg, "To", "Cc")

    outbound = is_operator_email(from_addr)
    if outbound:
        others = [p for p in to_pairs if not is_operator_email(p[1])]
    else:
        others = [(from_name, from_addr)]
    if not others:
        # Note to self, or a message only ever addressed to your own aliases.
        return False
    other_name, other_addr = others[0]

    contact = resolve_contact(s, email=other_addr, name=other_name or None)
    plain, html = mailparse.extract_bodies(msg)
    body = mailparse.clean_body(plain, html)
    if not body:
        return False

    occurred = mailparse.sent_at(msg)
    s.add(
        Interaction(
            contact_id=contact.id if contact else None,
            kind=InteractionKind.email,
            direction=Direction.outbound if outbound else Direction.inbound,
            occurred_at=occurred,
            subject=mailparse.decode_str(msg.get("Subject")) or "(no subject)",
            body=body,
            source="imap",
            source_ref=source_ref,
            meta={
                "folder": folder,
                "uid": uid,
                "from": from_addr,
                "to": [addr for _, addr in to_pairs],
                "in_reply_to": (msg.get("In-Reply-To") or "").strip().strip("<>") or None,
            },
        )
    )
    if contact and (not contact.last_contacted_at or contact.last_contacted_at < occurred):
        contact.last_contacted_at = occurred
    return True


def sync_folder(s: Session, conn, folder: str, *, limit: int) -> int:
    """Import new messages from one folder. Returns how many were added."""
    uidvalidity = _select(conn, folder)
    if uidvalidity is None:
        return 0

    validity_key = _state_key(folder, "uidvalidity")
    uid_key = _state_key(folder, "last_uid")
    stored_validity = get_state(s, validity_key)

    if stored_validity and stored_validity != str(uidvalidity):
        # The server renumbered the folder; our watermark means nothing now.
        log.warning("UIDVALIDITY changed for %r - restarting from the backfill window", folder)
        set_state(s, uid_key, "0")
    set_state(s, validity_key, str(uidvalidity))

    last_uid = int(get_state(s, uid_key, "0") or 0)
    uids = _search_uids(conn, folder, last_uid, settings.imap_backfill_days)
    if not uids:
        return 0

    truncated = len(uids) > limit
    batch = uids[:limit]
    added = 0
    highest = last_uid
    for uid in batch:
        try:
            msg = _fetch(conn, uid)
        except (imaplib.IMAP4.error, OSError) as exc:
            # Stop at the gap rather than skipping past it - the watermark must
            # not advance over a message we never looked at.
            log.warning("fetch failed for %s uid=%s: %s", folder, uid, exc)
            break
        if msg is not None and _import_message(
            s, msg, folder=folder, uid=uid, uidvalidity=uidvalidity
        ):
            added += 1
        highest = uid

    if highest > last_uid:
        set_state(s, uid_key, str(highest))
    s.flush()
    log.info(
        "imap %s: %d new message(s) up to uid %s%s",
        folder, added, highest, " (more waiting)" if truncated else "",
    )
    return added


def sync_mail(s: Session, *, limit: int | None = None) -> dict[str, int]:
    """One pass over every configured folder."""
    limit = limit or settings.imap_max_per_sync
    results: dict[str, int] = {}
    with connect() as conn:
        for folder in discover_folders(conn):
            try:
                results[folder] = sync_folder(s, conn, folder, limit=limit)
            except (imaplib.IMAP4.error, OSError) as exc:
                log.warning("imap sync failed for folder %r: %s", folder, exc)
                results[folder] = 0
            s.commit()
    return results
