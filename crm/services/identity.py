"""Phone/email normalisation and contact resolution.

Everything that creates a record goes through `resolve_contact` so the same
human never ends up as three rows.
"""
from __future__ import annotations

import logging
import re

import phonenumbers
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.models import Contact, ContactEmail, ContactPhone

log = logging.getLogger(__name__)

# PBXes hand out numbers in every shape: 6135551234, +16135551234, 9,6135551234,
# "Dave Mercer" <sip:6135551234@pbx>. Strip to something parseable first.
_SIP_RE = re.compile(r"sip:([^@;>]+)", re.I)
_DIGITS_RE = re.compile(r"[^\d+]")


def normalize_phone(raw: str | None, region: str | None = None) -> str | None:
    """Best-effort E.164. Returns None for anything that isn't a real number."""
    if not raw:
        return None
    text = str(raw).strip()
    if m := _SIP_RE.search(text):
        text = m.group(1)
    text = _DIGITS_RE.sub("", text)
    if not text:
        return None
    # Strip an outbound-dialling prefix like the leading 9 on a 10-digit local call.
    if len(text) == 11 and text.startswith("9") and not text.startswith("+"):
        text = text[1:]
    try:
        parsed = phonenumbers.parse(text, region or settings.default_region)
    except phonenumbers.NumberParseException:
        return None
    if not phonenumbers.is_possible_number(parsed):
        return None
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def pretty_phone(e164: str | None) -> str:
    if not e164:
        return ""
    try:
        parsed = phonenumbers.parse(e164, None)
        return phonenumbers.format_number(
            parsed, phonenumbers.PhoneNumberFormat.NATIONAL
        )
    except phonenumbers.NumberParseException:
        return e164


def normalize_email(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().lower()
    if m := re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", text):
        return m.group(0)
    return None


def is_operator_number(e164: str | None) -> bool:
    if not e164:
        return False
    own = {normalize_phone(n) for n in settings.operator_number_list}
    return e164 in own


def is_operator_email(address: str | None) -> bool:
    """True for any address that belongs to the operator, aliases included."""
    if not address:
        return False
    return address.lower() in settings.operator_email_list


# --------------------------------------------------------------------------- #
def find_by_phone(s: Session, e164: str) -> Contact | None:
    row = s.scalar(select(ContactPhone).where(ContactPhone.e164 == e164))
    return row.contact if row else None


def find_by_email(s: Session, address: str) -> Contact | None:
    row = s.scalar(
        select(ContactEmail).where(func.lower(ContactEmail.address) == address.lower())
    )
    return row.contact if row else None


def attach_phone(s: Session, contact: Contact, e164: str, label: str | None = None) -> None:
    existing = s.scalar(select(ContactPhone).where(ContactPhone.e164 == e164))
    if existing is None:
        s.add(ContactPhone(contact_id=contact.id, e164=e164, label=label))
        s.flush()
    elif existing.contact_id != contact.id:
        log.warning(
            "phone %s already belongs to contact %s; not reassigning to %s",
            e164, existing.contact_id, contact.id,
        )


def attach_email(s: Session, contact: Contact, address: str, label: str | None = None) -> None:
    address = address.lower()
    existing = s.scalar(
        select(ContactEmail).where(func.lower(ContactEmail.address) == address)
    )
    if existing is None:
        s.add(ContactEmail(contact_id=contact.id, address=address, label=label))
        s.flush()
    elif existing.contact_id != contact.id:
        log.warning(
            "email %s already belongs to contact %s; not reassigning to %s",
            address, existing.contact_id, contact.id,
        )


def resolve_contact(
    s: Session,
    *,
    phone: str | None = None,
    email: str | None = None,
    name: str | None = None,
    company: str | None = None,
    create: bool = True,
) -> Contact | None:
    """Find the contact behind an identifier, creating one if needed.

    Phone wins over email: on an inbound call the number is hard evidence, while
    a name heard over ASR is not.
    """
    e164 = normalize_phone(phone)
    addr = normalize_email(email)

    contact = None
    if e164:
        contact = find_by_phone(s, e164)
    if contact is None and addr:
        contact = find_by_email(s, addr)

    if contact is None:
        if not create:
            return None
        contact = Contact(
            display_name=(name or "").strip() or _placeholder_name(e164, addr),
            company=company,
        )
        _split_name(contact, name)
        s.add(contact)
        s.flush()
        log.info("created contact %s (%s)", contact.id, contact.display_name)

    if e164:
        attach_phone(s, contact, e164)
    if addr:
        attach_email(s, contact, addr)

    # Upgrade a placeholder once we learn a real name; never overwrite a real one.
    if name and name.strip() and _is_placeholder(contact.display_name):
        contact.display_name = name.strip()
        _split_name(contact, name)
    if company and not contact.company:
        contact.company = company

    s.flush()
    return contact


def _placeholder_name(e164: str | None, addr: str | None) -> str:
    if e164:
        return f"Unknown ({pretty_phone(e164)})"
    if addr:
        return addr
    return "Unknown contact"


def _is_placeholder(name: str | None) -> bool:
    return not name or name.startswith("Unknown") or "@" in name


def _split_name(contact: Contact, name: str | None) -> None:
    if not name or not name.strip():
        return
    parts = name.strip().split()
    contact.first_name = parts[0]
    if len(parts) > 1:
        contact.last_name = " ".join(parts[1:])


def merge_contacts(s: Session, keep_id: int, merge_id: int) -> Contact:
    """Fold `merge_id` into `keep_id`. Used by the UI's duplicate cleanup."""
    from crm.models import Appointment, Fact, Interaction, Need, Task

    if keep_id == merge_id:
        raise ValueError("Cannot merge a contact into itself.")
    keep = s.get(Contact, keep_id)
    dupe = s.get(Contact, merge_id)
    if keep is None or dupe is None:
        raise ValueError("Both contacts must exist.")

    for model in (Interaction, Fact, Need, Task, Appointment):
        for row in s.scalars(select(model).where(model.contact_id == merge_id)):
            row.contact_id = keep_id
    for phone in list(dupe.phones):
        phone.contact_id = keep_id
    for email in list(dupe.emails):
        email.contact_id = keep_id
    s.flush()

    # Those reassignments went in at the foreign-key level, so `dupe` still holds
    # the moved rows in its loaded collections. Deleting it now would cascade
    # delete-orphan straight through them. Expire first so the delete sees the
    # empty collections that are actually in the database.
    s.expire(dupe)
    s.expire(keep)

    if _is_placeholder(keep.display_name) and not _is_placeholder(dupe.display_name):
        keep.display_name = dupe.display_name
        keep.first_name, keep.last_name = dupe.first_name, dupe.last_name
    keep.company = keep.company or dupe.company
    keep.title = keep.title or dupe.title
    keep.address = keep.address or dupe.address
    keep.notes = "\n\n".join(x for x in (keep.notes, dupe.notes) if x) or None
    # The surviving profile is stale the moment the histories are combined.
    keep.ai_profile = None
    keep.ai_profile_updated_at = None

    s.flush()
    s.delete(dupe)
    s.flush()
    return keep
