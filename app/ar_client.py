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


def _entry_id_from_location(location: str | None) -> str | None:
    """Extract an AR REST entry id from a Location response header."""
    if not location:
        return None
    return str(location).rstrip("/").split("/")[-1] or None


def _safe_ar_string(value: str) -> str:
    """Escape a string value for simple AR REST qualifications."""
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')



def _norm_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _first_value(values: dict, *candidate_names: str) -> str:
    """Return the first value from a dict using tolerant AR field aliases."""
    if not values:
        return ""
    for name in candidate_names:
        if name in values and values.get(name) is not None:
            return str(values.get(name) or "").strip()
    normalized = {_norm_key(key): key for key in values.keys()}
    for name in candidate_names:
        key = normalized.get(_norm_key(name))
        if key is not None and values.get(key) is not None:
            return str(values.get(key) or "").strip()
    return ""


def _configuration_component_guid(values: dict) -> str:
    # In AR System Configuration Component Setting, field 3207 is the
    # Configuration Component GUID. Field 179 is used by the physical
    # AR System Configuration Component form and some join forms.
    return _first_value(values, "Configuration Component GUID", "Component GUID", "ComponentGuid", "3207", "179")


def _configuration_setting_guid(values: dict) -> str:
    # In AR System Configuration Component Setting / Component-Setting
    # Mapping, field 3206 is Configuration Setting GUID. In the physical
    # AR System Configuration Setting form it is field 179.
    return _first_value(values, "Configuration Setting GUID", "Setting GUID", "SettingGuid", "3206", "179")


def _configuration_setting_name(values: dict) -> str:
    return _first_value(values, "Setting Name", "SettingName", "Name", "3204")


def _configuration_component_name(values: dict) -> str:
    return _first_value(values, "Component Name", "ComponentName", "Name", "3200")


def _configuration_component_type(values: dict) -> str:
    return _first_value(values, "Component Type", "ComponentType", "Type", "3201")


def _configuration_setting_value(values: dict) -> str:
    return _first_value(values, "Setting Value", "Value", "SettingValue", "Configuration Setting Value", "SettingValueEncrypt", "3205")


def _value_field_candidates(values: dict | None = None) -> list[str]:
    candidates = [
        "Value",
        "value",
        "SettingValue",
        "Setting Value",
        "Setting Value__c",
        "Configuration Setting Value",
        "SettingValueEncrypt",
    ]
    if values:
        for key in values.keys():
            low = str(key).lower().replace(" ", "")
            if "value" in low and str(key) not in candidates:
                candidates.append(str(key))
    return candidates

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


def _looks_like_missing_form_error(message: str, form_name: str) -> bool:
    """Return True when AR REST reports that a specific form/schema is absent."""
    lowered = str(message or "").lower()
    compact = re.sub(r"\s+", "", lowered)
    form_lower = str(form_name or "").lower()
    if form_lower and form_lower not in lowered:
        return False
    needles = (
        "http 404",
        "form does not exist",
        "schema does not exist",
        "specified form does not exist",
        "the form specified does not exist",
        "not a valid form",
        "form not found",
        "schema not found",
        "message number 303",
        "arerr 303",
        "arerr [303]",
    )
    compact_needles = (
        'messagenumber":303',
        "messagenumber:303",
    )
    return any(needle in lowered for needle in needles) or any(needle in compact for needle in compact_needles)


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


