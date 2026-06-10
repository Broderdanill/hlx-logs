from __future__ import annotations

import logging
from urllib.parse import quote

import httpx

from .settings import ArSettings

logger = logging.getLogger(__name__)


class ArRestError(RuntimeError):
    pass


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
        response = await self.client.post(
            "/api/jwt/login",
            data={"username": username, "password": password},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
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
        finally:
            self.jwt = None

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
        response = await self.client.post(f"/api/arsys/v1/entry/{form}", json=payload, headers=self._headers())
        if response.status_code >= 400:
            raise ArRestError(f"Create log request failed: HTTP {response.status_code}: {response.text[:1000]}")
        location = response.headers.get("Location") or response.headers.get("location")
        if location:
            return location.rstrip("/").split("/")[-1]
        try:
            body = response.json()
            return body.get("values", {}).get("Request ID") or body.get("entryId")
        except Exception:
            logger.debug("Create response had no JSON body", exc_info=True)
            return None

    async def query_entries_by_transaction(self, transaction_id: str) -> list[dict]:
        form = quote(self.settings.form_name, safe="")
        q = self.settings.result_query_template.format(transaction_id=transaction_id.replace('"', '\\"'))
        logger.debug("Querying transaction entries: %s", q)
        response = await self.client.get(
            f"/api/arsys/v1/entry/{form}",
            params={"q": q, "fields": "values(Request ID,Pod,Directory,Filename,TransactionId)", "limit": "1000"},
            headers=self._headers(),
        )
        if response.status_code >= 400:
            raise ArRestError(f"Query transaction failed: HTTP {response.status_code}: {response.text[:1000]}")
        data = response.json()
        return data.get("entries", [])

    async def download_attachment(self, entry_id: str, field_name: str | None = None) -> bytes:
        form = quote(self.settings.form_name, safe="")
        field = quote(field_name or self.settings.attachment_field, safe="")
        response = await self.client.get(f"/api/arsys/v1/entry/{form}/{quote(entry_id, safe='')}/attach/{field}", headers=self._headers())
        if response.status_code >= 400:
            raise ArRestError(f"Download attachment failed for {entry_id}/{field}: HTTP {response.status_code}: {response.text[:1000]}")
        return response.content
