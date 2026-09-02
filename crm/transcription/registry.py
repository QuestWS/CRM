"""Engine selection, stereo handling, and the single entry point the pipeline uses."""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from crm.config import settings
from crm.transcription.base import (
    Transcriber,
    Transcript,
    merge_channel_transcripts,
    split_channels,
)

log = logging.getLogger(__name__)


def get_transcriber(engine: str | None = None) -> Transcriber:
    engine = (engine or settings.transcription_engine).lower()
    if engine in ("faster_whisper", "whisper", "local"):
        from crm.transcription.local_whisper import FasterWhisperTranscriber

        return FasterWhisperTranscriber()
    if engine == "assemblyai":
        from crm.transcription.assemblyai import AssemblyAITranscriber

        return AssemblyAITranscriber()
    if engine == "deepgram":
        from crm.transcription.deepgram import DeepgramTranscriber

        return DeepgramTranscriber()
    raise ValueError(f"Unknown transcription engine: {engine!r}")


def transcribe_call(
    path: Path,
    *,
    engine: str | None = None,
    operator_label: str | None = None,
    caller_label: str = "Caller",
) -> Transcript:
    """Transcribe a call recording, attributing speakers where the audio allows.

    A two-channel recording is split and each leg transcribed on its own, which
    is more reliable than diarisation. Anything else is transcribed whole.
    """
    transcriber = get_transcriber(engine)
    operator_label = operator_label or settings.operator_name or "Operator"

    if settings.split_stereo_channels:
        with tempfile.TemporaryDirectory(prefix="crm-split-") as tmp:
            pair = split_channels(path, Path(tmp))
            if pair:
                left, right = pair
                log.info("stereo recording: transcribing both legs of %s", path.name)
                # Asterisk MixMonitor writes the party who was called on channel 0
                # and the local (operator) leg on channel 1.
                return merge_channel_transcripts(
                    transcriber.transcribe(left),
                    transcriber.transcribe(right),
                    left_speaker=caller_label,
                    right_speaker=operator_label,
                )

    return transcriber.transcribe(path)
