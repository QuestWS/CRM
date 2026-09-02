"""Transcription interface plus the ffmpeg helpers the engines share."""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)


@dataclass
class Segment:
    start: float
    end: float
    text: str
    speaker: str | None = None

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 2),
            "end": round(self.end, 2),
            "text": self.text,
            "speaker": self.speaker,
        }


@dataclass
class Transcript:
    text: str
    segments: list[Segment] = field(default_factory=list)
    engine: str = "unknown"
    duration_seconds: float | None = None
    language: str | None = None

    def as_dialogue(self) -> str:
        """Speaker-labelled transcript, which is what the analyst prompt wants."""
        if not any(s.speaker for s in self.segments):
            return self.text
        lines: list[str] = []
        current: str | None = None
        buf: list[str] = []
        for seg in self.segments:
            speaker = seg.speaker or "Unknown"
            if speaker != current and buf:
                lines.append(f"{current}: {' '.join(buf).strip()}")
                buf = []
            current = speaker
            buf.append(seg.text.strip())
        if buf:
            lines.append(f"{current}: {' '.join(buf).strip()}")
        return "\n".join(lines)


class Transcriber(Protocol):
    name: str

    def transcribe(self, path: Path) -> Transcript: ...


# --------------------------------------------------------------------------- #
# audio helpers
# --------------------------------------------------------------------------- #
def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: Path) -> dict:
    """Return ffprobe's view of the first audio stream, or {} if unavailable."""
    if not shutil.which("ffprobe"):
        return {}
    try:
        out = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=channels,duration,codec_name",
                "-show_entries", "format=duration",
                "-of", "json", str(path),
            ],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
        data = json.loads(out)
        stream = (data.get("streams") or [{}])[0]
        duration = stream.get("duration") or data.get("format", {}).get("duration")
        return {
            "channels": int(stream.get("channels", 0) or 0),
            "codec": stream.get("codec_name"),
            "duration": float(duration) if duration else None,
        }
    except (subprocess.SubprocessError, ValueError, KeyError) as exc:
        log.warning("ffprobe failed for %s: %s", path, exc)
        return {}


def split_channels(path: Path, out_dir: Path) -> tuple[Path, Path] | None:
    """Split a 2-channel recording into (left, right) mono WAVs.

    Asterisk-based PBXs (FreePBX / PBXact) can record each leg of a call on its
    own channel. When they do, transcribing the legs separately gives reliable
    speaker attribution without a diarisation model.
    """
    if not have_ffmpeg():
        return None
    info = probe(path)
    if info.get("channels") != 2:
        return None

    out_dir.mkdir(parents=True, exist_ok=True)
    left = out_dir / f"{path.stem}.chan0.wav"
    right = out_dir / f"{path.stem}.chan1.wav"
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(path),
                "-filter_complex",
                "[0:a]channelsplit=channel_layout=stereo[l][r]",
                "-map", "[l]", "-ar", "16000", str(left),
                "-map", "[r]", "-ar", "16000", str(right),
            ],
            capture_output=True, text=True, timeout=900, check=True,
        )
    except subprocess.SubprocessError as exc:
        log.warning("channel split failed for %s: %s", path, exc)
        return None
    return left, right


def merge_channel_transcripts(
    left: Transcript, right: Transcript, left_speaker: str, right_speaker: str
) -> Transcript:
    """Interleave two single-speaker transcripts back into one dialogue."""
    segments: list[Segment] = []
    for tr, speaker in ((left, left_speaker), (right, right_speaker)):
        for seg in tr.segments:
            segments.append(Segment(seg.start, seg.end, seg.text, speaker))
    segments.sort(key=lambda s: s.start)
    merged = Transcript(
        text="",
        segments=segments,
        engine=left.engine,
        duration_seconds=max(
            left.duration_seconds or 0.0, right.duration_seconds or 0.0
        ) or None,
        language=left.language or right.language,
    )
    merged.text = merged.as_dialogue()
    return merged
