"""AssemblyAI transcription — the same engine the shop's service tracker uses.

The service tracker submits a mechanic's voice note and waits for a webhook,
because Apps Script has no way to block. The CRM has a worker thread, so it
polls instead: simpler, and no public URL needed.

Speaker labels come from AssemblyAI's diarisation here rather than from
splitting stereo channels, which is what a single-mic counter recording or a
mono call recording needs.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx

from crm.config import settings
from crm.transcription.base import Segment, Transcript

log = logging.getLogger(__name__)

BASE = "https://api.assemblyai.com/v2"
POLL_SECONDS = 3
POLL_TIMEOUT_SECONDS = 1800


class AssemblyAITranscriber:
    name = "assemblyai"

    def __init__(self, api_key: str | None = None, speaker_labels: bool = True) -> None:
        self.api_key = api_key if api_key is not None else settings.assemblyai_api_key
        self.speaker_labels = speaker_labels
        if not self.api_key:
            raise RuntimeError(
                "ASSEMBLYAI_API_KEY is not set. It is the same key the service "
                "tracker uses (Apps Script → Project Settings → Script properties)."
            )

    def _headers(self) -> dict[str, str]:
        return {"authorization": self.api_key}

    def _upload(self, path: Path) -> str:
        with path.open("rb") as fh:
            resp = httpx.post(
                f"{BASE}/upload",
                content=fh.read(),
                headers={**self._headers(), "content-type": "application/octet-stream"},
                timeout=600,
            )
        resp.raise_for_status()
        return resp.json()["upload_url"]

    def transcribe(self, path: Path) -> Transcript:
        audio_url = self._upload(path)
        body = {
            "audio_url": audio_url,
            "punctuate": True,
            "format_text": True,
            "language_code": "en_us",
            "speaker_labels": self.speaker_labels,
            # Boat work is full of make and model names that generic models
            # mangle. These are the ones that cost the most when wrong.
            "word_boost": [
                "Yamaha", "Mercury", "Mercruiser", "Evinrude", "Johnson", "Suzuki",
                "Volvo Penta", "Malibu", "MasterCraft", "Sea-Doo", "Yeti",
                "impeller", "lower unit", "outdrive", "stringer", "transom",
                "bellows", "gimbal", "skeg", "prop", "shrink wrap", "winterize",
            ],
            "boost_param": "default",
        }
        created = httpx.post(
            f"{BASE}/transcript", json=body, headers=self._headers(), timeout=60
        )
        created.raise_for_status()
        transcript_id = created.json()["id"]

        deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
        while True:
            poll = httpx.get(
                f"{BASE}/transcript/{transcript_id}", headers=self._headers(), timeout=60
            )
            poll.raise_for_status()
            data = poll.json()
            status = data.get("status")
            if status == "completed":
                break
            if status == "error":
                raise RuntimeError(f"AssemblyAI failed: {data.get('error')}")
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"AssemblyAI did not finish within {POLL_TIMEOUT_SECONDS}s "
                    f"(transcript {transcript_id})"
                )
            time.sleep(POLL_SECONDS)

        segments: list[Segment] = []
        for utterance in data.get("utterances") or []:
            text = (utterance.get("transcript") or "").strip()
            if not text:
                continue
            segments.append(
                Segment(
                    start=float(utterance.get("start", 0)) / 1000.0,
                    end=float(utterance.get("end", 0)) / 1000.0,
                    text=text,
                    speaker=f"Speaker {utterance.get('speaker', '?')}",
                )
            )

        text = "\n".join(f"{s.speaker}: {s.text}" for s in segments) if segments else (
            data.get("text") or ""
        ).strip()
        duration = data.get("audio_duration")

        return Transcript(
            text=text,
            segments=segments,
            engine="assemblyai",
            duration_seconds=float(duration) if duration else None,
            language=data.get("language_code"),
        )
