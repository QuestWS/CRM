"""Structured-output schemas. These are the contract between Claude and the DB."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Urgency = Literal["low", "normal", "high", "urgent"]
Category = Literal["personal", "business", "preference", "logistics", "other"]


class ExtractedPerson(BaseModel):
    full_name: str | None = Field(
        None, description="The other party's name exactly as they gave it."
    )
    first_name: str | None = None
    last_name: str | None = None
    company: str | None = None
    title: str | None = None
    phone: str | None = Field(
        None, description="Any phone number they stated for themselves, digits as spoken."
    )
    email: str | None = None
    address: str | None = Field(None, description="Service address or mailing address.")
    name_confidence: float = Field(
        0.0, description="0-1. Use <0.5 if the name was inferred rather than stated."
    )


class ExtractedFact(BaseModel):
    category: Category
    text: str = Field(description="One durable fact, written in the third person.")
    quote: str | None = Field(None, description="The words they used, if short and clear.")
    confidence: float = Field(0.7, description="0-1.")


class ExtractedNeed(BaseModel):
    title: str = Field(description="Short label for the job they want done.")
    description: str
    urgency: Urgency = "normal"
    value_estimate: str | None = Field(
        None, description="Dollar figure or size if they mentioned one, else null."
    )
    blockers: str | None = None
    resolved_in_this_conversation: bool = False


class ExtractedTask(BaseModel):
    title: str = Field(description="An action, phrased imperatively.")
    detail: str | None = None
    owner: Literal["me", "them"] = Field(
        "me", description="'me' = the operator committed to it. 'them' = the customer did."
    )
    due_hint: str | None = Field(
        None, description="Natural language timing as stated, e.g. 'Friday morning'."
    )
    due_at_iso: str | None = Field(
        None, description="ISO-8601 local datetime if it can be resolved, else null."
    )
    priority: Urgency = "normal"


class ExtractedAppointment(BaseModel):
    title: str
    starts_at_iso: str | None = Field(
        None, description="ISO-8601 local datetime. Null if no concrete time was agreed."
    )
    ends_at_iso: str | None = None
    location: str | None = None
    tentative: bool = Field(
        True, description="False only if both parties clearly confirmed the slot."
    )


class ConversationAnalysis(BaseModel):
    """What one call or email yields."""

    summary: str = Field(description="3-6 sentences. What happened and what it means.")
    outcome: str = Field(description="One line: where things stand now.")
    sentiment: Literal["positive", "neutral", "concerned", "frustrated", "angry"] = "neutral"
    person: ExtractedPerson
    facts: list[ExtractedFact] = []
    needs: list[ExtractedNeed] = []
    tasks: list[ExtractedTask] = []
    appointments: list[ExtractedAppointment] = []
    followup_suggestion: str | None = Field(
        None, description="The single most useful next move, or null."
    )
    missed_opportunity: str | None = Field(
        None, description="Something worth asking next time. Null if nothing stands out."
    )


class ProfileNarrative(BaseModel):
    """The rolling 'who is this person' brief shown at the top of a contact page."""

    profile: str = Field(
        description="2-4 short paragraphs. Person first, then their business situation."
    )
    talking_points: list[str] = Field(
        default=[], description="3-5 specific things to raise or ask about next time."
    )
    watch_outs: list[str] = Field(
        default=[], description="Sensitivities, past friction, or promises still owed."
    )


class DailyBrief(BaseModel):
    headline: str = Field(description="One sentence framing the day.")
    priorities: list[str] = Field(description="3-6 ordered, specific actions.")
    waiting_on_others: list[str] = []
    at_risk: list[str] = Field(
        default=[], description="People or deals going cold, and why it matters."
    )
