"""Client for EspoCRM's REST API.

EspoCRM is the system of record now: it owns contacts, email, cases, tasks and
the calendar. This service is a sidecar that reads what Espo has fetched, runs
the analysis, and writes the result back.

Verified against the EspoCRM source (v10) rather than from memory:

* Auth is the `X-Api-Key` header on an API User.
  (`Espo\\Core\\Authentication\\Logins\\ApiKey::HEADER_API_KEY`)
* Entities use generic routes - `GET/POST /:controller`,
  `GET/PUT/PATCH/DELETE /:controller/:id`. (`Resources/routes.json`)
* Search is `where[i][type]`, `where[i][attribute]`, `where[i][value]`, plus
  `maxSize`, `offset`, `orderBy`, `order`, `select`.
  (`Core/Record/SearchParamsFetcher`)
* A stream post is a `Note` with `type: "Post"` and `parentType`/`parentId`.
"""
from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urljoin

import httpx

from crm.config import settings

log = logging.getLogger(__name__)

TIMEOUT = 60.0
# Espo's own list views cap around here; going higher just risks a slow query.
MAX_PAGE = 200


class EspoError(RuntimeError):
    pass


class EspoNotConfigured(EspoError):
    pass


class EspoNotFound(EspoError):
    pass


def encode_where(clauses: list[dict]) -> dict[str, Any]:
    """Flatten where clauses into the bracketed query form Espo parses.

    `[{"type": "equals", "attribute": "status", "value": "Sent"}]` becomes
    `where[0][type]=equals&where[0][attribute]=status&where[0][value]=Sent`.
    A list value repeats the key with an empty subscript, which is how PHP
    reads an array.
    """
    params: dict[str, Any] = {}
    for index, clause in enumerate(clauses):
        for key, value in clause.items():
            base = f"where[{index}][{key}]"
            if isinstance(value, (list, tuple)):
                params[f"{base}[]"] = list(value)
            else:
                params[base] = value
    return params


class EspoClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        raw = base_url if base_url is not None else settings.espo_url
        self.base_url = raw.rstrip("/") + "/" if raw else ""
        self.api_key = api_key if api_key is not None else settings.espo_api_key
        self._client: httpx.Client | None = None

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.api_key)

    @property
    def api_root(self) -> str:
        return urljoin(self.base_url, "api/v1/")

    def _http(self) -> httpx.Client:
        if not self.configured:
            raise EspoNotConfigured(
                "Set ESPO_URL and ESPO_API_KEY in .env. The key comes from "
                "Administration → API Users → your API user → Api Key."
            )
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.api_root,
                headers={"X-Api-Key": self.api_key, "Content-Type": "application/json"},
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
            resp = self._http().request(method, path.lstrip("/"), **kwargs)
        except httpx.HTTPError as exc:
            raise EspoError(f"EspoCRM unreachable at {self.api_root}: {exc}") from exc

        if resp.status_code == 404:
            raise EspoNotFound(f"{method} {path} — not found.")
        if resp.status_code in (401, 403):
            # Espo puts the real reason in a header rather than the body.
            reason = resp.headers.get("X-Status-Reason") or resp.text[:200]
            raise EspoError(
                f"EspoCRM refused the request ({resp.status_code}): {reason}. "
                "Check the API key, and that the API user's role grants access "
                "to this entity."
            )
        if resp.status_code >= 400:
            reason = resp.headers.get("X-Status-Reason") or resp.text[:300]
            raise EspoError(f"EspoCRM {resp.status_code} on {method} {path}: {reason}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise EspoError(
                f"EspoCRM returned non-JSON from {path}. Usually the URL points at "
                "the web UI rather than the API root."
            ) from exc

    # ------------------------------------------------------------------ #
    # generic entity access
    # ------------------------------------------------------------------ #
    def list(
        self,
        entity: str,
        *,
        where: list[dict] | None = None,
        max_size: int = 50,
        offset: int = 0,
        order_by: str | None = None,
        order: str = "desc",
        select: list[str] | None = None,
    ) -> dict:
        """One page. Returns Espo's `{"total": n, "list": [...]}`."""
        params: dict[str, Any] = {
            "maxSize": min(max_size, MAX_PAGE),
            "offset": offset,
            "order": order.upper(),
        }
        if order_by:
            params["orderBy"] = order_by
        if select:
            params["select"] = ",".join(select)
        if where:
            params.update(encode_where(where))
        return self._request("GET", entity, params=params) or {"total": 0, "list": []}

    def iterate(
        self, entity: str, *, page_size: int = 100, limit: int | None = None, **kwargs
    ):
        """Page through a collection, stopping at `limit` records."""
        offset, yielded = 0, 0
        while True:
            page = self.list(entity, max_size=page_size, offset=offset, **kwargs)
            rows = page.get("list") or []
            if not rows:
                return
            for row in rows:
                yield row
                yielded += 1
                if limit is not None and yielded >= limit:
                    return
            offset += len(rows)
            if offset >= int(page.get("total") or 0):
                return

    def get(self, entity: str, record_id: str) -> dict:
        return self._request("GET", f"{entity}/{record_id}")

    def create(self, entity: str, data: dict) -> dict:
        return self._request("POST", entity, json=data)

    def update(self, entity: str, record_id: str, data: dict) -> dict:
        return self._request("PUT", f"{entity}/{record_id}", json=data)

    def delete(self, entity: str, record_id: str) -> None:
        self._request("DELETE", f"{entity}/{record_id}")

    def link(self, entity: str, record_id: str, link: str, ids: list[str]) -> None:
        self._request("POST", f"{entity}/{record_id}/{link}", json={"ids": ids})

    # ------------------------------------------------------------------ #
    # the bits this service actually uses
    # ------------------------------------------------------------------ #
    def health(self) -> dict:
        """Confirms the URL, the key and the API user's access in one call."""
        return self._request("GET", "App/user")

    def post_note(
        self,
        parent_type: str,
        parent_id: str,
        text: str,
        *,
        internal: bool = False,
    ) -> dict:
        """Write to a record's stream.

        `isInternal` hides a note from portal users - customers with a portal
        login never see it. Left off by default because most installs have no
        portal, but set it wherever the note is shop-only.
        """
        body = str(text or "").strip()
        if not body:
            raise EspoError("Refusing to post an empty note.")
        return self.create(
            "Note",
            {
                "type": "Post",
                "post": body,
                "parentType": parent_type,
                "parentId": parent_id,
                "isInternal": internal,
            },
        )

    def find_by_email(self, entity: str, address: str) -> dict | None:
        """First Contact/Lead/Account holding this address, or None."""
        page = self.list(
            entity,
            where=[{"type": "equals", "attribute": "emailAddress", "value": address}],
            max_size=1,
        )
        rows = page.get("list") or []
        return rows[0] if rows else None

    def find_by_phone(self, entity: str, number: str) -> dict | None:
        page = self.list(
            entity,
            where=[{"type": "equals", "attribute": "phoneNumber", "value": number}],
            max_size=1,
        )
        rows = page.get("list") or []
        return rows[0] if rows else None

    def recent_emails(
        self, *, since: str | None = None, limit: int = 50, folder: str | None = None
    ) -> list[dict]:
        """Emails Espo has fetched, oldest first so a watermark can advance.

        `since` is an Espo datetime string, `YYYY-MM-DD HH:MM:SS` in UTC.
        """
        where: list[dict] = []
        if since:
            where.append({"type": "after", "attribute": "dateSent", "value": since})
        if folder:
            where.append({"type": "equals", "attribute": "folderId", "value": folder})
        return list(
            self.iterate(
                "Email",
                where=where or None,
                order_by="dateSent",
                order="asc",
                limit=limit,
                select=[
                    "id", "name", "status", "dateSent", "from", "to",
                    "parentType", "parentId", "messageId", "isRead",
                ],
            )
        )

    def email_body(self, email_id: str) -> dict:
        """Full email. The list view does not carry the body."""
        return self.get("Email", email_id)

    def create_task(
        self,
        name: str,
        *,
        description: str | None = None,
        parent_type: str | None = None,
        parent_id: str | None = None,
        date_end: str | None = None,
        priority: str = "Normal",
        assigned_user_id: str | None = None,
    ) -> dict:
        data: dict[str, Any] = {"name": name[:255], "status": "Not Started",
                                "priority": priority}
        if description:
            data["description"] = description
        if parent_type and parent_id:
            data["parentType"] = parent_type
            data["parentId"] = parent_id
        if date_end:
            data["dateEnd"] = date_end
        if assigned_user_id:
            data["assignedUserId"] = assigned_user_id
        return self.create("Task", data)


_default: EspoClient | None = None


def get_client() -> EspoClient:
    global _default
    if _default is None:
        _default = EspoClient()
    return _default
