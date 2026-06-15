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


def _safe_ar_string(value: str) -> str:
    """Escape a string value for simple AR REST qualifications."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _looks_like_missing_entry_error(message: str) -> bool:
    """Return True for AR REST messages that mean the requested row is absent."""
    lowered = str(message or "").lower()
    needles = (
        "could not find setting",
        "no entries",
        "no entry",
        "entry does not exist",
        "does not exist",
        "not found",
        'messagenumber":302',
        "messagenumber:302",
        "message number 302",
    )
    return any(needle in lowered for needle in needles)


def _friendly_request_error(exc: httpx.RequestError, base_url: str) -> ArRestError:
    return ArRestError(
        "Could not reach AR REST API at "
        f"{base_url!r}: {exc}. Check AR_BASE_URL/config.yaml from inside the "
        "hlx-logs container, for example with curl to /api/jwt/login."
    )


LOG_CONTROL_DEFINITIONS = {
    # AR server Debug-mode bitmask values and the related filename setting rows
    # in AR System Configuration Component Setting. Filename settings are saved
    # as separate Setting Value updates, for example Filter-Log-File.
    "sql": {"label": "SQL", "default_filename": "arsql.log", "bit_value": 1, "filename_setting": "SQL-Log-File"},
    "filter": {"label": "Filter", "default_filename": "arfilter.log", "bit_value": 2, "filename_setting": "Filter-Log-File"},
    "user": {"label": "User", "default_filename": "aruser.log", "bit_value": 4, "filename_setting": "User-Log-File"},
    "escalation": {"label": "Escalation", "default_filename": "arescl.log", "bit_value": 8, "filename_setting": "Escalation-Log-File"},
    "api": {"label": "API", "default_filename": "arapi.log", "bit_value": 16, "filename_setting": "API-Log-File"},
    "thread": {"label": "Thread", "default_filename": "arthread.log", "bit_value": 32, "filename_setting": "Thread-Log-File"},
    "alert": {"label": "Alert", "default_filename": "aralert.log", "bit_value": 64, "filename_setting": "Alert-Log-File"},
    "servergroup": {"label": "Server Group", "default_filename": "arsrvgrp.log", "bit_value": 256, "filename_setting": "Server-Group-Log-File"},
    "fts": {"label": "Full Text Index", "default_filename": "arftindx.log", "bit_value": 512, "filename_setting": "Full-Text-Indexer-Log-File"},
    "archive": {"label": "Archive", "default_filename": "ararchive.log", "bit_value": 1024, "filename_setting": "Archive-Log-File"},
    "dso": {"label": "Distributed Server", "default_filename": "ardist.log", "bit_value": 32768, "filename_setting": "DSO-Log-File"},
    "approval": {"label": "Approval", "default_filename": "arapprov.log", "bit_value": 65536, "filename_setting": "Approval-Log-File"},
    "plugin": {"label": "Plug-in", "default_filename": "arplugin.log", "bit_value": 131072, "filename_setting": "Plugin-Log-File"},
}

LOG_SETTING_TEMPLATES = [
    {"key": "none", "label": "None / disable server debug", "logs": []},
    {"key": "filter", "label": "Filter", "logs": ["filter"]},
    {"key": "workflow", "label": "Workflow trace", "logs": ["filter", "escalation", "api"]},
    {"key": "sql_filter", "label": "SQL + Filter", "logs": ["sql", "filter"]},
    {"key": "api", "label": "API", "logs": ["api"]},
    {"key": "performance", "label": "Performance / SQL", "logs": ["sql", "api", "thread"]},
    {"key": "server", "label": "Server diagnostics", "logs": ["api", "thread", "servergroup", "alert"]},
    {"key": "all_core", "label": "All supported debug logs", "logs": ["sql", "filter", "user", "escalation", "api", "thread", "alert", "servergroup", "fts", "archive", "dso", "approval", "plugin"]},
]

LOG_CONTROL_BY_FILENAME = {v["default_filename"].lower(): k for k, v in LOG_CONTROL_DEFINITIONS.items()}

def log_control_key_for_filename(filename: str) -> str | None:
    name = (filename or "").strip().lower()
    if name in LOG_CONTROL_BY_FILENAME:
        return LOG_CONTROL_BY_FILENAME[name]
    if "filter" in name:
        return "filter"
    if "sql" in name:
        return "sql"
    if "api" in name:
        return "api"
    if "escal" in name:
        return "escalation"
    if "thread" in name:
        return "thread"
    if "user" in name:
        return "user"
    if "alert" in name:
        return "alert"
    if "plugin" in name and "java" in name:
        return "javaplugin"
    if "plugin" in name:
        return "plugin"
    if "email" in name or "mail" in name:
        return "email"
    if "cmdbservice" in name:
        return "cmdbservice"
    if "cmdb" in name:
        return "cmdb"
    if "process" in name:
        return "process"
    return None


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


    async def query_entries(self, form_name: str, *, q: str = "", fields: str = "", limit: int = 1000) -> list[dict]:
        """Generic AR REST entry query helper."""
        form = quote(form_name, safe="")
        params: dict[str, str] = {"limit": str(limit)}
        if q:
            params["q"] = q
        if fields:
            params["fields"] = fields
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}",
                params=params,
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Query failed for form {form_name}: HTTP {response.status_code}: {response.text[:1000]}")
        return response.json().get("entries", [])

    async def _get_entry(self, form_name: str, *, entry_id: str, fields: str = "") -> dict:
        """Read one AR REST entry. Used to discover physical field aliases."""
        form = quote(form_name, safe="")
        params: dict[str, str] = {}
        if fields:
            params["fields"] = fields
        try:
            response = await self.client.get(
                f"/api/arsys/v1/entry/{form}/{quote(str(entry_id), safe='')}",
                params=params,
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Read failed for {form_name}/{entry_id}: HTTP {response.status_code}: {response.text[:1000]}")
        return response.json()

    async def get_server_config_setting(self, *, server_name: str, setting_name: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Read one server configuration setting row from the Component Setting view."""
        safe_server = _safe_ar_string(server_name)
        safe_setting = _safe_ar_string(setting_name)
        q = "('Setting Name' = \"" + safe_setting + "\") AND ('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"" + safe_server + "\")"
        entries = await self.query_entries(
            form_name,
            q=q,
            fields="values(Setting Value,Setting Name,Component Type,Component Name,Configuration Setting GUID,Configuration Component GUID)",
            limit=1,
        )
        if not entries:
            raise ArRestError(f"Could not find setting {setting_name!r} for server {server_name!r} in {form_name}.")
        entry = entries[0]
        values = entry.get("values") or {}
        raw = str(values.get("Setting Value", "")).strip()
        return {"entry_id": _entry_id_from_links(entry), "setting_name": setting_name, "value": raw, "raw": raw, "values": values}

    async def get_server_debug_mode(self, *, server_name: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Read the AR server Debug-mode bitmask row for one server/pod.

        The row is selected by:
          Setting Name = Debug-mode
          Component Type = com.bmc.arsys.server
          Component Name = server_name
        """
        row = await self.get_server_config_setting(server_name=server_name, setting_name="Debug-mode", form_name=form_name)
        raw = str(row.get("raw", "0")).strip() or "0"
        try:
            value = int(raw)
        except ValueError:
            raise ArRestError(f"Debug-mode Setting Value for {server_name!r} is not numeric: {raw!r}")
        row["value"] = value
        row["raw"] = raw
        return row

    async def _put_entry_value(self, *, form_name: str, entry_id: str, values: dict[str, str]) -> int:
        form = quote(form_name, safe="")
        try:
            response = await self.client.put(
                f"/api/arsys/v1/entry/{form}/{quote(str(entry_id), safe='')}",
                json={"values": values},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"PUT failed for {form_name}/{entry_id}: HTTP {response.status_code}: {response.text[:1200]}")
        return response.status_code

    async def _put_entry_value_field_ids(self, *, form_name: str, entry_id: str, values: dict[int | str, str]) -> int:
        """PUT an entry using numeric AR field IDs as JSON keys.

        Some BMC/internal forms do not expose the same field names on the
        physical schema that the join/display form shows. In those cases AR
        REST can still accept numeric field IDs, for example 3205 for the
        Debug-mode Setting Value.
        """
        form = quote(form_name, safe="")
        payload_values = {str(k): v for k, v in values.items()}
        try:
            response = await self.client.put(
                f"/api/arsys/v1/entry/{form}/{quote(str(entry_id), safe='')}",
                json={"values": payload_values},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"PUT failed for {form_name}/{entry_id} using field ids: HTTP {response.status_code}: {response.text[:1200]}")
        return response.status_code


    async def _delete_entry(self, *, form_name: str, entry_id: str) -> int:
        form = quote(form_name, safe="")
        try:
            response = await self.client.delete(
                f"/api/arsys/v1/entry/{form}/{quote(str(entry_id), safe='')}",
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"DELETE failed for {form_name}/{entry_id}: HTTP {response.status_code}: {response.text[:1200]}")
        return response.status_code

    async def _verify_server_debug_mode(self, *, server_name: str, expected_value: int, form_name: str) -> dict:
        verified = await self.get_server_debug_mode(server_name=server_name, form_name=form_name)
        if verified["value"] != int(expected_value):
            raise ArRestError(
                f"Debug-mode verification failed for {server_name}: expected {int(expected_value)}, "
                f"but AR still returns {verified['value']}."
            )
        return verified

    async def _verify_server_config_setting_value(self, *, server_name: str, setting_name: str, expected_value: str, form_name: str) -> dict:
        verified = await self.get_server_config_setting(server_name=server_name, setting_name=setting_name, form_name=form_name)
        if str(verified.get("raw", "")).strip() != str(expected_value).strip():
            raise ArRestError(
                f"{setting_name} verification failed for {server_name}: expected {expected_value!r}, "
                f"but AR still returns {verified.get('raw')!r}."
            )
        return verified

    async def _put_secondary_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, current: dict, form_name: str) -> dict:
        """Update the underlying AR System Configuration Setting row for one server setting.

        The Component Setting form is a join form. The REST entry id normally
        contains both join-side ids, for example primary|secondary|secondary.
        For filename settings the Configuration Setting GUID is not a valid
        REST entry id on the physical form, so prefer the secondary id from the
        join entry id and only use the GUID as a last resort.
        """
        secondary_form = "AR System Configuration Setting"
        values = current.get("values") or {}
        setting_guid = str(values.get("Configuration Setting GUID") or "").strip()
        join_entry_id = str(current.get("entry_id") or "").strip()
        errors: list[str] = []
        candidate_entry_ids: list[str] = []

        def add_candidate(value: str | None) -> None:
            value = str(value or "").strip()
            if value and value not in candidate_entry_ids:
                candidate_entry_ids.append(value)

        # AR REST join entry ids are often pipe-delimited. The secondary
        # physical row id is commonly the second/third segment. Try those first.
        if "|" in join_entry_id:
            parts = [part for part in join_entry_id.split("|") if part]
            for part in parts[1:]:
                add_candidate(part)

        # Query the physical row by the join key if a GUID is available. Do not
        # request guessed value fields here; AR fails the whole query when any
        # requested field alias does not exist.
        if setting_guid:
            q = f"'179' = \"{_safe_ar_string(setting_guid)}\""
            try:
                entries = await self.query_entries(secondary_form, q=q, fields="", limit=3)
                for entry in entries:
                    add_candidate(_entry_id_from_links(entry))
            except ArRestError as exc:
                errors.append(f"query {secondary_form} by field id 179: {exc}")
            add_candidate(setting_guid)

        # Most installations expose the physical Setting Value as Value. Keep a
        # few aliases as fallbacks, but avoid numeric 3205 because some versions
        # report that it does not exist on the physical REST schema.
        value_field_candidates = [
            "Value",
            "SettingValue",
            "Setting Value",
            "Configuration Setting Value",
            "SettingValueEncrypt",
        ]
        for entry_id in list(candidate_entry_ids):
            try:
                entry = await self._get_entry(secondary_form, entry_id=entry_id)
                for key in (entry.get("values") or {}).keys():
                    low = str(key).lower().replace(" ", "")
                    if "value" in low and str(key) not in value_field_candidates:
                        value_field_candidates.append(str(key))
            except ArRestError as exc:
                errors.append(f"read {secondary_form}/{entry_id}: {exc}")

        for entry_id in candidate_entry_ids:
            seen_fields: set[str] = set()
            for field_name in value_field_candidates:
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                try:
                    status = await self._put_entry_value(
                        form_name=secondary_form,
                        entry_id=entry_id,
                        values={field_name: str(setting_value)},
                    )
                    verified = await self._verify_server_config_setting_value(
                        server_name=server_name,
                        setting_name=setting_name,
                        expected_value=str(setting_value),
                        form_name=form_name,
                    )
                    return {
                        "server_name": server_name,
                        "setting_name": setting_name,
                        "entry_id": entry_id,
                        "old_value": current.get("raw", current.get("value", "")),
                        "new_value": verified.get("raw", ""),
                        "status_code": status,
                        "method": f"PUT {secondary_form}.{field_name}",
                    }
                except ArRestError as exc:
                    errors.append(f"PUT {secondary_form}/{entry_id} field {field_name}: {exc}")

        detail = "; ".join(errors[-10:]) if errors else "no candidate secondary row found"
        raise ArRestError(f"Could not update underlying {secondary_form} row for {server_name} / {setting_name}: {detail}")

    async def _put_join_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, current: dict, form_name: str) -> dict:
        entry_id = current.get("entry_id")
        if not entry_id:
            raise ArRestError(f"Could not determine entry id for setting {setting_name!r} / server {server_name!r}.")
        values = current.get("values") or {}
        payload_values = {
            "Setting Name": values.get("Setting Name", setting_name),
            "Component Type": values.get("Component Type", "com.bmc.arsys.server"),
            "Component Name": values.get("Component Name", server_name),
            "Setting Value": str(setting_value),
        }
        status = await self._put_entry_value(form_name=form_name, entry_id=entry_id, values=payload_values)
        verified = await self._verify_server_config_setting_value(server_name=server_name, setting_name=setting_name, expected_value=str(setting_value), form_name=form_name)
        return {
            "server_name": server_name,
            "setting_name": setting_name,
            "entry_id": entry_id,
            "old_value": current.get("raw", current.get("value", "")),
            "new_value": verified.get("raw", ""),
            "status_code": status,
            "method": f"PUT {form_name}",
        }

    async def _merge_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, current: dict, form_name: str) -> dict:
        safe_server = _safe_ar_string(server_name)
        safe_setting = _safe_ar_string(setting_name)
        qualification = "('Setting Name' = \"" + safe_setting + "\") AND ('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"" + safe_server + "\")"
        form = quote(form_name, safe="")
        payload = {
            "values": {
                "Setting Name": setting_name,
                "Component Type": "com.bmc.arsys.server",
                "Component Name": server_name,
                "Setting Value": str(setting_value),
            },
            "mergeOptions": {
                "ignorePatterns": False,
                "ignoreRequired": False,
                "workflowEnabled": True,
                "associationsEnabled": False,
                "mergeType": "DUP_MERGE",
                "multimatchOption": 0,
            },
            "qualification": qualification,
        }
        try:
            response = await self.client.post(
                f"/api/arsys/v1/mergeEntry/{form}",
                json=payload,
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"mergeEntry {setting_name} failed for {server_name}: HTTP {response.status_code}: {response.text[:1200]}")
        verified = await self._verify_server_config_setting_value(server_name=server_name, setting_name=setting_name, expected_value=str(setting_value), form_name=form_name)
        return {
            "server_name": server_name,
            "setting_name": setting_name,
            "entry_id": current.get("entry_id"),
            "old_value": current.get("raw", current.get("value", "")),
            "new_value": verified.get("raw", ""),
            "status_code": response.status_code,
            "method": "mergeEntry",
        }

    async def set_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        current = await self.get_server_config_setting(server_name=server_name, setting_name=setting_name, form_name=form_name)
        attempts: list[str] = []
        for updater in (self._put_secondary_server_config_setting_value, self._put_join_server_config_setting_value, self._merge_server_config_setting_value):
            try:
                return await updater(server_name=server_name, setting_name=setting_name, setting_value=str(setting_value), current=current, form_name=form_name)
            except ArRestError as exc:
                attempts.append(f"{updater.__name__}: {exc}")
                logger.warning("%s update attempt failed for %s using %s: %s", setting_name, server_name, updater.__name__, exc)
        raise ArRestError(f"Update {setting_name} failed for {server_name}. " + " | ".join(attempts))


    async def upsert_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Set a server config setting, creating it via mergeEntry when missing."""
        try:
            return await self.set_server_config_setting_value(server_name=server_name, setting_name=setting_name, setting_value=setting_value, form_name=form_name)
        except ArRestError as exc:
            if not _looks_like_missing_entry_error(str(exc)):
                raise
        safe_server = _safe_ar_string(server_name)
        safe_setting = _safe_ar_string(setting_name)
        qualification = "('Setting Name' = \"" + safe_setting + "\") AND ('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"" + safe_server + "\")"
        form = quote(form_name, safe="")
        payload = {
            "values": {
                "Setting Name": setting_name,
                "Component Type": "com.bmc.arsys.server",
                "Component Name": server_name,
                "Setting Value": str(setting_value),
            },
            "mergeOptions": {
                "ignorePatterns": False,
                "ignoreRequired": False,
                "workflowEnabled": True,
                "associationsEnabled": False,
                "mergeType": "DUP_MERGE",
                "multimatchOption": 0,
            },
            "qualification": qualification,
        }
        try:
            response = await self.client.post(f"/api/arsys/v1/mergeEntry/{form}", json=payload, headers=self._headers())
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"mergeEntry {setting_name} failed for {server_name}: HTTP {response.status_code}: {response.text[:1200]}")
        verified = await self._verify_server_config_setting_value(server_name=server_name, setting_name=setting_name, expected_value=str(setting_value), form_name=form_name)
        return {
            "server_name": server_name,
            "setting_name": setting_name,
            "entry_id": verified.get("entry_id"),
            "old_value": "",
            "new_value": verified.get("raw", ""),
            "status_code": response.status_code,
            "method": "mergeEntry upsert",
        }

    async def delete_server_config_setting(self, *, server_name: str, setting_name: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Delete a server config setting row from the Component Setting view.

        Used for disabling optional settings such as Restrict-Log-Users. Delete
        is idempotent: if the row is already missing, report a clean no-op.
        """
        try:
            current = await self.get_server_config_setting(server_name=server_name, setting_name=setting_name, form_name=form_name)
        except ArRestError as exc:
            if _looks_like_missing_entry_error(str(exc)):
                return {"server_name": server_name, "setting_name": setting_name, "deleted": False, "status_code": 0, "message": "already absent"}
            raise
        entry_id = current.get("entry_id")
        if not entry_id:
            return {"server_name": server_name, "setting_name": setting_name, "deleted": False, "status_code": 0, "message": "already absent"}
        try:
            status = await self._delete_entry(form_name=form_name, entry_id=entry_id)
        except ArRestError as exc:
            if _looks_like_missing_entry_error(str(exc)):
                return {"server_name": server_name, "setting_name": setting_name, "deleted": False, "status_code": 0, "message": "already absent"}
            raise
        return {"server_name": server_name, "setting_name": setting_name, "deleted": True, "status_code": status}

    async def _put_secondary_server_debug_mode(self, *, server_name: str, debug_mode: int, current: dict, form_name: str) -> dict:
        """Update the underlying AR System Configuration Setting row.

        AR System Configuration Component Setting is a join form. The uploaded
        definition shows Setting Value mapped from secondary form index 1, and
        the join qualifier links primary field 3206 to secondary field 179. In
        practice, writing the join form can return success without changing the
        underlying configuration row, so write AR System Configuration Setting
        directly and verify through the join form afterwards.
        """
        secondary_form = "AR System Configuration Setting"
        values = current.get("values") or {}
        setting_guid = str(values.get("Configuration Setting GUID") or "").strip()
        errors: list[str] = []

        # AR System Configuration Component Setting is a join form. Its
        # Configuration Setting GUID is mapped from the primary join row, but
        # the join qualifier links that value to field id 179 on the physical
        # AR System Configuration Setting row. In REST, the physical form may
        # not expose the human-readable join field names, so use numeric field
        # IDs when querying/updating it.
        candidate_entry_ids: list[str] = []
        if setting_guid:
            candidate_entry_ids.append(setting_guid)

            q = f"'179' = \"{_safe_ar_string(setting_guid)}\""
            try:
                entries = await self.query_entries(secondary_form, q=q, fields="values(179,3204,3205)", limit=2)
                for entry in entries:
                    entry_id = _entry_id_from_links(entry)
                    if entry_id and entry_id not in candidate_entry_ids:
                        candidate_entry_ids.append(entry_id)
            except ArRestError as exc:
                errors.append(f"query {secondary_form} by field id 179: {exc}")

        # The physical AR System Configuration Setting form does not always
        # expose the join-view labels (for example "Setting Value") through
        # REST, and some environments also reject numeric JSON keys such as
        # "3205" on PUT.  The most reliable approach is therefore to try the
        # real physical value field names first.  In BMC configuration forms
        # this is commonly just "Value", while the join view renames it to
        # "Setting Value".
        value_field_candidates = [
            "Value",
            "value",
            "SettingValue",
            "Setting Value",
            "Setting Value__c",
            "Configuration Setting Value",
            "SettingValueEncrypt",
        ]

        # If the physical entry can be read, add any returned key that looks
        # like a value field. This makes the updater adapt to field aliases in
        # different AR versions/customizations.
        for entry_id in list(candidate_entry_ids):
            try:
                entry = await self._get_entry(secondary_form, entry_id=entry_id)
                for key in (entry.get("values") or {}).keys():
                    low = str(key).lower().replace(" ", "")
                    if "value" in low and str(key) not in value_field_candidates:
                        value_field_candidates.append(str(key))
            except ArRestError as exc:
                errors.append(f"read {secondary_form}/{entry_id}: {exc}")

        for entry_id in candidate_entry_ids:
            seen_fields: set[str] = set()
            for field_name in value_field_candidates:
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                try:
                    status = await self._put_entry_value(
                        form_name=secondary_form,
                        entry_id=entry_id,
                        values={field_name: str(int(debug_mode))},
                    )
                    verified = await self._verify_server_debug_mode(server_name=server_name, expected_value=debug_mode, form_name=form_name)
                    return {
                        "server_name": server_name,
                        "entry_id": entry_id,
                        "old_value": current["value"],
                        "new_value": verified["value"],
                        "status_code": status,
                        "method": f"PUT {secondary_form}.{field_name}",
                    }
                except ArRestError as exc:
                    errors.append(f"PUT {secondary_form}/{entry_id} field {field_name}: {exc}")

            # Last-resort only: some AR REST versions accept numeric-looking
            # keys on query but not on update. Keep this fallback for versions
            # that do accept it, but do not rely on it first.
            try:
                status = await self._put_entry_value_field_ids(
                    form_name=secondary_form,
                    entry_id=entry_id,
                    values={3205: str(int(debug_mode))},
                )
                verified = await self._verify_server_debug_mode(server_name=server_name, expected_value=debug_mode, form_name=form_name)
                return {
                    "server_name": server_name,
                    "entry_id": entry_id,
                    "old_value": current["value"],
                    "new_value": verified["value"],
                    "status_code": status,
                    "method": f"PUT {secondary_form} field 3205",
                }
            except ArRestError as exc:
                errors.append(f"PUT {secondary_form}/{entry_id} field 3205: {exc}")

        detail = "; ".join(errors[-10:]) if errors else "no candidate secondary row found"
        raise ArRestError(f"Could not update underlying {secondary_form} row for {server_name}: {detail}")

    async def _put_join_server_debug_mode(self, *, server_name: str, debug_mode: int, current: dict, form_name: str) -> dict:
        """Try a normal AR REST PUT update for the join-form Debug-mode row."""
        entry_id = current.get("entry_id")
        if not entry_id:
            raise ArRestError(f"Could not determine entry id for Debug-mode setting row for {server_name!r}.")
        values = current.get("values") or {}
        payload_values = {
            "Setting Name": values.get("Setting Name", "Debug-mode"),
            "Component Type": values.get("Component Type", "com.bmc.arsys.server"),
            "Component Name": values.get("Component Name", server_name),
            "Setting Value": str(int(debug_mode)),
        }
        status = await self._put_entry_value(form_name=form_name, entry_id=entry_id, values=payload_values)
        verified = await self._verify_server_debug_mode(server_name=server_name, expected_value=debug_mode, form_name=form_name)
        return {
            "server_name": server_name,
            "entry_id": entry_id,
            "old_value": current["value"],
            "new_value": verified["value"],
            "status_code": status,
            "method": f"PUT {form_name}",
        }

    async def _merge_server_debug_mode(self, *, server_name: str, debug_mode: int, current: dict, form_name: str) -> dict:
        """Fallback update using AR REST mergeEntry with a qualification."""
        safe_server = _safe_ar_string(server_name)
        qualification = "('Setting Name' = \"Debug-mode\") AND ('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"" + safe_server + "\")"
        form = quote(form_name, safe="")
        payload = {
            "values": {
                "Setting Name": "Debug-mode",
                "Component Type": "com.bmc.arsys.server",
                "Component Name": server_name,
                "Setting Value": str(int(debug_mode)),
            },
            "mergeOptions": {
                "ignorePatterns": False,
                "ignoreRequired": False,
                "workflowEnabled": True,
                "associationsEnabled": False,
                "mergeType": "DUP_MERGE",
                "multimatchOption": 0,
            },
            "qualification": qualification,
        }
        try:
            response = await self.client.post(
                f"/api/arsys/v1/mergeEntry/{form}",
                json=payload,
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"mergeEntry Debug-mode failed for {server_name}: HTTP {response.status_code}: {response.text[:1200]}")
        verified = await self._verify_server_debug_mode(server_name=server_name, expected_value=debug_mode, form_name=form_name)
        return {
            "server_name": server_name,
            "entry_id": current.get("entry_id"),
            "old_value": current["value"],
            "new_value": verified["value"],
            "status_code": response.status_code,
            "method": "mergeEntry",
        }

    async def set_server_debug_mode(self, *, server_name: str, debug_mode: int, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Update Setting Value for the server Debug-mode bitmask row.

        This reads from the Component Setting join form but writes the underlying
        AR System Configuration Setting row first, because Setting Value is
        mapped from the secondary form in the join definition. Every attempted
        write is verified by reading the join form again before reporting success.
        """
        current = await self.get_server_debug_mode(server_name=server_name, form_name=form_name)
        attempts: list[str] = []
        for updater in (self._put_secondary_server_debug_mode, self._put_join_server_debug_mode, self._merge_server_debug_mode):
            try:
                return await updater(server_name=server_name, debug_mode=debug_mode, current=current, form_name=form_name)
            except ArRestError as exc:
                attempts.append(f"{updater.__name__}: {exc}")
                logger.warning("Debug-mode update attempt failed for %s using %s: %s", server_name, updater.__name__, exc)
        raise ArRestError(f"Update Debug-mode failed for {server_name}. " + " | ".join(attempts))

    async def discover_pods(self, *, form_name: str, query: str, value_field: str) -> list[str]:
        fields = f"values({value_field})"
        entries = await self.query_entries(form_name, q=query, fields=fields, limit=10000)
        pods: list[str] = []
        seen: set[str] = set()
        for entry in entries:
            value = str((entry.get("values") or {}).get(value_field, "")).strip()
            if value and value not in seen:
                seen.add(value)
                pods.append(value)
        return pods

    async def discover_log_files_for_pod(
        self,
        pod: str,
        *,
        form_name: str,
        server_field: str,
        filename_field: str,
        size_field: str,
    ) -> list[dict]:
        safe_pod = pod.replace('"', '\"')
        q = f"'{server_field}' = \"{safe_pod}\""
        fields = f"values({filename_field},{size_field})"
        entries = await self.query_entries(form_name, q=q, fields=fields, limit=10000)
        logs: list[dict] = []
        seen: set[str] = set()
        for entry in entries:
            values = entry.get("values") or {}
            filename = str(values.get(filename_field, "")).strip()
            if not filename or filename in seen:
                continue
            seen.add(filename)
            logs.append({
                "filename": filename,
                "file_size": str(values.get(size_field, "")).strip(),
                "entry_id": _entry_id_from_links(entry),
            })
        logs.sort(key=lambda item: item["filename"].lower())
        return logs


    async def submit_entry(self, form_name: str, values: dict) -> dict:
        """Submit a generic AR REST entry and return response details.

        Used for display/service forms where server-side workflow performs the
        actual work. AR REST accepts field names in the values object, which
        keeps this helper independent of field IDs.
        """
        form = quote(form_name, safe="")
        payload = {"values": values}
        try:
            response = await self.client.post(f"/api/arsys/v1/entry/{form}", json=payload, headers=self._headers())
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Submit to form {form_name} failed: HTTP {response.status_code}: {response.text[:1200]}")
        body = None
        try:
            body = response.json() if response.text else None
        except Exception:
            body = None
        return {
            "status_code": response.status_code,
            "location": response.headers.get("Location") or response.headers.get("location"),
            "body": body,
        }

    async def set_all_server_logs(self, *, enable: bool, form_name: str = "AR System Server Group Log Management") -> dict:
        """Enable or disable the common AR System server logs via BMC's log-management service form.

        This mirrors the important inputs used by the bundled
        `AR System Server Group Log:SetLogs` active link. The target form is a
        display/service form; submitting the entry runs server-side workflow that
        writes AR System Configuration Component Setting records.
        """
        yes_or_none = "Yes" if enable else None
        true_or_none = "True" if enable else None
        save = "Save"
        values = {
            "SelectMode": "All",
            "Operation": "Set",
            "logfileappend": "T",
            "maxlogfilesize": "0",
            "maxloginstance": "10",
            "perthreadlogs": "Yes" if enable else None,
            # Core AR server logs
            "API": yes_or_none,
            "apilogfile": "arapi.log",
            "SaveApiSetting": save,
            "Escalation Log": yes_or_none,
            "escalationlogfile": "aresclator.log",
            "SaveEscSetting": save,
            "Filter Log": yes_or_none,
            "filterlogfile": "arfilter.log",
            "SaveFilterSetting": save,
            "SQL": yes_or_none,
            "sqllogfile": "arsql.log",
            "SaveSQLSetting": save,
            "Thread": yes_or_none,
            "threadlogfile": "arthread.log",
            "SaveThreadSetting": save,
            "User Log": yes_or_none,
            "userlogfile": "aruser.log",
            "SaveUserSetting": save,
            "Alert": yes_or_none,
            "alertlogfile": "aralert.log",
            "SaveAlertSetting": save,
            "Full Text Index": yes_or_none,
            "ftindexerlogfile": "arfts.log",
            "SaveFTSSetting": save,
            "Server Group": yes_or_none,
            "servergrouplogfile": "arservergroup.log",
            "SaveServerGroupSetting": save,
            "Archive Log": yes_or_none,
            "archivelogfile": "ararchive.log",
            "SaveArchiveSetting": save,
            "Distributed Server": yes_or_none,
            "dsologfile": "ardso.log",
            "DSO Log Level": "1" if enable else None,
            "SaveDSOSetting": save,
            # Plug-in / Java plug-in / Approval / Assignment / Email / CMDB / Flashboard / Process
            "Plug-In Server": yes_or_none,
            "pluginlogfile": "arplugin.log",
            "Plugin Log Level:": "INFO" if enable else None,
            "SavePluginSetting": save,
            "Arpscf_fld_EnableLogs": true_or_none,
            "Arpscf_fld_LogFile": "arjavaplugin.log",
            "Arpscf_fld_LogLevel": "INFO" if enable else None,
            "SaveJavaPluginSetting": save,
            "Approval Log": "Approval" if enable else None,
            "approvallogfile": "arapproval.log",
            "Approval Log Level:": "1" if enable else None,
            "SaveApprovalSetting": save,
            "AE-Log-Enabled": true_or_none,
            "SaveAssignmentSetting": save,
            "EmailLog": true_or_none,
            "EmailEngineLogName": "aremail.log",
            "EmailLogLevel": "INFO" if enable else None,
            "SaveEmailSetting": save,
            "CMDBEngineLog": true_or_none,
            "CMDBEngineLogName": "arcmdb.log",
            "CMDBEngineLogLevel": "INFO" if enable else None,
            "SaveCMDBSetting": save,
            "CMDBServiceLog": true_or_none,
            "CMDBServiceLogName": "arcmdbservice.log",
            "CMDBServiceLogLevel": "INFO" if enable else None,
            "SaveCMDBServiceLog": save,
            "FlashboardLog": true_or_none,
            "Flashboard_LogName": "arflashboard.log",
            "Flashboard_LogLevel": "INFO" if enable else None,
            "SaveFlashboardLog": save,
            "ProcessLog": true_or_none,
            "ProcessLogName": "arprocess.log",
            "SaveProcessSetting": save,
        }
        # Avoid sending null filename/log-level fields when disabling; AR workflow
        # only needs the checkbox/save fields to clear the corresponding settings.
        if not enable:
            values = {k: v for k, v in values.items() if v is not None or k in {"SelectMode", "Operation"}}
        result = await self.submit_entry(form_name, values)
        result["action"] = "enable" if enable else "disable"
        result["form"] = form_name
        result["sent_fields"] = sorted(values.keys())
        return result

    async def set_server_log(self, *, log_key: str, enable: bool, filename: str | None = None, form_name: str = "AR System Server Group Log Management") -> dict:
        """Enable/disable one AR server log type and optionally set its log filename.

        The values mirror the fields used by BMC's AR System Server Group Log
        Management display form. One request sends only the selected log type's
        enable field, filename field and Save*Setting field, so a UI can toggle
        logs one by one rather than using a broad all-logs operation.
        """
        key = (log_key or "").strip().lower()
        definition = LOG_CONTROL_DEFINITIONS.get(key)
        if not definition:
            raise ArRestError(f"Unknown log control key: {log_key}")
        chosen_filename = (filename or definition.get("default_filename") or "").strip()
        if definition.get("filename_field") and (chosen_filename.startswith("/") or ":" in chosen_filename or ".." in chosen_filename or chosen_filename.startswith("\\")):
            raise ArRestError("Log filename must be a relative filename/path, not an absolute path.")
        values = {
            "SelectMode": "Selected",
            "Operation": "Set",
            "logfileappend": "T",
            "maxlogfilesize": "0",
            "maxloginstance": "10",
            definition["enable_field"]: definition["on_value"] if enable else None,
            definition["save_field"]: "Save",
        }
        if definition.get("filename_field") and chosen_filename:
            values[definition["filename_field"]] = chosen_filename
        if enable and definition.get("level_field") and definition.get("level_value"):
            values[definition["level_field"]] = definition["level_value"]
        # Keep the disabled value in the payload; many AR display-form workflows
        # distinguish between a missing field and an explicitly cleared field.
        result = await self.submit_entry(form_name, values)
        result["action"] = "enable" if enable else "disable"
        result["form"] = form_name
        result["log_key"] = key
        result["filename"] = chosen_filename
        result["sent_fields"] = sorted(values.keys())
        return result

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
