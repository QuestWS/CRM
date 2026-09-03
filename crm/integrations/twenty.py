"""Client for Twenty CRM's REST API.

Twenty is the system of record: people, companies, notes, tasks, and the
mailbox it syncs over IMAP. This service is a sidecar that reads what Twenty
fetched, runs the analysis, and writes the result back.

Read out of the Twenty source (v1, `packages/twenty-server`) rather than from
memory - these are the details that would have been plausible and wrong:

* Paths come from `ApiPath` in `twenty-shared/types`: REST is `/rest`,
  GraphQL `/graphql`, metadata `/metadata`, health `/healthz`.
* Auth is a JWT API key on `Authorization: Bearer` (`JwtAuthGuard`).
* Objects are `person`, `company`, `note`, `task`, `message`, `messageThread`
  - plural camelCase in REST paths (`/rest/people`, `/rest/notes`).
* **A note does not carry its parent.** Attaching it to a person means creating
  a separate `noteTarget` record. Same for tasks and `taskTarget`. That is the
  main structural difference from every other CRM's API, and the thing most
  likely to be got wrong.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from crm.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 60.0
MAX_PAGE = 60  # Twenty's REST list cap


class TwentyError(RuntimeError):
    pass


class TwentyNotConfigured(TwentyError):
    pass


class TwentyClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        raw = base_url if base_url is not None else settings.twenty_url
        self.base_url = raw.rstrip("/") if raw else ""
        self.api_key = api_key if api_key is not None else settings.twenty_api_key
        self._client: httpx.Client | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    def _http(self) -> httpx.Client:
        if not self.configured:
            raise TwentyNotConfigured(
                "Set TWENTY_URL and TWENTY_API_KEY in .env. The key comes from "
                "Settings → APIs → Create API key in Twenty."
            )
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
                follow_redirects=True,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    # ------------------------------------------------------------------ #
    def _request(self, method: str, path: str, **kwargs) -> Any:
        try:
            resp = self._http().request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise TwentyError(f"Twenty unreachable at {self.base_url}: {exc}") from exc

        if resp.status_code in (401, 403):
            raise TwentyError(
                f"Twenty refused the request ({resp.status_code}). The API key is "
                "wrong, expired, or belongs to another workspace."
            )
        if resp.status_code >= 400:
            detail = resp.text[:400]
            try:
                body = resp.json()
                detail = body.get("messages") or body.get("message") or detail
            except ValueError:
                pass
            raise TwentyError(f"Twenty {resp.status_code} on {method} {path}: {detail}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise TwentyError(
                f"Twenty returned non-JSON from {path}. Usually TWENTY_URL points "
                "at the web app rather than the server."
            ) from exc

    # ------------------------------------------------------------------ #
    # generic REST
    # ------------------------------------------------------------------ #
    def list(
        self,
        obj: str,
        *,
        filter_: str | None = None,
        order_by: str | None = None,
        limit: int = 30,
        starting_after: str | None = None,
        depth: int | None = None,
    ) -> dict:
        """One page of `obj` (plural camelCase, e.g. "people", "messages").

        Twenty paginates with an opaque cursor, not an offset - the response
        carries `pageInfo.endCursor` and `pageInfo.hasNextPage`.
        """
        params: dict[str, Any] = {"limit": min(limit, MAX_PAGE)}
        if filter_:
            params["filter"] = filter_
        if order_by:
            params["orderBy"] = order_by
        if starting_after:
            params["starting_after"] = starting_after
        if depth is not None:
            params["depth"] = depth
        return self._request("GET", f"/rest/{obj}", params=params) or {}

    @staticmethod
    def _records(payload: dict, obj: str) -> list[dict]:
        """Twenty nests the rows under `data.<object>`."""
        data = payload.get("data") or {}
        rows = data.get(obj)
        if rows is None and len(data) == 1:
            rows = next(iter(data.values()))
        return rows or []

    def iterate(self, obj: str, *, page_size: int = 30, limit: int | None = None, **kwargs):
        cursor, yielded = None, 0
        while True:
            page = self.list(obj, limit=page_size, starting_after=cursor, **kwargs)
            rows = self._records(page, obj)
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            info = page.get("pageInfo") or {}
            cursor = info.get("endCursor")
            if not info.get("hasNextPage") or not cursor:
                return

    def get(self, obj: str, record_id: str, *, depth: int | None = None) -> dict:
        params = {"depth": depth} if depth is not None else None
        payload = self._request("GET", f"/rest/{obj}/{record_id}", params=params) or {}
        data = payload.get("data") or {}
        return next(iter(data.values()), {}) if data else {}

    def create(self, obj: str, data: dict) -> dict:
        payload = self._request("POST", f"/rest/{obj}", json=data) or {}
        body = payload.get("data") or {}
        return next(iter(body.values()), {}) if body else {}

    def update(self, obj: str, record_id: str, data: dict) -> dict:
        payload = self._request("PATCH", f"/rest/{obj}/{record_id}", json=data) or {}
        body = payload.get("data") or {}
        return next(iter(body.values()), {}) if body else {}

    # ------------------------------------------------------------------ #
    # what the sidecar actually uses
    # ------------------------------------------------------------------ #
    def health(self) -> bool:
        try:
            self._http().get("/healthz", timeout=15)
        except httpx.HTTPError as exc:
            raise TwentyError(f"Twenty unreachable at {self.base_url}: {exc}") from exc
        return True

    def whoami(self) -> dict:
        """Cheapest call that proves the key works and names the workspace."""
        query = "query { currentWorkspace { id displayName } }"
        body = self._request("POST", "/graphql", json={"query": query}) or {}
        if body.get("errors"):
            raise TwentyError(f"Twenty rejected the API key: {body['errors']}")
        return (body.get("data") or {}).get("currentWorkspace") or {}

    def find_person_by_email(self, address: str) -> dict | None:
        rows = self._records(
            self.list(
                "people",
                filter_=f"emails.primaryEmail[eq]:{address}",
                limit=1,
            ),
            "people",
        )
        return rows[0] if rows else None

    def create_note(self, title: str, body_markdown: str) -> dict:
        """A note on its own. Attach it with `attach_note` - Twenty keeps the
        link in a separate record."""
        return self.create(
            "notes",
            {
                "title": title[:255],
                # Twenty stores rich text as a blocknote/markdown pair; the
                # markdown half is what round-trips cleanly through the API.
                "bodyV2": {"markdown": body_markdown, "blocknote": None},
            },
        )

    def attach_note(self, note_id: str, *, person_id: str | None = None,
                    company_id: str | None = None, opportunity_id: str | None = None) -> dict:
        target: dict[str, Any] = {"noteId": note_id}
        if person_id:
            target["personId"] = person_id
        if company_id:
            target["companyId"] = company_id
        if opportunity_id:
            target["opportunityId"] = opportunity_id
        if len(target) == 1:
            raise TwentyError("A note target needs a person, company or opportunity.")
        return self.create("noteTargets", target)

    def post_note(
        self,
        title: str,
        body_markdown: str,
        *,
        person_id: str | None = None,
        company_id: str | None = None,
    ) -> dict:
        """Create a note and link it in one go - the common case."""
        note = self.create_note(title, body_markdown)
        note_id = note.get("id")
        if note_id and (person_id or company_id):
            self.attach_note(note_id, person_id=person_id, company_id=company_id)
        return note

    def create_task(
        self,
        title: str,
        *,
        body_markdown: str | None = None,
        person_id: str | None = None,
        company_id: str | None = None,
        due_at: str | None = None,
        status: str = "TODO",
    ) -> dict:
        data: dict[str, Any] = {"title": title[:255], "status": status}
        if body_markdown:
            data["bodyV2"] = {"markdown": body_markdown, "blocknote": None}
        if due_at:
            data["dueAt"] = due_at
        task = self.create("tasks", data)
        task_id = task.get("id")
        if task_id and (person_id or company_id):
            target: dict[str, Any] = {"taskId": task_id}
            if person_id:
                target["personId"] = person_id
            if company_id:
                target["companyId"] = company_id
            self.create("taskTargets", target)
        return task

    def recent_messages(self, *, since: str | None = None, limit: int = 50) -> list[dict]:
        """Messages Twenty synced from the mailbox, oldest first.

        Oldest first so a watermark can advance safely: newest-first would
        strand anything that arrived while a page was being processed.
        """
        filter_ = f"receivedAt[gte]:{since}" if since else None
        return list(
            self.iterate(
                "messages",
                filter_=filter_,
                order_by="receivedAt",
                limit=limit,
                depth=1,  # pulls messageParticipants so we can see who it is
            )
        )


_default: TwentyClient | None = None


def get_client() -> TwentyClient:
    global _default
    if _default is None:
        _default = TwentyClient()
    return _default
