"""Client for the Quest Watersports service tracker (QuestWS/servicetracker).

That app is one Apps Script `/exec` endpoint that answers
`POST {fn, token, args}` sent as `text/plain`. The CRM is simply another
client of it, so nothing over there needs changing to make this work.

Two rules carried over from that repo, deliberately:

* **BiT is never integrated with.** A job id *is* a BiT invoice number, so the
  CRM cannot create jobs. Walk-in intake produces a draft for a person to key
  in; it never invents a ticket.
* **Nothing customer-facing is ever sent.** The only write this client makes is
  `addWriterNote`, which creates a `writer_note` - shop-only by construction,
  filtered out of the customer view by `customerView_` rather than by anyone
  remembering to exclude it.
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
# Apps Script tokens are sealed with a timestamp; re-signing in is cheap and a
# stale token is an unhelpful failure mode mid-sync.
TOKEN_TTL_SECONDS = 30 * 60


class ServiceTrackerError(RuntimeError):
    pass


class NotConfigured(ServiceTrackerError):
    pass


class ServiceTrackerClient:
    def __init__(self, exec_url: str | None = None, password: str | None = None) -> None:
        self.exec_url = (
            exec_url if exec_url is not None else settings.servicetracker_exec_url
        )
        self.password = (
            password if password is not None else settings.servicetracker_password
        )
        self._token: str | None = None
        self._token_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return bool(self.exec_url and self.password)

    # ------------------------------------------------------------------ #
    def _post(self, fn: str, args: list | None = None, token: str | None = None) -> dict:
        if not self.configured:
            raise NotConfigured(
                "Set SERVICETRACKER_EXEC_URL and SERVICETRACKER_PASSWORD in .env. "
                "The /exec URL is the one in the service tracker's "
                "assets/lib/config.js."
            )
        payload = {"fn": fn, "args": args or []}
        if token:
            payload["token"] = token
        try:
            # text/plain on purpose: it is what the shop's pages send, and it
            # keeps the browser preflight off Apps Script.
            resp = httpx.post(
                self.exec_url,
                content=json.dumps(payload),
                headers={"Content-Type": "text/plain;charset=utf-8"},
                timeout=TIMEOUT,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise ServiceTrackerError(f"Service tracker unreachable: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            # Apps Script answers an auth failure with an HTML login page.
            raise ServiceTrackerError(
                "Service tracker returned something that is not JSON. Usually this "
                "means the /exec deployment is not set to run as the owner with "
                "access for anyone."
            ) from exc

        if isinstance(body, dict) and body.get("error"):
            raise ServiceTrackerError(str(body["error"]))
        return body

    def _admin_token(self, force: bool = False) -> str:
        with self._lock:
            fresh = self._token and (time.time() - self._token_at) < TOKEN_TTL_SECONDS
            if fresh and not force:
                return self._token  # type: ignore[return-value]
            body = self._post("adminSignIn", [self.password])
            token = body.get("token")
            if not token:
                raise ServiceTrackerError(
                    "Service tracker did not return a token. Check "
                    "SERVICETRACKER_PASSWORD against ADMIN_PASSWORD over there."
                )
            self._token, self._token_at = token, time.time()
            return token

    def _call(self, fn: str, args: list | None = None) -> dict:
        """An admin call that re-signs in once if the token has gone stale."""
        try:
            return self._post(fn, args, token=self._admin_token())
        except ServiceTrackerError as exc:
            if "sign in" not in str(exc).lower() and "token" not in str(exc).lower():
                raise
            log.info("service tracker token rejected; signing in again")
            return self._post(fn, args, token=self._admin_token(force=True))

    # ------------------------------------------------------------------ #
    def ping(self) -> bool:
        return bool(self._post("ping").get("ok"))

    def list_jobs(self, status: str = "open") -> list[dict]:
        """Job summaries. `status` is 'open', 'all', or a specific status."""
        return self._call("listJobs", [{"status": status}]).get("jobs", [])

    def get_job(self, job_id: str) -> dict:
        return self._call("getJob", [job_id])

    def add_writer_note(self, job_id: str, text: str) -> dict:
        """Add a 'From the office' note to a job's log.

        `writer_note` is shop-only over there: `customerView_` returns
        `customer_note` and nothing else, so this can never surface to a
        customer even if the tracking page is switched back on.
        """
        body = str(text or "").strip()
        if not body:
            raise ServiceTrackerError("Refusing to push an empty note.")
        return self._call("addWriterNote", [job_id, body])

    def set_job_alert(self, job_id: str, text: str) -> dict:
        """The red banner a mechanic sees when they open the job."""
        return self._call("setJobAlert", [job_id, str(text or "").strip()])


_default: ServiceTrackerClient | None = None


def get_client() -> ServiceTrackerClient:
    global _default
    if _default is None:
        _default = ServiceTrackerClient()
    return _default
