"""Google OAuth for Calendar.

Email comes in over IMAP (see crm/ingest/imap_mail.py); Google is used only for
the calendar, so the token carries calendar scopes and nothing else. Run
`python -m crm.cli google-auth` once; the refresh token keeps the worker going
unattended after that.
"""
from __future__ import annotations

import logging
from pathlib import Path

from crm.config import settings

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
]


class GoogleNotConfigured(RuntimeError):
    pass


def _load_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_path = Path(settings.google_token_file)
    if not token_path.exists():
        raise GoogleNotConfigured(
            f"No Google token at {token_path}. Run: python -m crm.cli google-auth"
        )
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    if not creds.valid:
        raise GoogleNotConfigured(
            "Stored Google credentials are not valid. Re-run: python -m crm.cli google-auth"
        )
    return creds


def authorize_interactive() -> Path:
    """Run the consent flow in a browser and write the token file."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secret = Path(settings.google_client_secret_file)
    if not secret.exists():
        raise GoogleNotConfigured(
            f"Missing OAuth client secrets at {secret}.\n"
            "Create a Desktop-app OAuth client in Google Cloud Console, download "
            "the JSON, and save it there (see docs/architecture.md)."
        )
    flow = InstalledAppFlow.from_client_secrets_file(str(secret), SCOPES)
    creds = flow.run_local_server(port=0)
    token_path = Path(settings.google_token_file)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    token_path.chmod(0o600)
    log.info("google credentials written to %s", token_path)
    return token_path


def calendar_service():
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=_load_credentials(), cache_discovery=False)


def is_configured() -> bool:
    return Path(settings.google_token_file).exists()
