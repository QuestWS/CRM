"""Intake for call recordings produced by a Sangoma / FreePBX / PBXact system.

Three independent paths, because different Sangoma deployments expose
recordings differently and most sites end up using more than one:

  * webhook   - the PBX (or a small shim on it) POSTs metadata plus the audio.
  * directory - a mounted or rsync'd copy of the PBX's monitor folder.
  * upload    - a human drops a file into the web UI.

All three converge on `register_recording`, which is idempotent on file hash.

Note on scope: the recording itself and the "this call is being recorded"
announcement are configured on the PBX, not here - see docs/sangoma-setup.md.
A phone app cannot be made to record its own calls; the PBX is the only place
that legitimately sees both legs of the call.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
import shutil
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from crm.config import settings
from crm.models import Direction, Recording, RecordingStatus, to_naive_utc
from crm.services.identity import is_operator_number, normalize_phone

log = logging.getLogger(__name__)

AUDIO_SUFFIXES = {".wav", ".mp3", ".ogg", ".m4a", ".gsm", ".wav49", ".opus", ".flac"}

# Asterisk-derived recording names carry the call's identity in the filename:
#   out-6135551234-201-20240115-143022-1705345822.14.wav
#   in-201-6135551234-20240115-143022-1705345822.14.wav
#   external-6135551234-20240115-143022-1705345822.14.wav
# The date and time are the only reliably positioned tokens, so anchor on them.
# The stamp must sit between dashes: without that boundary the tail of a phone
# number happily matches, e.g. "6135551234-20240115" yields "35551234-202401".
_STAMP_RE = re.compile(r"(?:^|-)(?P<date>\d{8})-(?P<time>\d{6})(?=-|$)")
_DIRECTION_TOKENS = {
    "out": Direction.outbound,
    "outbound": Direction.outbound,
    "in": Direction.inbound,
    "inbound": Direction.inbound,
    "external": Direction.inbound,
    "exten": Direction.internal,
    "internal": Direction.internal,
    "q": Direction.inbound,
    "queue": Direction.inbound,
}


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def parse_recording_filename(name: str) -> dict:
    """Pull direction, endpoints, timestamp and call id out of a PBX filename.

    Returns whatever it could work out; every key may be None. Filename parsing
    is a fallback - webhook metadata or the CDR always wins when present.
    """
    stem = Path(name).stem
    out: dict = {
        "direction": Direction.unknown,
        "from_number": None,
        "to_number": None,
        "call_started_at": None,
        "pbx_call_id": None,
    }

    match = None
    for candidate in _STAMP_RE.finditer(stem):
        try:
            out["call_started_at"] = dt.datetime.strptime(
                f"{candidate.group('date')}{candidate.group('time')}", "%Y%m%d%H%M%S"
            )
        except ValueError:
            continue  # eight digits that aren't a date; keep looking
        match = candidate
        break
    if match is None:
        return out

    head = [t for t in stem[: match.start("date")].strip("-").split("-") if t]
    tail = stem[match.end("time") :].strip("-")
    out["pbx_call_id"] = tail or None

    direction = Direction.unknown
    if head and head[0].lower() in _DIRECTION_TOKENS:
        direction = _DIRECTION_TOKENS[head[0].lower()]
        head = head[1:]
    out["direction"] = direction

    endpoints = [t for t in head if re.fullmatch(r"\+?\d{2,20}", t)]
    if direction == Direction.outbound:
        # out-<dialled>-<extension>
        out["to_number"] = endpoints[0] if endpoints else None
        out["from_number"] = endpoints[1] if len(endpoints) > 1 else None
    elif len(endpoints) >= 2:
        # in-<extension>-<caller>
        out["to_number"] = endpoints[0]
        out["from_number"] = endpoints[1]
    elif endpoints:
        out["from_number"] = endpoints[0]

    return out


def infer_direction(from_number: str | None, to_number: str | None) -> Direction:
    """Fall back to 'which side is us' when the filename didn't say."""
    from_is_us = is_operator_number(from_number)
    to_is_us = is_operator_number(to_number)
    if from_is_us and not to_is_us:
        return Direction.outbound
    if to_is_us and not from_is_us:
        return Direction.inbound
    if from_is_us and to_is_us:
        return Direction.internal
    return Direction.unknown


def counterparty(rec: Recording) -> str | None:
    """The number belonging to the person who is not the operator."""
    if rec.direction == Direction.outbound:
        return rec.to_number or rec.from_number
    if rec.direction == Direction.inbound:
        return rec.from_number or rec.to_number
    for number in (rec.from_number, rec.to_number):
        if number and not is_operator_number(number):
            return number
    return rec.from_number or rec.to_number


