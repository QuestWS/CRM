"""Client for the winter services system (QuestWS/winter-quotes_26-27).

Its staff console posts `{api:'console', fn, token, args}` to one Apps Script
`/exec` endpoint and gets `{ok:1, ...}` or `{ok:0, error}` back. Auth is a
staff PIN, not a password, and the token it returns carries that person's
permissions.

Three rules from that repo's CLAUDE.md govern everything here, and they are
why this client is shaped the way it is:

* **Never send email to a customer.** Not a test, not a preview-that-sends.
  So no send function exists on this client at all - `sendEmail`, `bulkSend`
  and the rest of the console's mail surface are deliberately not wrapped.
  A capability that isn't here can't be called by mistake.
* **Treat the sheet as read-only** unless the task is to change a named quote.
  The only write is `staffNote`, against a quote number a person approved.
* **Never put customer PII in the repo.** Nothing here logs a name, phone or
  email - quotes are identified by number in every log line.
"""
from __future__ import annotations

import json
import logging
import threading
import time

import httpx

from crm.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 60.0
# Console sessions last hours over there; re-authing well inside that is cheap
# and avoids a stale token mid-sync.
TOKEN_TTL_SECONDS = 30 * 60


class WinterQuotesError(RuntimeError):
    pass


class NotConfigured(WinterQuotesError):
    pass


class PermissionDenied(WinterQuotesError):
    """The PIN signed in but lacks the permission for this call."""


class WinterQuotesClient:
    def __init__(self, exec_url: str | None = None, pin: str | None = None) -> None:
        self.exec_url = exec_url if exec_url is not None else settings.winter_exec_url
        self.pin = pin if pin is not None else settings.winter_pin
        self._token: str | None = None
        self._token_at: float = 0.0
        self._staff_name: str | None = None
        self._perms: dict = {}
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.exec_url and self.pin)

    @property
    def staff_name(self) -> str | None:
        return self._staff_name

    # ------------------------------------------------------------------ #
    def _post(self, fn: str, args: list | None = None, token: str | None = None) -> dict:
        if not self.configured:
            raise NotConfigured(
                "Set WINTER_EXEC_URL and WINTER_PIN in .env. The /exec URL is the "
                "one in the winter system's admin/index.html (API_URL); the PIN is "
                "a staff console PIN with the 'keys' permission."
            )
        payload = {"api": "console", "fn": fn, "args": args or []}
        if token:
            payload["token"] = token
        try:
            resp = httpx.post(
                self.exec_url,
                content=json.dumps(payload),
                headers={"Content-Type": "text/plain;charset=utf-8"},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise WinterQuotesError(f"Winter system unreachable: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise WinterQuotesError(
                "Winter system returned something that is not JSON - usually a "
                "deployment not set to run as the owner with access for anyone."
            ) from exc

        if not isinstance(body, dict):
            raise WinterQuotesError(f"Unexpected response from {fn}.")
        if body.get("ok") == 0:
            error = str(body.get("error") or "Refused.")
            if "permission" in error.lower() or "not allowed" in error.lower():
                raise PermissionDenied(
                    f"{error} The console PIN needs the 'keys' permission to add "
                    "staff notes."
                )
            raise WinterQuotesError(error)
        return body

    def _auth_token(self, force: bool = False) -> str:
        with self._lock:
            fresh = self._token and (time.time() - self._token_at) < TOKEN_TTL_SECONDS
            if fresh and not force:
                return self._token  # type: ignore[return-value]
            body = self._post("auth", [self.pin])
            token = body.get("token")
            if not token:
                raise WinterQuotesError("Winter system accepted the PIN but sent no token.")
            self._token = token
            self._token_at = time.time()
            self._staff_name = body.get("name")
            self._perms = body.get("perms") or {}
            log.info("winter console session as %s", self._staff_name)
            return token

    def _call(self, fn: str, args: list | None = None) -> dict:
        try:
            return self._post(fn, args, token=self._auth_token())
        except PermissionDenied:
            raise
        except WinterQuotesError as exc:
            if "sign in" not in str(exc).lower() and "session" not in str(exc).lower():
                raise
            log.info("winter console session expired; signing in again")
            return self._post(fn, args, token=self._auth_token(force=True))

    # ------------------------------------------------------------------ #
    # reads
    # ------------------------------------------------------------------ #
    def storage_view(self) -> list[dict]:
        """Every quote across every storage tab, flattened.

        This is the cheap bulk read - one call for the whole season. It carries
        no phone or email, so `lookup` fills those in for the quotes we still
        need them for.
        """
        body = self._call("storageView")
        rows: list[dict] = []
        for group in body.get("groups") or []:
            tab = group.get("tab") or group.get("name") or ""
            for row in group.get("rows") or []:
                rows.append({**row, "storage": tab})
        # Older shapes return a flat list; accept either rather than break on it.
        if not rows and isinstance(body.get("rows"), list):
            rows = list(body["rows"])
        return rows

    def lookup(self, quote_no: str) -> dict:
        """One quote in full - including the phone and email we match on."""
        return self._call("lookup", [quote_no])

    def search(self, last_name: str) -> list[dict]:
        """Search is by LAST NAME only over there, and wants 2+ letters."""
        if len(str(last_name).strip()) < 2:
            raise WinterQuotesError("Winter search needs at least 2 letters of a last name.")
        return self._call("search", [last_name]).get("hits", [])

    # ------------------------------------------------------------------ #
    # the only write
    # ------------------------------------------------------------------ #
    def set_staff_note(self, quote_no: str, note: str) -> dict:
        """Set the staff note on one quote.

        Staff-side only: the note lives in the payload and is never rendered to
        a customer. It replaces rather than appends, which is why the caller
        composes the full text.

        Note that "Nothing changed." is that function's answer when the text is
        already identical. That is a no-op, not a failure, and the caller is
        told so rather than being made to retry.
        """
        body = str(note or "").strip()
        if not body:
            raise WinterQuotesError("Refusing to push an empty note.")
        if len(body) > 4000:
            # Truncated here rather than server-side, so what we store locally
            # matches what the quote actually carries.
            body = body[:3990].rsplit("\n", 1)[0] + "\n[truncated]"
        try:
            return self._call("staffNote", [quote_no, body])
        except WinterQuotesError as exc:
            if "nothing changed" in str(exc).lower():
                return {"ok": 1, "msg": "Note already present.", "unchanged": True}
            raise

    def get_staff_note(self, quote_no: str) -> str:
        return str(self.lookup(quote_no).get("staffNote") or "")


_default: WinterQuotesClient | None = None


def get_client() -> WinterQuotesClient:
    global _default
    if _default is None:
        _default = WinterQuotesClient()
    return _default
