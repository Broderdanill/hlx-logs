from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import os
from typing import Any

import yaml


@dataclass
class ArSettings:
    base_url: str
    form_name: str = "HLX:Logs"
    attachment_field: str = "1EX"
    verify_tls: bool = False
    request_timeout_seconds: int = 60
    poll_interval_seconds: int = 2
    poll_timeout_seconds: int = 60
    result_query_template: str = "'TransactionId' = \"{transaction_id}\""


@dataclass
class DiscoverySettings:
    enabled: bool = True
    refresh_interval_seconds: int = 300
    pod_form_name: str = "AR System Configuration Component Setting"
    pod_query: str = "'Setting Name' = \"Configuration-Name\""
    pod_value_field: str = "Setting Value"
    log_form_name: str = "AR System Server Group Logs"
    log_server_field: str = "Server Name"
    log_filename_field: str = "fileName"
    log_size_field: str = "File Size"
    default_directory: str = "/opt/bmc/ARSystem/db"
    include_zero_byte_logs: bool = True


@dataclass
class SecuritySettings:
    require_admin_group: bool = True
    user_form: str = "User"
    login_field: str = "Login Name"
    group_list_field: str = "Group List"
    admin_group_id: str = "1"


@dataclass
class StorageSettings:
    data_dir: str = "/data"
    retention_days: int = 5


@dataclass
class PodConfig:
    id: str
    label: str
    enabled: bool = True
    tags: list[str] = field(default_factory=list)


@dataclass
class LogTypeConfig:
    id: str
    label: str
    filename: str
    directory: str
    available_on_tags: list[str] = field(default_factory=list)
    available_on_pods: list[str] = field(default_factory=list)
    enabled: bool = True
    parser: str = "generic"
    category: str = "Discovered"
    description: str = ""
    severity: str = "info"
    tags: list[str] = field(default_factory=list)
    file_sizes_by_pod: dict[str, str] = field(default_factory=dict)


@dataclass
class AppConfig:
    ar: ArSettings
    pods: list[PodConfig]
    log_types: list[LogTypeConfig]
    storage: StorageSettings = field(default_factory=StorageSettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    discovery: DiscoverySettings = field(default_factory=DiscoverySettings)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    return int(value)


def load_config(path: str | Path) -> AppConfig:
    data: dict[str, Any] = {}
    cfg_path = Path(path)
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    ar_data = data.get("ar", {})
    ar = ArSettings(
        base_url=os.getenv("AR_BASE_URL", ar_data.get("base_url", "http://ars-arserver:8008")).rstrip("/"),
        form_name=os.getenv("AR_FORM_NAME", ar_data.get("form_name", "HLX:Logs")),
        attachment_field=os.getenv("AR_ATTACHMENT_FIELD", ar_data.get("attachment_field", "1EX")),
        verify_tls=_env_bool("AR_VERIFY_TLS", bool(ar_data.get("verify_tls", False))),
        request_timeout_seconds=_env_int("AR_REQUEST_TIMEOUT_SECONDS", ar_data.get("request_timeout_seconds", 60)),
        poll_interval_seconds=_env_int("AR_POLL_INTERVAL_SECONDS", ar_data.get("poll_interval_seconds", 2)),
        poll_timeout_seconds=_env_int("AR_POLL_TIMEOUT_SECONDS", ar_data.get("poll_timeout_seconds", 60)),
        result_query_template=os.getenv("AR_RESULT_QUERY_TEMPLATE", ar_data.get("result_query_template", "'TransactionId' = \"{transaction_id}\"")),
    )

    storage_data = data.get("storage", {})
    storage = StorageSettings(
        data_dir=os.getenv("DATA_DIR", storage_data.get("data_dir", "/data")),
        retention_days=_env_int("RETENTION_DAYS", storage_data.get("retention_days", 5)),
    )

    security_data = data.get("security", {})
    security = SecuritySettings(
        require_admin_group=_env_bool("REQUIRE_ADMIN_GROUP", bool(security_data.get("require_admin_group", True))),
        user_form=os.getenv("AR_USER_FORM", security_data.get("user_form", "User")),
        login_field=os.getenv("AR_LOGIN_FIELD", security_data.get("login_field", "Login Name")),
        group_list_field=os.getenv("AR_GROUP_LIST_FIELD", security_data.get("group_list_field", "Group List")),
        admin_group_id=os.getenv("AR_ADMIN_GROUP_ID", str(security_data.get("admin_group_id", "1"))),
    )

    discovery_data = data.get("discovery", {})
    discovery = DiscoverySettings(
        enabled=_env_bool("DISCOVERY_ENABLED", bool(discovery_data.get("enabled", True))),
        refresh_interval_seconds=_env_int("DISCOVERY_REFRESH_INTERVAL_SECONDS", discovery_data.get("refresh_interval_seconds", 300)),
        pod_form_name=os.getenv("DISCOVERY_POD_FORM_NAME", discovery_data.get("pod_form_name", "AR System Configuration Component Setting")),
        pod_query=os.getenv("DISCOVERY_POD_QUERY", discovery_data.get("pod_query", "'Setting Name' = \"Configuration-Name\"")),
        pod_value_field=os.getenv("DISCOVERY_POD_VALUE_FIELD", discovery_data.get("pod_value_field", "Setting Value")),
        log_form_name=os.getenv("DISCOVERY_LOG_FORM_NAME", discovery_data.get("log_form_name", "AR System Server Group Logs")),
        log_server_field=os.getenv("DISCOVERY_LOG_SERVER_FIELD", discovery_data.get("log_server_field", "Server Name")),
        log_filename_field=os.getenv("DISCOVERY_LOG_FILENAME_FIELD", discovery_data.get("log_filename_field", "fileName")),
        log_size_field=os.getenv("DISCOVERY_LOG_SIZE_FIELD", discovery_data.get("log_size_field", "File Size")),
        default_directory=os.getenv("DISCOVERY_DEFAULT_DIRECTORY", discovery_data.get("default_directory", "/opt/bmc/ARSystem/db")),
        include_zero_byte_logs=_env_bool("DISCOVERY_INCLUDE_ZERO_BYTE_LOGS", bool(discovery_data.get("include_zero_byte_logs", True))),
    )

    pods = [PodConfig(**item) for item in data.get("pods", [])]
    log_types = [LogTypeConfig(**item) for item in data.get("log_types", [])]
    return AppConfig(ar=ar, pods=pods, log_types=log_types, storage=storage, security=security, discovery=discovery)