# --------------------------------------------------------------------------- #
def register_recording(
    s: Session,
    source_path: Path,
    *,
    from_number: str | None = None,
    to_number: str | None = None,
    direction: Direction | None = None,
    call_started_at: dt.datetime | None = None,
    pbx_call_id: str | None = None,
    consent_announced: bool = False,
    copy: bool = True,
) -> tuple[Recording, bool]:
    """Take custody of a recording file. Returns (recording, created).

    Idempotent on content hash: re-running a directory scan, or a PBX retrying
    a webhook, will not create a second row or a second transcription job.
    """
    source_path = Path(source_path)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)

    digest = sha256_file(source_path)
    existing = s.scalar(select(Recording).where(Recording.sha256 == digest))
    if existing:
        log.debug("recording %s already known as id=%s", source_path.name, existing.id)
        return existing, False

    parsed = parse_recording_filename(source_path.name)
    from_number = normalize_phone(from_number) or normalize_phone(parsed["from_number"])
    to_number = normalize_phone(to_number) or normalize_phone(parsed["to_number"])
    started = to_naive_utc(call_started_at) or parsed["call_started_at"]

    resolved_direction = direction or parsed["direction"]
    if resolved_direction in (None, Direction.unknown):
        resolved_direction = infer_direction(from_number, to_number)

    stored = source_path
    if copy:
        day = (started or dt.datetime.utcnow()).strftime("%Y/%m/%d")
        target_dir = settings.recordings_dir / day
        target_dir.mkdir(parents=True, exist_ok=True)
        stored = target_dir / f"{digest[:12]}_{source_path.name}"
        if not stored.exists():
            shutil.copy2(source_path, stored)

    rec = Recording(
        path=str(stored),
        sha256=digest,
        original_filename=source_path.name,
        bytes=stored.stat().st_size,
        from_number=from_number,
        to_number=to_number,
        direction=resolved_direction,
        call_started_at=started,
        pbx_call_id=pbx_call_id or parsed["pbx_call_id"],
        consent_announced=consent_announced,
        status=RecordingStatus.pending,
    )
    s.add(rec)
    s.flush()
    log.info(
        "registered recording id=%s %s %s -> %s",
        rec.id, rec.direction.value, rec.from_number, rec.to_number,
    )
    return rec, True


def import_directory(
    s: Session, directory: Path | None = None, *, since_days: int | None = None
) -> list[Recording]:
    """Scan a PBX monitor folder (or a copy of one) for recordings we don't have."""
    directory = Path(directory or settings.sangoma_watch_dir or "")
    if not directory.is_dir():
        raise NotADirectoryError(
            f"{directory} is not a directory. Set SANGOMA_WATCH_DIR to the mounted "
            "copy of the PBX's recording folder."
        )

    cutoff = None
    if since_days:
        cutoff = dt.datetime.now().timestamp() - since_days * 86400

    new: list[Recording] = []
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_SUFFIXES:
            continue
        if cutoff and path.stat().st_mtime < cutoff:
            continue
        # Skip files the PBX is still writing.
        if path.stat().st_size == 0:
            continue
        try:
            rec, created = register_recording(s, path)
        except OSError as exc:
            log.warning("could not import %s: %s", path, exc)
            continue
        if created:
            new.append(rec)
    log.info("directory scan of %s: %d new recording(s)", directory, len(new))
    return new


def normalize_webhook_payload(payload: dict) -> dict:
    """Map the many field names a PBX or shim might send onto our own.

    Sangoma products (FreePBX, PBXact, Business Voice) and the community shims
    around them are not consistent about field naming, so accept the common
    spellings rather than demanding one.
    """
    def pick(*keys: str) -> str | None:
        for key in keys:
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
        return None

    started_raw = pick("call_started_at", "start_time", "starttime", "calldate", "timestamp")
    started: dt.datetime | None = None
    if started_raw:
        for parse in (
            lambda v: dt.datetime.fromisoformat(v.replace("Z", "+00:00")),
            lambda v: dt.datetime.fromtimestamp(float(v), dt.UTC),
            lambda v: dt.datetime.strptime(v, "%Y-%m-%d %H:%M:%S"),
        ):
            try:
                started = to_naive_utc(parse(started_raw))
                break
            except (ValueError, OSError, OverflowError):
                continue
        if started is None:
            log.warning("unrecognised call start timestamp: %r", started_raw)

    direction_raw = (pick("direction", "calltype", "type") or "").lower()
    direction = _DIRECTION_TOKENS.get(direction_raw, Direction.unknown)

    return {
        "from_number": pick("from_number", "from", "src", "caller", "callerid", "cnum"),
        "to_number": pick("to_number", "to", "dst", "callee", "destination", "did"),
        "direction": direction,
        "call_started_at": started,
        "pbx_call_id": pick("pbx_call_id", "uniqueid", "call_id", "linkedid", "accountcode"),
        "duration_seconds": pick("duration", "billsec", "duration_seconds"),
        "recording_url": pick("recording_url", "recordingfile_url", "url"),
        "recording_path": pick("recording_path", "recordingfile", "file", "path"),
        "consent_announced": str(
            pick("consent_announced", "announcement_played") or ""
        ).lower() in ("1", "true", "yes"),
    }
