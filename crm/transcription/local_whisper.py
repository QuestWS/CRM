"""Local transcription with faster-whisper. No audio leaves the machine."""
from __future__ import annotations

import logging
from pathlib import Path

from crm.config import settings
from crm.transcription.base import Segment, Transcript

log = logging.getLogger(__name__)

_model = None


def _load():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel  # imported lazily: heavy

        device = settings.whisper_device
        if device == "auto":
            try:
                import torch  # noqa: F401

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                device = "cpu"
        log.info(
            "loading whisper model=%s device=%s compute=%s",
            settings.whisper_model, device, settings.whisper_compute_type,
        )
        _model = WhisperModel(
            settings.whisper_model,
            device=device,
            compute_type=settings.whisper_compute_type,
        )
    return _model


class FasterWhisperTranscriber:
    name = "faster_whisper"

    def transcribe(self, path: Path) -> Transcript:
        model = _load()
        segments_iter, info = model.transcribe(
            str(path),
            vad_filter=True,
            beam_size=5,
            # Business calls are full of names, addresses and numbers. Giving the
            # decoder a hint reduces the mangling that survives into extraction.
            initial_prompt=(
                "A recorded business phone call. Names, street addresses, phone "
                "numbers, dollar amounts and appointment times may be spoken."
            ),
        )
        segments = [
            Segment(start=s.start, end=s.end, text=s.text.strip())
            for s in segments_iter
            if s.text and s.text.strip()
        ]
        text = " ".join(s.text for s in segments).strip()
        return Transcript(
            text=text,
            segments=segments,
            engine=f"faster_whisper:{settings.whisper_model}",
            duration_seconds=getattr(info, "duration", None),
            language=getattr(info, "language", None),
        )
