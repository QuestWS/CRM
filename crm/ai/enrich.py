"""The three AI jobs: analyse a conversation, rewrite a profile, write the brief."""
from __future__ import annotations

import datetime as dt
import logging

from crm.ai.client import ClaudeClient, get_client
from crm.ai.prompts import BRIEF_WRITER, PROFILE_WRITER, conversation_system
from crm.ai.schemas import ConversationAnalysis, DailyBrief, ProfileNarrative
from crm.config import settings

log = logging.getLogger(__name__)

# ASR output for a long call is the bulk of the prompt; leave room but don't
# silently truncate - the caller decides what to do if a call runs past this.
MAX_TRANSCRIPT_CHARS = 400_000


class TranscriptTooLong(ValueError):
    pass


def analyze_conversation(
    *,
    body: str,
    kind: str,
    direction: str,
    occurred_at: dt.datetime,
    known_context: str = "",
    subject: str | None = None,
    from_number: str | None = None,
    to_number: str | None = None,
    duration_seconds: int | None = None,
    client: ClaudeClient | None = None,
) -> ConversationAnalysis:
    """Turn one call transcript or email into structured CRM records."""
    if len(body) > MAX_TRANSCRIPT_CHARS:
        raise TranscriptTooLong(
            f"Conversation is {len(body):,} characters, over the "
            f"{MAX_TRANSCRIPT_CHARS:,} limit. Split it before analysing."
        )

    client = client or get_client()
    header = [f"Type: {kind}", f"Direction: {direction}", f"When: {occurred_at.isoformat()}"]
    if subject:
        header.append(f"Subject: {subject}")
    if from_number:
        header.append(f"From: {from_number}")
    if to_number:
        header.append(f"To: {to_number}")
    if duration_seconds:
        header.append(f"Duration: {duration_seconds // 60}m {duration_seconds % 60}s")

    parts = ["<conversation_metadata>", "\n".join(header), "</conversation_metadata>"]
    if known_context.strip():
        parts += [
            "",
            "<already_on_file>",
            "What the CRM already knows about this person. Do not repeat these as "
            "new facts; only add what is new or has changed.",
            known_context.strip(),
            "</already_on_file>",
        ]
    parts += ["", "<conversation>", body.strip(), "</conversation>"]

    return client.structured(
        system=conversation_system(
            operator_name=settings.operator_name,
            business=settings.operator_business,
            timezone=settings.timezone,
            occurred_at=occurred_at.isoformat(),
        ),
        prompt="\n".join(parts),
        schema=ConversationAnalysis,
    )


def write_profile(
    *,
    contact_label: str,
    facts_block: str,
    needs_block: str,
    history_block: str,
    client: ClaudeClient | None = None,
) -> ProfileNarrative:
    client = client or get_client()
    prompt = "\n".join(
        [
            f"<contact>{contact_label}</contact>",
            "",
            "<known_facts>",
            facts_block or "(none recorded yet)",
            "</known_facts>",
            "",
            "<open_needs>",
            needs_block or "(none recorded yet)",
            "</open_needs>",
            "",
            "<recent_history>",
            history_block or "(no interactions yet)",
            "</recent_history>",
        ]
    )
    return client.structured(
        system=PROFILE_WRITER, prompt=prompt, schema=ProfileNarrative, max_tokens=4000
    )


def write_brief(*, context_block: str, client: ClaudeClient | None = None) -> DailyBrief:
    client = client or get_client()
    return client.structured(
        system=BRIEF_WRITER,
        prompt=context_block,
        schema=DailyBrief,
        max_tokens=4000,
    )
