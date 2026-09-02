"""Deepgram transcription - cloud, with real diarisation. Opt-in."""
from __future__ import annotations

import logging
from pathlib import Path

import httpx

from crm.config import settings
from crm.transcription.base import Segment, Transcript

log = logging.getLogger(__name__)

ENDPOINT = "https://api.deepgram.com/v1/listen"


class DeepgramTranscriber:
    name = "deepgram"

    def __init__(self, api_key: str | None = None, model: str = "nova-2") -> None:
        self.api_key = api_key or settings.deepgram_api_key
        self.model = model
        if not self.api_key:
            raise RuntimeError("DEEPGRAM_API_KEY is not set.")

    def transcribe(self, path: Path) -> Transcript:
        params = {
            "model": self.model,
            "smart_format": "true",
            "diarize": "true",
            "punctuate": "true",
            "utterances": "true",
        }
        with path.open("rb") as fh:
            resp = httpx.post(
                ENDPOINT,
                params=params,
                content=fh.read(),
                headers={
                    "Authorization": f"Token {self.api_key}",
                    "Content-Type": "audio/*",
                },
                timeout=600,
            )
        resp.raise_for_status()
        data = resp.json()

        segments: list[Segment] = []
        for utt in data.get("results", {}).get("utterances", []) or []:
            segments.append(
                Segment(
                    start=float(utt.get("start", 0.0)),
                    end=float(utt.get("end", 0.0)),
                    text=(utt.get("transcript") or "").strip(),
                    speaker=f"Speaker {utt.get('speaker', '?')}",
                )
            )
        if segments:
            text = "\n".join(f"{s.speaker}: {s.text}" for s in segments)
        else:
            alts = (
                data.get("results", {}).get("channels", [{}])[0].get("alternatives", [{}])
            )
            text = (alts[0].get("transcript") or "").strip()

        return Transcript(
            text=text,
            segments=segments,
            engine=f"deepgram:{self.model}",
            duration_seconds=data.get("metadata", {}).get("duration"),
        )