COMPONENT_FORM = "AR System Configuration Component"
CONFIGURATION_SETTING_FORM = "AR System Configuration Setting"
COMPONENT_SETTING_FORM = "AR System Configuration Component Setting"
COMPONENT_SETTING_MAPPING_FORM = "AR System Configuration Component-Setting Mapping"
BLOCKED_COMPONENT_SETTING_FORM = "AR System Block Configuration Component Setting"
BLOCKED_COMPONENT_SETTING_STATUS = "Blocked Setting"
ALLOWED_COMPONENT_SETTING_STATUS = "Allowed Setting"


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
        physical schema that the join/display form shows. A few environments
        accept numeric field IDs through REST, so this helper is retained only
        as a compatibility fallback.
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

    async def _submit_entry_values(self, *, form_name: str, values: dict[int | str, str]) -> dict:
        """Submit one AR REST entry using field names or numeric field IDs."""
        form = quote(form_name, safe="")
        payload_values = {str(k): v for k, v in values.items()}
        try:
            response = await self.client.post(
                f"/api/arsys/v1/entry/{form}",
                json={"values": payload_values},
                headers=self._headers(),
            )
        except httpx.RequestError as exc:
            raise _friendly_request_error(exc, self.settings.base_url) from exc
        if response.status_code >= 400:
            raise ArRestError(f"Submit to {form_name} failed: HTTP {response.status_code}: {response.text[:1200]}")
        body = None
        try:
            body = response.json() if response.text else None
        except Exception:
            body = None
        return {
            "status_code": response.status_code,
            "entry_id": _entry_id_from_location(response.headers.get("Location") or response.headers.get("location")),
            "body": body,
        }


    def _component_metadata_from_entry(self, entry: dict, *, server_name: str) -> dict:
        values = entry.get("values") or {}
        return {
            "entry_id": _entry_id_from_links(entry),
            "values": values,
            "component_guid": _configuration_component_guid(values),
            "component_type": _configuration_component_type(values) or "com.bmc.arsys.server",
            "component_name": _configuration_component_name(values) or server_name,
        }

    def _configuration_setting_metadata_from_entry(self, entry: dict, *, setting_name: str) -> dict:
        values = entry.get("values") or {}
        return {
            "entry_id": _entry_id_from_links(entry),
            "values": values,
            "setting_guid": _configuration_setting_guid(values),
            "setting_name": _configuration_setting_name(values) or setting_name,
            "raw": _configuration_setting_value(values),
        }

    async def _query_entries_best_effort(self, form_name: str, *, q_candidates: list[str], limit: int = 10, fields: str = "") -> tuple[list[dict], list[str]]:
        """Try several AR qualifications and return the first non-empty result."""
        errors: list[str] = []
        for q in q_candidates:
            try:
                entries = await self.query_entries(form_name, q=q, fields=fields, limit=limit)
            except ArRestError as exc:
                errors.append(f"{q}: {exc}")
                continue
            if entries:
                return entries, errors
        return [], errors

    async def get_configuration_setting_metadata(self, *, setting_name: str, component_guid: str | None = None, limit: int = 25) -> dict:
        """Find a row in AR System Configuration Setting and return its GUID.

        AR System Configuration Component Setting stores Configuration Setting
        GUID as the key back to AR System Configuration Setting.  Optional
        rows may be absent from Component Setting even though the setting row is
        already present in Configuration Setting, so use this lookup before
        creating/updating a component-setting link.
        """
        safe_setting = _safe_ar_string(setting_name)
        # AR System Configuration Setting is the physical setting form.  In the
        # supplied definitions, the display field is named "Name" (field 3204)
        # and the setting GUID is field 179.  It does not contain Component
        # Type/Name or Configuration Component GUID, so do not query those here.
        q_candidates: list[str] = [
            f"'Name' = \"{safe_setting}\"",
            f"'3204' = \"{safe_setting}\"",
            # Some environments expose the same field with the join label.
            f"'Setting Name' = \"{safe_setting}\"",
        ]
        entries, errors = await self._query_entries_best_effort(CONFIGURATION_SETTING_FORM, q_candidates=q_candidates, limit=limit)
        if not entries:
            detail = "; ".join(errors[-4:]) if errors else "no matching rows returned"
            raise ArRestError(f"Could not find setting {setting_name!r} in {CONFIGURATION_SETTING_FORM}: {detail}")

        def score(entry: dict) -> int:
            values = entry.get("values") or {}
            result = 0
            row_name = _configuration_setting_name(values)
            if row_name and row_name.lower() == setting_name.lower():
                result += 20
            if _configuration_setting_guid(values):
                result += 10
            if component_guid:
                row_component_guid = _configuration_component_guid(values)
                if row_component_guid and row_component_guid == component_guid:
                    result += 100
            return result

        best = max(entries, key=score)
        metadata = self._configuration_setting_metadata_from_entry(best, setting_name=setting_name)
        if not metadata.get("setting_guid") and metadata.get("entry_id"):
            try:
                full_entry = await self._get_entry(CONFIGURATION_SETTING_FORM, entry_id=str(metadata.get("entry_id")))
                metadata = self._configuration_setting_metadata_from_entry(full_entry, setting_name=setting_name)
                metadata["entry_id"] = metadata.get("entry_id") or _entry_id_from_links(best)
            except ArRestError as exc:
                logger.debug("Could not read full %s row for %s: %s", CONFIGURATION_SETTING_FORM, setting_name, exc)
        if component_guid and _configuration_component_guid(metadata.get("values") or {}) and _configuration_component_guid(metadata.get("values") or {}) != component_guid:
            logger.debug(
                "%s match for %s had a different Configuration Component GUID than %s; using best available setting GUID.",
                CONFIGURATION_SETTING_FORM,
                setting_name,
                component_guid,
            )
        return metadata

    async def _put_configuration_setting_value(
        self,
        *,
        setting_name: str,
        setting_value: str,
        setting_metadata: dict,
        verify_server_name: str | None = None,
        form_name: str = COMPONENT_SETTING_FORM,
    ) -> dict:
        """Update the physical AR System Configuration Setting value row.

        This is used both for normal updates and before creating a missing
        Component Setting link.  The caller may pass verify_server_name to
        verify through AR System Configuration Component Setting after the PUT.
        """
        setting_guid = str(setting_metadata.get("setting_guid") or "").strip()
        metadata_values = setting_metadata.get("values") or {}
        errors: list[str] = []
        candidate_entry_ids: list[str] = []

        def add_candidate(value: str | None) -> None:
            value = str(value or "").strip()
            if value and value not in candidate_entry_ids:
                candidate_entry_ids.append(value)

        add_candidate(setting_metadata.get("entry_id"))
        if setting_guid:
            q = f"'179' = \"{_safe_ar_string(setting_guid)}\""
            try:
                entries = await self.query_entries(CONFIGURATION_SETTING_FORM, q=q, fields="", limit=3)
                for entry in entries:
                    add_candidate(_entry_id_from_links(entry))
            except ArRestError as exc:
                errors.append(f"query {CONFIGURATION_SETTING_FORM} by field id 179: {exc}")
            add_candidate(setting_guid)

        value_field_candidates = _value_field_candidates(metadata_values)
        for entry_id in list(candidate_entry_ids):
            try:
                entry = await self._get_entry(CONFIGURATION_SETTING_FORM, entry_id=entry_id)
                value_field_candidates = _value_field_candidates({**metadata_values, **(entry.get("values") or {})})
            except ArRestError as exc:
                errors.append(f"read {CONFIGURATION_SETTING_FORM}/{entry_id}: {exc}")

        for entry_id in candidate_entry_ids:
            seen_fields: set[str] = set()
            for field_name in value_field_candidates:
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                try:
                    status = await self._put_entry_value(
                        form_name=CONFIGURATION_SETTING_FORM,
                        entry_id=entry_id,
                        values={field_name: str(setting_value)},
                    )
                    verified = None
                    if verify_server_name:
                        verified = await self._verify_server_config_setting_value(
                            server_name=verify_server_name,
                            setting_name=setting_name,
                            expected_value=str(setting_value),
                            form_name=form_name,
                        )
                    return {
                        "entry_id": entry_id,
                        "setting_guid": setting_guid,
                        "status_code": status,
                        "method": f"PUT {CONFIGURATION_SETTING_FORM}.{field_name}",
                        "verified": verified,
                    }
                except ArRestError as exc:
                    errors.append(f"PUT {CONFIGURATION_SETTING_FORM}/{entry_id} field {field_name}: {exc}")

        detail = "; ".join(errors[-10:]) if errors else "no candidate configuration setting row found"
        raise ArRestError(f"Could not update {CONFIGURATION_SETTING_FORM} for {setting_name}: {detail}")

    async def _enrich_current_with_configuration_setting_metadata(self, *, current: dict, setting_name: str) -> dict:
        """Attach Configuration Setting metadata when the physical form has a match."""
        values = current.get("values") or {}
        current["values"] = values
        component_guid = _configuration_component_guid(values)
        try:
            setting_metadata = await self.get_configuration_setting_metadata(setting_name=setting_name, component_guid=component_guid or None)
        except ArRestError as exc:
            logger.debug("Could not enrich %s with %s metadata: %s", setting_name, CONFIGURATION_SETTING_FORM, exc)
            return current
        setting_guid = str(setting_metadata.get("setting_guid") or "").strip()
        if setting_guid and not _configuration_setting_guid(values):
            values["Configuration Setting GUID"] = setting_guid
        current["setting_metadata"] = setting_metadata
        return current


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

        Component Setting carries both Configuration Component GUID and
        Configuration Setting GUID.  Before writing, enrich the current row from
        AR System Configuration Setting so all log settings can use the matched
        Configuration Setting GUID when the physical setting row exists.
        """
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name=setting_name)
        values = current.get("values") or {}
        setting_guid = _configuration_setting_guid(values)
        setting_metadata = current.get("setting_metadata") or {}
        join_entry_id = str(current.get("entry_id") or "").strip()
        errors: list[str] = []
        candidate_entry_ids: list[str] = []

        def add_candidate(value: str | None) -> None:
            value = str(value or "").strip()
            if value and value not in candidate_entry_ids:
                candidate_entry_ids.append(value)

        add_candidate(setting_metadata.get("entry_id"))

        # AR REST join entry ids are often pipe-delimited. The secondary
        # physical row id is commonly the second/third segment. Try those too.
        if "|" in join_entry_id:
            parts = [part for part in join_entry_id.split("|") if part]
            for part in parts[1:]:
                add_candidate(part)

        if setting_guid:
            q = f"'179' = \"{_safe_ar_string(setting_guid)}\""
            try:
                entries = await self.query_entries(CONFIGURATION_SETTING_FORM, q=q, fields="", limit=3)
                for entry in entries:
                    add_candidate(_entry_id_from_links(entry))
            except ArRestError as exc:
                errors.append(f"query {CONFIGURATION_SETTING_FORM} by field id 179: {exc}")
            add_candidate(setting_guid)

        if setting_metadata:
            try:
                updated = await self._put_configuration_setting_value(
                    setting_name=setting_name,
                    setting_value=str(setting_value),
                    setting_metadata=setting_metadata,
                    verify_server_name=server_name,
                    form_name=form_name,
                )
                verified = updated.get("verified") or await self._verify_server_config_setting_value(
                    server_name=server_name,
                    setting_name=setting_name,
                    expected_value=str(setting_value),
                    form_name=form_name,
                )
                return {
                    "server_name": server_name,
                    "setting_name": setting_name,
                    "entry_id": updated.get("entry_id"),
                    "old_value": current.get("raw", current.get("value", "")),
                    "new_value": verified.get("raw", ""),
                    "status_code": updated.get("status_code", 0),
                    "method": updated.get("method") or f"PUT {CONFIGURATION_SETTING_FORM}",
                }
            except ArRestError as exc:
                errors.append(f"metadata update {CONFIGURATION_SETTING_FORM}: {exc}")

        value_field_candidates = _value_field_candidates((setting_metadata or {}).get("values") or {})
        for entry_id in list(candidate_entry_ids):
            try:
                entry = await self._get_entry(CONFIGURATION_SETTING_FORM, entry_id=entry_id)
                value_field_candidates = _value_field_candidates({**((setting_metadata or {}).get("values") or {}), **(entry.get("values") or {})})
            except ArRestError as exc:
                errors.append(f"read {CONFIGURATION_SETTING_FORM}/{entry_id}: {exc}")

        for entry_id in candidate_entry_ids:
            seen_fields: set[str] = set()
            for field_name in value_field_candidates:
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                try:
                    status = await self._put_entry_value(
                        form_name=CONFIGURATION_SETTING_FORM,
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
                        "method": f"PUT {CONFIGURATION_SETTING_FORM}.{field_name}",
                    }
                except ArRestError as exc:
                    errors.append(f"PUT {CONFIGURATION_SETTING_FORM}/{entry_id} field {field_name}: {exc}")

        detail = "; ".join(errors[-10:]) if errors else "no candidate secondary row found"
        raise ArRestError(f"Could not update underlying {CONFIGURATION_SETTING_FORM} row for {server_name} / {setting_name}: {detail}")

    async def _put_join_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, current: dict, form_name: str) -> dict:
        entry_id = current.get("entry_id")
        if not entry_id:
            raise ArRestError(f"Could not determine entry id for setting {setting_name!r} / server {server_name!r}.")
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name=setting_name)
        values = current.get("values") or {}
        payload_values = {
            "Setting Name": values.get("Setting Name", setting_name),
            "Component Type": values.get("Component Type", "com.bmc.arsys.server"),
            "Component Name": values.get("Component Name", server_name),
            "Setting Value": str(setting_value),
        }
        component_guid = _configuration_component_guid(values)
        setting_guid = _configuration_setting_guid(values)
        if component_guid:
            payload_values["Configuration Component GUID"] = component_guid
        if setting_guid:
            payload_values["Configuration Setting GUID"] = setting_guid
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
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name=setting_name)
        values = current.get("values") or {}
        component_type = _configuration_component_type(values) or "com.bmc.arsys.server"
        component_name = _configuration_component_name(values) or server_name
        component_guid = _configuration_component_guid(values)
        setting_guid = _configuration_setting_guid(values)
        safe_server = _safe_ar_string(component_name)
        safe_setting = _safe_ar_string(setting_name)
        qualification = "('Setting Name' = \"" + safe_setting + "\") AND ('Component Type' = \"" + _safe_ar_string(component_type) + "\") AND ('Component Name' = \"" + safe_server + "\")"
        form = quote(form_name, safe="")
        payload_values = {
            "Setting Name": setting_name,
            "Component Type": component_type,
            "Component Name": component_name,
            "Setting Value": str(setting_value),
        }
        if component_guid:
            payload_values["Configuration Component GUID"] = component_guid
        if setting_guid:
            payload_values["Configuration Setting GUID"] = setting_guid
        payload = {
            "values": payload_values,
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
            "method": "mergeEntry with component/setting GUID",
        }

    def _blocked_component_settings_qualification(self, setting_names: list[str] | tuple[str, ...] | set[str]) -> str:
        names = [str(name).strip() for name in setting_names if str(name).strip()]
        if not names:
            raise ValueError("At least one Setting Name is required when checking blocked component settings.")
        status_part = f"'Status' = \"{BLOCKED_COMPONENT_SETTING_STATUS}\""
        name_parts: list[str] = []
        for name in names:
            safe_name = _safe_ar_string(name)
            name_parts.append(f"'Setting Name' = \"{safe_name}\"")
        return f"({status_part}) AND (" + " OR ".join(name_parts) + ")"

    async def list_blocked_component_settings(self, *, setting_names: list[str] | tuple[str, ...] | set[str], limit: int = 10000) -> list[dict]:
        """Return blocked rows only for the requested Setting Name values.

        BMC can create rows in AR System Block Configuration Component Setting
        with Status = Blocked Setting. Only rows whose Setting Name matches
        the setting currently being saved are considered. This avoids
        changing unrelated blocked settings in the AR environment.
        """
        try:
            entries = await self.query_entries(
                BLOCKED_COMPONENT_SETTING_FORM,
                q=self._blocked_component_settings_qualification(setting_names),
                fields="values(Status,Setting Name)",
                limit=limit,
            )
        except ArRestError as exc:
            if _looks_like_missing_form_error(str(exc), BLOCKED_COMPONENT_SETTING_FORM):
                logger.info(
                    "%s is not present in this AR environment; skipping blocked component setting check.",
                    BLOCKED_COMPONENT_SETTING_FORM,
                )
                return []
            raise
        blocked: list[dict] = []
        for entry in entries:
            blocked.append({
                "entry_id": _entry_id_from_links(entry),
                "values": entry.get("values") or {},
            })
        return blocked

    async def allow_blocked_component_settings(self, *, setting_names: list[str] | tuple[str, ...] | set[str]) -> dict:
        """Unlock only blocked rows for the requested component settings.

        The operation is idempotent. If no rows are blocked for the supplied
        setting names, it returns a clean result without changing anything.
        """
        blocked = await self.list_blocked_component_settings(setting_names=setting_names)
        updated: list[dict] = []
        for row in blocked:
            entry_id = row.get("entry_id")
            if not entry_id:
                raise ArRestError(
                    f"{BLOCKED_COMPONENT_SETTING_FORM} contains a blocked row without a REST entry id."
                )
            status = await self._put_entry_value(
                form_name=BLOCKED_COMPONENT_SETTING_FORM,
                entry_id=entry_id,
                values={"Status": ALLOWED_COMPONENT_SETTING_STATUS},
            )
            values = row.get("values") or {}
            updated.append({
                "entry_id": entry_id,
                "setting_name": values.get("Setting Name") or "",
                "status_code": status,
            })
        remaining = await self.list_blocked_component_settings(setting_names=setting_names)
        if remaining:
            ids = ", ".join(str(row.get("entry_id") or "unknown") for row in remaining[:10])
            names = ", ".join(sorted({str(n).strip() for n in setting_names if str(n).strip()}))
            raise ArRestError(
                f"Could not unlock blocked component setting rows for {names}. Remaining blocked rows: {ids}"
            )
        return {"checked": True, "updated_count": len(updated), "updated": updated}

    async def verify_component_settings_unblocked(self, *, setting_names: list[str] | tuple[str, ...] | set[str]) -> dict:
        """Verify that requested Setting Name values are no longer blocked."""
        blocked = await self.list_blocked_component_settings(setting_names=setting_names)
        if blocked:
            ids = ", ".join(str(row.get("entry_id") or "unknown") for row in blocked[:10])
            names = ", ".join(sorted({str(n).strip() for n in setting_names if str(n).strip()}))
            raise ArRestError(
                f"Component settings are still blocked in {BLOCKED_COMPONENT_SETTING_FORM} for {names}: {ids}"
            )
        return {"checked": True, "blocked_count": 0}

    async def get_server_component_metadata(self, *, server_name: str, form_name: str = COMPONENT_SETTING_FORM) -> dict:
        """Return component metadata for a server/pod.

        Prefer AR System Configuration Component Setting because that join form
        exposes the exact fields we need in the supplied definitions:
        Component Name (3200), Component Type (3201), Configuration Setting GUID
        (3206) and Configuration Component GUID (3207).  Do not depend on the
        physical AR System Configuration Component field labels, because those
        labels vary between environments and can produce ARERR 1587.
        """
        safe_server = _safe_ar_string(server_name)
        errors: list[str] = []

        component_setting_queries = [
            f"('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"{safe_server}\")",
            f"'Component Name' = \"{safe_server}\"",
            f"'3200' = \"{safe_server}\"",
        ]
        entries, query_errors = await self._query_entries_best_effort(form_name, q_candidates=component_setting_queries, limit=25, fields="values(Component Name,Component Type,Setting Name,Configuration Component GUID,Configuration Setting GUID)")
        errors.extend(query_errors)
        if entries:
            def score_component_setting(entry: dict) -> int:
                values = entry.get("values") or {}
                result = 0
                if (_configuration_component_name(values) or "").lower() == server_name.lower():
                    result += 100
                if (_configuration_component_type(values) or "").lower() == "com.bmc.arsys.server":
                    result += 20
                if _configuration_component_guid(values):
                    result += 50
                if (_configuration_setting_name(values) or "").lower() == "debug-mode":
                    result += 5
                return result

            best = max(entries, key=score_component_setting)
            metadata = self._component_metadata_from_entry(best, server_name=server_name)
            if not metadata.get("component_guid") and metadata.get("entry_id"):
                try:
                    full_entry = await self._get_entry(form_name, entry_id=str(metadata.get("entry_id")))
                    metadata = self._component_metadata_from_entry(full_entry, server_name=server_name)
                    metadata["entry_id"] = metadata.get("entry_id") or _entry_id_from_links(best)
                except ArRestError as exc:
                    errors.append(f"read full {form_name} row: {exc}")
            if metadata.get("component_guid"):
                return metadata
            errors.append(f"{form_name} rows for {server_name} did not expose Configuration Component GUID")

        # Last-resort: use joins that expose the component GUID with field 179.
        # These forms may not contain a row for every setting, so they are only
        # fallback discovery paths for the component GUID.
        default_component_form = "AR System Default Configuration Setting Global - Components"
        default_queries = [
            f"('Component Type' = \"com.bmc.arsys.server\") AND ('Component Name' = \"{safe_server}\")",
            f"'Component Name' = \"{safe_server}\"",
            f"'3200' = \"{safe_server}\"",
        ]
        entries, query_errors = await self._query_entries_best_effort(default_component_form, q_candidates=default_queries, limit=25)
        errors.extend(query_errors)
        if entries:
            best = max(
                entries,
                key=lambda entry: (
                    100 if (_configuration_component_name(entry.get("values") or {}) or "").lower() == server_name.lower() else 0
                ) + (50 if _configuration_component_guid(entry.get("values") or {}) else 0),
            )
            metadata = self._component_metadata_from_entry(best, server_name=server_name)
            if metadata.get("component_guid"):
                return metadata

        # Avoid querying AR System Configuration Component with mutable field
        # labels first. If it is needed, field IDs are less likely to break.
        component_form_queries = [
            f"'3200' = \"{safe_server}\"",
        ]
        entries, query_errors = await self._query_entries_best_effort(COMPONENT_FORM, q_candidates=component_form_queries, limit=5)
        errors.extend(query_errors)
        if entries:
            best = max(entries, key=lambda entry: 50 if _configuration_component_guid(entry.get("values") or {}) else 0)
            metadata = self._component_metadata_from_entry(best, server_name=server_name)
            if metadata.get("component_guid"):
                return metadata

        detail = "; ".join(errors[-6:]) if errors else "no existing component rows returned"
        raise ArRestError(f"Could not find component metadata for server {server_name!r}: {detail}")

    async def _submit_component_setting_mapping_value(
        self,
        *,
        server_name: str,
        setting_name: str,
        setting_value: str,
        component_metadata: dict,
        form_name: str,
    ) -> dict:
        """Create the missing Component-Setting mapping row.

        AR System Configuration Component Setting is a join that is driven by
        the Component-Setting Mapping row plus AR System Configuration Setting.
        Therefore, when a setting exists in AR System Configuration Setting but
        not in Component Setting, create the mapping with field 3207
        (Configuration Component GUID) and 3206 (Configuration Setting GUID).
        """
        values = component_metadata.get("values") or {}
        component_guid = str(component_metadata.get("component_guid") or _configuration_component_guid(values)).strip()
        component_name = str(component_metadata.get("component_name") or _configuration_component_name(values) or server_name).strip() or server_name
        if not component_guid:
            raise ArRestError(f"Could not map {setting_name} for {server_name}: missing Configuration Component GUID.")

        setting_metadata = await self.get_configuration_setting_metadata(setting_name=setting_name)
        setting_guid = str(setting_metadata.get("setting_guid") or "").strip()
        if not setting_guid:
            raise ArRestError(f"Could not map {setting_name} for {server_name}: missing Configuration Setting GUID from {CONFIGURATION_SETTING_FORM}.")

        try:
            physical_update = await self._put_configuration_setting_value(
                setting_name=setting_name,
                setting_value=str(setting_value),
                setting_metadata=setting_metadata,
                verify_server_name=None,
                form_name=form_name,
            )
        except ArRestError as exc:
            logger.debug("Could not pre-update %s before mapping %s/%s: %s", CONFIGURATION_SETTING_FORM, server_name, setting_name, exc)
            physical_update = None

        safe_component_guid = _safe_ar_string(component_guid)
        safe_setting_guid = _safe_ar_string(setting_guid)
        qualification = f"('Configuration Component GUID' = \"{safe_component_guid}\") AND ('Configuration Setting GUID' = \"{safe_setting_guid}\")"
        payload_values = {
            "Short Description": f"{component_name}:{setting_name}",
            "Configuration Component GUID": component_guid,
            "Configuration Setting GUID": setting_guid,
        }
        form = quote(COMPONENT_SETTING_MAPPING_FORM, safe="")
        payload = {
            "values": payload_values,
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
        errors: list[str] = []
        try:
            response = await self.client.post(f"/api/arsys/v1/mergeEntry/{form}", json=payload, headers=self._headers())
            if response.status_code >= 400:
                raise ArRestError(f"mergeEntry {COMPONENT_SETTING_MAPPING_FORM} failed: HTTP {response.status_code}: {response.text[:1200]}")
            status_code = response.status_code
        except (ArRestError, httpx.RequestError) as exc:
            if isinstance(exc, httpx.RequestError):
                raise _friendly_request_error(exc, self.settings.base_url) from exc
            errors.append(str(exc))
            try:
                submitted = await self._submit_entry_values(
                    form_name=COMPONENT_SETTING_MAPPING_FORM,
                    values=payload_values,
                )
                status_code = int(submitted.get("status_code") or 0)
            except ArRestError as submit_exc:
                errors.append(str(submit_exc))
                try:
                    submitted = await self._submit_entry_values(
                        form_name=COMPONENT_SETTING_MAPPING_FORM,
                        values={8: f"{component_name}:{setting_name}", 3207: component_guid, 3206: setting_guid},
                    )
                    status_code = int(submitted.get("status_code") or 0)
                except ArRestError as id_submit_exc:
                    errors.append(str(id_submit_exc))
                    raise ArRestError(
                        f"Could not create mapping for {setting_name} / {server_name} in {COMPONENT_SETTING_MAPPING_FORM}: "
                        + "; ".join(errors[-4:])
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
            "entry_id": verified.get("entry_id"),
            "old_value": "",
            "new_value": verified.get("raw", ""),
            "status_code": status_code,
            "method": f"merge/submit {COMPONENT_SETTING_MAPPING_FORM} with component/setting GUID",
            "configuration_setting_update": physical_update,
        }


    async def _merge_missing_server_config_setting_value(
        self,
        *,
        server_name: str,
        setting_name: str,
        setting_value: str,
        component_metadata: dict,
        form_name: str,
    ) -> dict:
        """Create/update a missing Component Setting row using both GUIDs.

        Component Setting is linked by Configuration Component GUID from
        AR System Configuration Component and Configuration Setting GUID from
        AR System Configuration Setting.  When the setting exists in the
        physical setting form, update that value first and include the matched
        Configuration Setting GUID in the Component Setting merge.
        """
        values = component_metadata.get("values") or {}
        component_guid = str(component_metadata.get("component_guid") or _configuration_component_guid(values)).strip()
        component_type = str(component_metadata.get("component_type") or _configuration_component_type(values) or "com.bmc.arsys.server").strip() or "com.bmc.arsys.server"
        component_name = str(component_metadata.get("component_name") or _configuration_component_name(values) or server_name).strip() or server_name
        setting_metadata: dict | None = None
        setting_guid = ""
        physical_update: dict | None = None
        try:
            setting_metadata = await self.get_configuration_setting_metadata(setting_name=setting_name, component_guid=component_guid or None)
            setting_guid = str(setting_metadata.get("setting_guid") or "").strip()
            physical_update = await self._put_configuration_setting_value(
                setting_name=setting_name,
                setting_value=str(setting_value),
                setting_metadata=setting_metadata,
                verify_server_name=None,
                form_name=form_name,
            )
        except ArRestError as exc:
            logger.debug("Could not pre-update %s for missing %s/%s: %s", CONFIGURATION_SETTING_FORM, server_name, setting_name, exc)

        safe_server = _safe_ar_string(component_name)
        safe_setting = _safe_ar_string(setting_name)
        qualification = "('Setting Name' = \"" + safe_setting + "\") AND ('Component Type' = \"" + _safe_ar_string(component_type) + "\") AND ('Component Name' = \"" + safe_server + "\")"
        payload_values = {
            "Setting Name": setting_name,
            "Component Type": component_type,
            "Component Name": component_name,
            "Setting Value": str(setting_value),
        }
        if component_guid:
            payload_values["Configuration Component GUID"] = component_guid
        if setting_guid:
            payload_values["Configuration Setting GUID"] = setting_guid
        form = quote(form_name, safe="")
        payload = {
            "values": payload_values,
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
            raise ArRestError(f"mergeEntry create {setting_name} failed for {server_name}: HTTP {response.status_code}: {response.text[:1200]}")
        verified = await self._verify_server_config_setting_value(
            server_name=server_name,
            setting_name=setting_name,
            expected_value=str(setting_value),
            form_name=form_name,
        )
        return {
            "server_name": server_name,
            "setting_name": setting_name,
            "entry_id": verified.get("entry_id"),
            "old_value": "",
            "new_value": verified.get("raw", ""),
            "status_code": response.status_code,
            "method": "mergeEntry upsert with component GUID and configuration setting GUID" if setting_guid else ("mergeEntry upsert with component GUID" if component_guid else "mergeEntry upsert"),
            "configuration_setting_update": physical_update,
        }


    async def _submit_missing_component_setting_value(
        self,
        *,
        server_name: str,
        setting_name: str,
        setting_value: str,
        component_metadata: dict,
        form_name: str,
    ) -> dict:
        """Fallback submit directly to Component Setting with both GUIDs."""
        values = component_metadata.get("values") or {}
        component_guid = str(component_metadata.get("component_guid") or _configuration_component_guid(values)).strip()
        component_type = str(component_metadata.get("component_type") or _configuration_component_type(values) or "com.bmc.arsys.server").strip() or "com.bmc.arsys.server"
        component_name = str(component_metadata.get("component_name") or _configuration_component_name(values) or server_name).strip() or server_name
        if not component_guid:
            raise ArRestError(f"Could not create {setting_name} for {server_name}: missing Configuration Component GUID.")
        setting_metadata = await self.get_configuration_setting_metadata(setting_name=setting_name, component_guid=component_guid or None)
        setting_guid = str(setting_metadata.get("setting_guid") or "").strip()
        if not setting_guid:
            raise ArRestError(f"Could not create {setting_name} for {server_name}: missing Configuration Setting GUID from {CONFIGURATION_SETTING_FORM}.")
        try:
            physical_update = await self._put_configuration_setting_value(
                setting_name=setting_name,
                setting_value=str(setting_value),
                setting_metadata=setting_metadata,
                verify_server_name=None,
                form_name=form_name,
            )
        except ArRestError as exc:
            logger.debug("Could not pre-update %s before Component Setting submit for %s/%s: %s", CONFIGURATION_SETTING_FORM, server_name, setting_name, exc)
            physical_update = None
        submitted = await self._submit_entry_values(
            form_name=form_name,
            values={
                "Configuration Component GUID": component_guid,
                "Configuration Setting GUID": setting_guid,
                "Component Type": component_type,
                "Component Name": component_name,
                "Setting Name": setting_name,
                "Setting Value": str(setting_value),
            },
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
            "entry_id": verified.get("entry_id") or submitted.get("entry_id"),
            "old_value": "",
            "new_value": verified.get("raw", ""),
            "status_code": submitted.get("status_code", 0),
            "method": f"POST {form_name} with component/setting GUID",
            "configuration_setting_update": physical_update,
        }


    async def _submit_missing_physical_server_config_setting_value(
        self,
        *,
        server_name: str,
        setting_name: str,
        setting_value: str,
        component_metadata: dict,
        form_name: str,
    ) -> dict:
        """Last-resort creation on AR System Configuration Setting.

        The physical setting form has Name (3204), Value (3205) and
        Configuration Setting GUID (179).  It does not have Configuration
        Component GUID.  After creating the setting row, create the
        Component-Setting Mapping row to make it visible in Component Setting.
        """
        values = component_metadata.get("values") or {}
        component_guid = str(component_metadata.get("component_guid") or _configuration_component_guid(values)).strip()
        if not component_guid:
            raise ArRestError(f"Could not create {setting_name} for {server_name}: missing Configuration Component GUID.")

        created_setting: dict | None = None
        existing_setting: dict | None = None
        try:
            existing_setting = await self.get_configuration_setting_metadata(setting_name=setting_name)
        except ArRestError:
            existing_setting = None

        if not existing_setting:
            payload_candidates: list[tuple[str, dict[int | str, str]]] = [
                ("field names Name/Value", {"Short Description": setting_name, "Name": setting_name, "Value": str(setting_value)}),
                ("field ids 8/3204/3205", {8: setting_name, 3204: setting_name, 3205: str(setting_value)}),
            ]
            errors: list[str] = []
            for label, payload_values in payload_candidates:
                try:
                    created_setting = await self._submit_entry_values(form_name=CONFIGURATION_SETTING_FORM, values=payload_values)
                    break
                except ArRestError as exc:
                    errors.append(f"{label}: {exc}")
            if not created_setting:
                raise ArRestError(
                    f"Could not create {setting_name} in {CONFIGURATION_SETTING_FORM}: "
                    + "; ".join(errors[-4:])
                )

        mapped = await self._submit_component_setting_mapping_value(
            server_name=server_name,
            setting_name=setting_name,
            setting_value=str(setting_value),
            component_metadata=component_metadata,
            form_name=form_name,
        )
        mapped["method"] = f"POST {CONFIGURATION_SETTING_FORM} then {mapped.get('method')}" if created_setting else mapped.get("method")
        mapped["configuration_setting_create"] = created_setting
        return mapped

    async def set_server_config_setting_value(self, *, server_name: str, setting_name: str, setting_value: str, form_name: str = "AR System Configuration Component Setting") -> dict:
        if form_name == "AR System Configuration Component Setting":
            await self.allow_blocked_component_settings(setting_names=[setting_name])
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
        """Set a server config setting, creating it when missing.

        Existing rows are updated through the normal writer. If the row is
        absent, create it with server component metadata so optional settings
        such as Restrict-Log-Users can be enabled in environments where they do
        not already exist.
        """
        try:
            return await self.set_server_config_setting_value(server_name=server_name, setting_name=setting_name, setting_value=setting_value, form_name=form_name)
        except ArRestError as exc:
            if not _looks_like_missing_entry_error(str(exc)):
                raise
            missing_error = exc

        if form_name == "AR System Configuration Component Setting":
            await self.allow_blocked_component_settings(setting_names=[setting_name])

        component_metadata = await self.get_server_component_metadata(server_name=server_name, form_name=form_name)
        attempts: list[str] = [f"initial update: {missing_error}"]
        for creator in (self._submit_component_setting_mapping_value, self._merge_missing_server_config_setting_value, self._submit_missing_component_setting_value, self._submit_missing_physical_server_config_setting_value):
            try:
                return await creator(
                    server_name=server_name,
                    setting_name=setting_name,
                    setting_value=str(setting_value),
                    component_metadata=component_metadata,
                    form_name=form_name,
                )
            except ArRestError as exc:
                attempts.append(f"{creator.__name__}: {exc}")
                logger.warning("%s create attempt failed for %s using %s: %s", setting_name, server_name, creator.__name__, exc)
        raise ArRestError(f"Create {setting_name} failed for {server_name}. " + " | ".join(attempts[-4:]))

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
        if form_name == "AR System Configuration Component Setting":
            await self.allow_blocked_component_settings(setting_names=[setting_name])
        try:
            status = await self._delete_entry(form_name=form_name, entry_id=entry_id)
        except ArRestError as exc:
            if _looks_like_missing_entry_error(str(exc)):
                return {"server_name": server_name, "setting_name": setting_name, "deleted": False, "status_code": 0, "message": "already absent"}
            raise
        return {"server_name": server_name, "setting_name": setting_name, "deleted": True, "status_code": status}

    async def _put_secondary_server_debug_mode(self, *, server_name: str, debug_mode: int, current: dict, form_name: str) -> dict:
        """Update Debug-mode in AR System Configuration Setting.

        The current Component Setting row is enriched from the physical setting
        form first, so Debug-mode also uses the matched Configuration Setting
        GUID when one exists in AR System Configuration Setting.
        """
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name="Debug-mode")
        values = current.get("values") or {}
        setting_guid = _configuration_setting_guid(values)
        setting_metadata = current.get("setting_metadata") or {}
        join_entry_id = str(current.get("entry_id") or "").strip()
        errors: list[str] = []
        candidate_entry_ids: list[str] = []

        def add_candidate(value: str | None) -> None:
            value = str(value or "").strip()
            if value and value not in candidate_entry_ids:
                candidate_entry_ids.append(value)

        add_candidate(setting_metadata.get("entry_id"))
        if "|" in join_entry_id:
            parts = [part for part in join_entry_id.split("|") if part]
            for part in parts[1:]:
                add_candidate(part)
        if setting_guid:
            q = f"'179' = \"{_safe_ar_string(setting_guid)}\""
            try:
                entries = await self.query_entries(CONFIGURATION_SETTING_FORM, q=q, fields="", limit=3)
                for entry in entries:
                    add_candidate(_entry_id_from_links(entry))
            except ArRestError as exc:
                errors.append(f"query {CONFIGURATION_SETTING_FORM} by field id 179: {exc}")
            add_candidate(setting_guid)

        if setting_metadata:
            try:
                updated = await self._put_configuration_setting_value(
                    setting_name="Debug-mode",
                    setting_value=str(int(debug_mode)),
                    setting_metadata=setting_metadata,
                    verify_server_name=server_name,
                    form_name=form_name,
                )
                verified = await self._verify_server_debug_mode(server_name=server_name, expected_value=debug_mode, form_name=form_name)
                return {
                    "server_name": server_name,
                    "entry_id": updated.get("entry_id"),
                    "old_value": current["value"],
                    "new_value": verified["value"],
                    "status_code": updated.get("status_code", 0),
                    "method": updated.get("method") or f"PUT {CONFIGURATION_SETTING_FORM}",
                }
            except ArRestError as exc:
                errors.append(f"metadata update {CONFIGURATION_SETTING_FORM}: {exc}")

        value_field_candidates = _value_field_candidates((setting_metadata or {}).get("values") or {})
        for entry_id in list(candidate_entry_ids):
            try:
                entry = await self._get_entry(CONFIGURATION_SETTING_FORM, entry_id=entry_id)
                value_field_candidates = _value_field_candidates({**((setting_metadata or {}).get("values") or {}), **(entry.get("values") or {})})
            except ArRestError as exc:
                errors.append(f"read {CONFIGURATION_SETTING_FORM}/{entry_id}: {exc}")

        for entry_id in candidate_entry_ids:
            seen_fields: set[str] = set()
            for field_name in value_field_candidates:
                if field_name in seen_fields:
                    continue
                seen_fields.add(field_name)
                try:
                    status = await self._put_entry_value(
                        form_name=CONFIGURATION_SETTING_FORM,
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
                        "method": f"PUT {CONFIGURATION_SETTING_FORM}.{field_name}",
                    }
                except ArRestError as exc:
                    errors.append(f"PUT {CONFIGURATION_SETTING_FORM}/{entry_id} field {field_name}: {exc}")

            try:
                status = await self._put_entry_value_field_ids(
                    form_name=CONFIGURATION_SETTING_FORM,
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
                    "method": f"PUT {CONFIGURATION_SETTING_FORM} field 3205",
                }
            except ArRestError as exc:
                errors.append(f"PUT {CONFIGURATION_SETTING_FORM}/{entry_id} field 3205: {exc}")

        detail = "; ".join(errors[-10:]) if errors else "no candidate secondary row found"
        raise ArRestError(f"Could not update underlying {CONFIGURATION_SETTING_FORM} row for {server_name}: {detail}")

    async def _put_join_server_debug_mode(self, *, server_name: str, debug_mode: int, current: dict, form_name: str) -> dict:
        """Try a normal AR REST PUT update for the join-form Debug-mode row."""
        entry_id = current.get("entry_id")
        if not entry_id:
            raise ArRestError(f"Could not determine entry id for Debug-mode setting row for {server_name!r}.")
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name="Debug-mode")
        values = current.get("values") or {}
        payload_values = {
            "Setting Name": values.get("Setting Name", "Debug-mode"),
            "Component Type": values.get("Component Type", "com.bmc.arsys.server"),
            "Component Name": values.get("Component Name", server_name),
            "Setting Value": str(int(debug_mode)),
        }
        component_guid = _configuration_component_guid(values)
        setting_guid = _configuration_setting_guid(values)
        if component_guid:
            payload_values["Configuration Component GUID"] = component_guid
        if setting_guid:
            payload_values["Configuration Setting GUID"] = setting_guid
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
        current = await self._enrich_current_with_configuration_setting_metadata(current=current, setting_name="Debug-mode")
        values = current.get("values") or {}
        component_type = _configuration_component_type(values) or "com.bmc.arsys.server"
        component_name = _configuration_component_name(values) or server_name
        component_guid = _configuration_component_guid(values)
        setting_guid = _configuration_setting_guid(values)
        safe_server = _safe_ar_string(component_name)
        qualification = "('Setting Name' = \"Debug-mode\") AND ('Component Type' = \"" + _safe_ar_string(component_type) + "\") AND ('Component Name' = \"" + safe_server + "\")"
        form = quote(form_name, safe="")
        payload_values = {
            "Setting Name": "Debug-mode",
            "Component Type": component_type,
            "Component Name": component_name,
            "Setting Value": str(int(debug_mode)),
        }
        if component_guid:
            payload_values["Configuration Component GUID"] = component_guid
        if setting_guid:
            payload_values["Configuration Setting GUID"] = setting_guid
        payload = {
            "values": payload_values,
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
            "method": "mergeEntry with component/setting GUID",
        }

    async def set_server_debug_mode(self, *, server_name: str, debug_mode: int, form_name: str = "AR System Configuration Component Setting") -> dict:
        """Update Setting Value for the server Debug-mode bitmask row.

        This reads from the Component Setting join form but writes the underlying
        AR System Configuration Setting row first, because Setting Value is
        mapped from the secondary form in the join definition. Every attempted
        write is verified by reading the join form again before reporting success.
        """
        if form_name == "AR System Configuration Component Setting":
            await self.allow_blocked_component_settings(setting_names=["Debug-mode"])
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
