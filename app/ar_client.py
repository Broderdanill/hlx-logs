from __future__ import annotations

import logging
from urllib.parse import quote
import re

import httpx

from .settings import ArSettings

logger = logging.getLogger(__name__)


class ArRestError(RuntimeError):
    pass


def _entry_id_from_links(entry: dict) -> str | None:
    """Extract AR REST entry id from an entry _links.self href.

    Custom forms may expose Request ID as e.g. Request ID__c, or not include it
    in the requested values at all. The AR REST response normally includes the
    canonical entry URL in _links.self, so use that as the stable entry-id source.
    """
    links = entry.get("_links") or {}
    self_link = links.get("self")
    href = None
    if isinstance(self_link, list) and self_link:
        href = self_link[0].get("href")
    elif isinstance(self_link, dict):
        href = self_link.get("href")
    if not href:
        return None
    return href.rstrip("/").split("/")[-1]


def _friendly_request_error(exc: httpx.RequestError, base_url: str) -> ArRestError:
    return ArRestError(
        "Could not reach AR REST API at "
        f"{base_url!r}: {exc}. Check AR_BASE_URL/config.yaml from inside the "
        "hlx-logs container, for example with curl to /api/jwt/login."
    )


class ArClient:
    def __init__(self, settings: ArSettings, jwt: str | None = None):
        self.settings = settings
        self.jwt = jwt
        self.client = httpx.AsyncClient(
            base_url=settings.base_url,
            verify=settings.verify_tls,
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self.client.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {"X-Requested-By": "XMLHttpRequest"}
        if self.jwt:
            headers["Authorization"] = f"AR-JWT {self.jwt}"
        return headers

    async def login(self, username: str, password: str) -> str:
        logger.debug("Logging in to AR REST at %s", self.settings.base_url)
        try:
            response = await self.client.post(
                "/api/jwt/login",
                data={"username": username, "password": password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"AR login failed: HTTP {response.status_code}: {response.text[:500]}")
        token = response.text.strip().strip('"')
        if not token:
            raise ArRestError("AR login returned an empty token")
        self.jwt = token
        return token

    async def logout(self) -> None:
        if not self.jwt:
            return
        try:
            await self.client.post("/api/jwt/logout", headers=self._headers())
        except httpx.RequestError:
            logger.debug("AR logout request failed", exc_info=True)
        finally:
            self.jwt = None


    async def user_is_member_of_group(self, *, username: str, user_form: str = "User", login_field: str = "Login Name", group_list_field: str = "Group List", group_id: str = "1") -> bool:
        """Return True when the authenticated user record contains group_id.

        This uses AR REST with the JWT that was just created. The default
        configuration checks the classic User form and Group List field for
        Administrator group id 1. Group List values vary between environments
        (semicolon separated, space separated, or display values), so parsing is
        intentionally tolerant and only matches complete numeric tokens.
        """
        form = quote(user_form, safe="")
        safe_user = username.replace('"', '\"')
        q = f"'{login_field}' = \"{safe_user}\""
        fields = f"values({group_list_field})"
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}",
                params={"q": q, "fields": fields, "limit": "1"},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Admin group verification failed: HTTP {response.status_code}: {response.text[:1000]}")
        entries = response.json().get("entries", [])
        if not entries:
            return False
        values = entries[0].get("values", {})
        group_value = str(values.get(group_list_field, ""))
        tokens = re.findall(r"\d+", group_value)
        return str(group_id) in tokens

    async def create_log_request(self, pod: str, directory: str, filename: str, transaction_id: str) -> str | None:
        form = quote(self.settings.form_name, safe="")
        payload = {
            "values": {
                "Pod": pod,
                "Directory": directory,
                "TransactionId": transaction_id,
                "Filename": filename,
            }
        }
        logger.info("Requesting log: pod=%s directory=%s filename=%s transaction=%s", pod, directory, filename, transaction_id)
        try:
            response = await self.client.post(f"/api/arsys/v1/entry/{form}", json=payload, headers=self._headers())
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Create log request failed: HTTP {response.status_code}: {response.text[:1000]}")
        location = response.headers.get("Location") or response.headers.get("location")
        if location:
            return location.rstrip("/").split("/")[-1]
        try:
            body = response.json()
            return body.get("entryId") or _entry_id_from_links(body)
        except Exception:
            logger.debug("Create response had no JSON body", exc_info=True)
            return None

    async def get_entry(self, entry_id: str) -> dict:
        form = quote(self.settings.form_name, safe="")
        fields = "values(Pod,Directory,Filename,TransactionId,Status__c)"
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}/{quote(entry_id, safe='')}",
                params={"fields": fields},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Get entry failed for {entry_id}: HTTP {response.status_code}: {response.text[:1000]}")
        return response.json()

    async def query_entries_by_transaction(self, transaction_id: str) -> list[dict]:
        form = quote(self.settings.form_name, safe="")
        q = self.settings.result_query_template.format(transaction_id=transaction_id.replace('"', '\\"'))
        logger.debug("Querying transaction entries: %s", q)
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}",
                params={"q": q, "fields": "values(Pod,Directory,Filename,TransactionId,Status__c)", "limit": "1000"},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Query transaction failed: HTTP {response.status_code}: {response.text[:1000]}")
        data = response.json()
        return data.get("entries", [])

    async def download_attachment(self, entry_id: str, field_name: str | None = None) -> bytes:
        """Download an attachment using the documented AR REST endpoint.

        BMC documents this as:
        GET /api/arsys/v1/entry/{formName}/{entryId}/attach/{fieldName}
        For HLX:Logs the attachment field name is normally 1EX, not the label
        "Log File".
        """
        form = quote(self.settings.form_name, safe="")
        field_name = field_name or self.settings.attachment_field
        field = quote(field_name, safe="")
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}/{quote(entry_id, safe='')}/attach/{field}",
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(
                f"Download attachment failed for entry {entry_id}, field {field_name}: "
                f"HTTP {response.status_code}: {response.text[:1000]}"
            )
        return response.content
