"""Central configuration, loaded from environment / .env."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        # The repo's .env first, then one in the working directory if there is
        # one (later wins). Without the absolute path, running from cron or a
        # systemd unit silently picks up no configuration at all.
        env_file=(REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- identity of the operator (used to tell "me" from "the caller") ---
    operator_name: str = Field("Me", alias="OPERATOR_NAME")
    operator_business: str = Field("", alias="OPERATOR_BUSINESS")
    # What the business actually sells and services, in your own words. Fed to
    # the analyst so it routes calls against your real lines rather than a
    # generic guess. One per line or comma separated.
    business_lines: str = Field(
        "Powerboat sales and service; sailboats; kayaks and paddleboards; "
        "e-bikes; a boat club; winter storage, shrink wrap and winterization; "
        "parts and accessories",
        alias="BUSINESS_LINES",
    )
    operator_email: str = Field("", alias="OPERATOR_EMAIL")
    operator_aliases: str = Field("", alias="OPERATOR_ALIASES")  # comma separated
    operator_numbers: str = Field("", alias="OPERATOR_NUMBERS")  # comma separated
    default_region: str = Field("US", alias="DEFAULT_REGION")  # for phone parsing
    timezone: str = Field("America/Chicago", alias="TIMEZONE")

    # --- storage ---
    database_url: str = Field("sqlite:///./data/crm.db", alias="DATABASE_URL")
    media_root: Path = Field(REPO_ROOT / "media", alias="MEDIA_ROOT")

    # --- anthropic ---
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    claude_model: str = Field("claude-opus-5", alias="CLAUDE_MODEL")
    claude_effort: str = Field("medium", alias="CLAUDE_EFFORT")

    # --- transcription ---
    transcription_engine: str = Field("faster_whisper", alias="TRANSCRIPTION_ENGINE")
    whisper_model: str = Field("small.en", alias="WHISPER_MODEL")
    whisper_device: str = Field("auto", alias="WHISPER_DEVICE")
    whisper_compute_type: str = Field("int8", alias="WHISPER_COMPUTE_TYPE")
    split_stereo_channels: bool = Field(True, alias="SPLIT_STEREO_CHANNELS")
    deepgram_api_key: str = Field("", alias="DEEPGRAM_API_KEY")
    # The shop already transcribes mechanic voice notes with AssemblyAI, so the
    # same key and the same vendor cover the CRM's calls too.
    assemblyai_api_key: str = Field("", alias="ASSEMBLYAI_API_KEY")

    # --- sangoma / pbx ingest ---
    sangoma_webhook_secret: str = Field("", alias="SANGOMA_WEBHOOK_SECRET")
    # The mobile app's Upload URL cannot send custom auth headers, so the secret
    # lives in the path instead: /api/app/<token>/recording
    app_upload_token: str = Field("", alias="APP_UPLOAD_TOKEN")
    app_upload_max_mb: int = Field(200, alias="APP_UPLOAD_MAX_MB")
    sangoma_watch_dir: Path | None = Field(None, alias="SANGOMA_WATCH_DIR")
    sangoma_api_base: str = Field("", alias="SANGOMA_API_BASE")
    sangoma_client_id: str = Field("", alias="SANGOMA_CLIENT_ID")
    sangoma_client_secret: str = Field("", alias="SANGOMA_CLIENT_SECRET")

    # --- email (imap) ---
    imap_host: str = Field("", alias="IMAP_HOST")
    imap_port: int = Field(993, alias="IMAP_PORT")
    imap_username: str = Field("", alias="IMAP_USERNAME")
    imap_password: str = Field("", alias="IMAP_PASSWORD")
    imap_ssl: bool = Field(True, alias="IMAP_SSL")
    # Blank means: auto-discover INBOX plus whatever the server flags as \Sent.
    imap_folders: str = Field("", alias="IMAP_FOLDERS")
    imap_backfill_days: int = Field(30, alias="IMAP_BACKFILL_DAYS")
    imap_max_per_sync: int = Field(200, alias="IMAP_MAX_PER_SYNC")

    # --- service tracker (QuestWS/servicetracker) ---
    # The shop's work-order app. One Apps Script /exec endpoint; the CRM is
    # just another client of it. Never talks to BiT - same rule as over there.
    servicetracker_exec_url: str = Field("", alias="SERVICETRACKER_EXEC_URL")
    servicetracker_password: str = Field("", alias="SERVICETRACKER_PASSWORD")
    servicetracker_sync_minutes: int = Field(10, alias="SERVICETRACKER_SYNC_MINUTES")
    # Staged notes wait for a click by default. The shop's culture is that
    # nothing leaves on a timer; keep it that way unless asked otherwise.
    servicetracker_autopush: bool = Field(False, alias="SERVICETRACKER_AUTOPUSH")

    # --- google (calendar only) ---
    google_client_secret_file: Path = Field(
        REPO_ROOT / ".credentials/client_secret.json", alias="GOOGLE_CLIENT_SECRET_FILE"
    )
    google_token_file: Path = Field(
        REPO_ROOT / ".credentials/token.json", alias="GOOGLE_TOKEN_FILE"
    )
    calendar_id: str = Field("primary", alias="CALENDAR_ID")

    # --- scheduling ---
    email_poll_minutes: int = Field(10, alias="EMAIL_POLL_MINUTES")
    pipeline_poll_seconds: int = Field(30, alias="PIPELINE_POLL_SECONDS")
    briefing_hour: int = Field(7, alias="BRIEFING_HOUR")

    # --- web ---
    host: str = Field("127.0.0.1", alias="HOST")
    port: int = Field(8000, alias="PORT")

    @field_validator("database_url", mode="after")
    @classmethod
    def _anchor_sqlite(cls, value: str) -> str:
        """Anchor a relative sqlite file to the repo, for the same reason as above."""
        prefix = "sqlite:///"
        if not value.startswith(prefix):
            return value
        raw = value[len(prefix) :]
        if not raw or raw.startswith("/") or raw == ":memory:":
            return value
        return prefix + str((REPO_ROOT / raw).resolve())

    @field_validator("sangoma_watch_dir", mode="before")
    @classmethod
    def _blank_path_is_none(cls, value):
        """An unset SANGOMA_WATCH_DIR must stay None, not become Path(".")."""
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("media_root", "google_client_secret_file", "google_token_file",
                     "sangoma_watch_dir", mode="after")
    @classmethod
    def _anchor_to_repo(cls, value):
        """Resolve relative paths against the repo, not the current directory.

        Otherwise a cron job or systemd unit with a different working directory
        silently writes recordings somewhere else.
        """
        if value is None:
            return None
        path = Path(value)
        return path if path.is_absolute() else (REPO_ROOT / path).resolve()

    @property
    def operator_email_list(self) -> list[str]:
        """Every address that means 'me' - used to tell sent mail from received."""
        candidates = [
            self.operator_email,
            self.imap_username,
            *self.operator_aliases.split(","),
        ]
        seen: dict[str, None] = {}
        for value in candidates:
            addr = value.strip().lower()
            if addr and "@" in addr:
                seen[addr] = None
        return list(seen)

    @property
    def business_line_list(self) -> list[str]:
        raw = self.business_lines.replace("\n", ";")
        return [line.strip() for line in raw.split(";") if line.strip()]

    @property
    def servicetracker_configured(self) -> bool:
        return bool(self.servicetracker_exec_url and self.servicetracker_password)

    @property
    def imap_folder_list(self) -> list[str]:
        return [f.strip() for f in self.imap_folders.split(",") if f.strip()]

    @property
    def imap_configured(self) -> bool:
        return bool(self.imap_host and self.imap_username and self.imap_password)

    @property
    def operator_number_list(self) -> list[str]:
        return [n.strip() for n in self.operator_numbers.split(",") if n.strip()]

    @property
    def recordings_dir(self) -> Path:
        return self.media_root / "recordings"


@lru_cache
def get_settings() -> Settings:
    s = Settings()  # type: ignore[call-arg]
    s.recordings_dir.mkdir(parents=True, exist_ok=True)
    if s.database_url.startswith("sqlite:///"):
        Path(s.database_url.replace("sqlite:///", "")).parent.mkdir(
            parents=True, exist_ok=True
        )
    return s


settings = get_settings()
