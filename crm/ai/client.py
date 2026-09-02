"""Thin wrapper over the Anthropic SDK.

Two entry points: `structured()` for schema-validated extraction, `text()` for
prose. Both are safe to call when no API key is configured - they raise a
single, recognisable error the pipeline records instead of crashing the worker.
"""
from __future__ import annotations

import logging
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from crm.config import settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)

# The server-side fallback re-runs a declined request on another model inside the
# same call, so a single odd-sounding transcript can't stall the pipeline.
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AIUnavailable(RuntimeError):
    """No API key, or the model chain declined the request."""


class ClaudeClient:
    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key if api_key is not None else settings.anthropic_api_key
        self.model = model or settings.claude_model
        self.effort = settings.claude_effort
        self._client: anthropic.Anthropic | None = None

    @property
    def available(self) -> bool:
        return bool(self._api_key)

    @property
    def client(self) -> anthropic.Anthropic:
        if not self.available:
            raise AIUnavailable(
                "ANTHROPIC_API_KEY is not set - transcription will still run, "
                "but summaries and profiles are skipped."
            )
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=self._api_key)
        return self._client

    # ------------------------------------------------------------------ #
    def structured(
        self,
        *,
        system: str,
        prompt: str,
        schema: type[T],
        max_tokens: int = 8000,
        effort: str | None = None,
    ) -> T:
        """Ask for a response that validates against `schema`."""
        response = self.client.messages.parse(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
            thinking={"type": "adaptive"},
            output_config={"effort": effort or self.effort},
        )
        if response.stop_reason == "refusal":
            raise AIUnavailable(f"Model declined the request: {response.stop_details}")
        parsed = response.parsed_output
        if parsed is None:
            raise AIUnavailable("Model returned no parseable output.")
        return parsed

    def text(
        self,
        *,
        system: str,
        prompt: str,
        max_tokens: int = 4000,
        effort: str | None = None,
    ) -> str:
        """Free-form prose, with server-side refusal fallback enabled."""
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config={"effort": effort or self.effort},
        )
        try:
            response = self.client.beta.messages.create(
                betas=[FALLBACK_BETA], fallbacks="default", **kwargs
            )
        except (anthropic.BadRequestError, TypeError) as exc:
            # Older SDK or an account without the fallback beta: run it plain.
            log.debug("falling back to non-beta messages.create: %s", exc)
            response = self.client.messages.create(**kwargs)

        if response.stop_reason == "refusal":
            raise AIUnavailable(f"Model declined the request: {response.stop_details}")
        return "".join(b.text for b in response.content if b.type == "text").strip()


_default: ClaudeClient | None = None


def get_client() -> ClaudeClient:
    global _default
    if _default is None:
        _default = ClaudeClient()
    return _default
