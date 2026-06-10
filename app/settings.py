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
    attachment_field: str = "ZipFile"
    verify_tls: bool = False
    request_timeout_seconds: int = 60
    poll_interval_seconds: int = 2
    poll_timeout_seconds: int = 60
    result_query_template: str = "'TransactionId' = \"{transaction_id}\""


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


@dataclass
class AppConfig:
    ar: ArSettings
    pods: list[PodConfig]
    log_types: list[LogTypeConfig]


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def load_config(path: str | Path) -> AppConfig:
    data: dict[str, Any] = {}
    cfg_path = Path(path)
    if cfg_path.exists():
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}

    ar_data = data.get("ar", {})
    ar = ArSettings(
        base_url=os.getenv("AR_BASE_URL", ar_data.get("base_url", "http://platform-user-ext:8008")).rstrip("/"),
        form_name=os.getenv("AR_FORM_NAME", ar_data.get("form_name", "HLX:Logs")),
        attachment_field=os.getenv("AR_ATTACHMENT_FIELD", ar_data.get("attachment_field", "ZipFile")),
        verify_tls=_env_bool("AR_VERIFY_TLS", bool(ar_data.get("verify_tls", False))),
        request_timeout_seconds=int(os.getenv("AR_REQUEST_TIMEOUT_SECONDS", ar_data.get("request_timeout_seconds", 60))),
        poll_interval_seconds=int(os.getenv("AR_POLL_INTERVAL_SECONDS", ar_data.get("poll_interval_seconds", 2))),
        poll_timeout_seconds=int(os.getenv("AR_POLL_TIMEOUT_SECONDS", ar_data.get("poll_timeout_seconds", 60))),
        result_query_template=os.getenv("AR_RESULT_QUERY_TEMPLATE", ar_data.get("result_query_template", "'TransactionId' = \"{transaction_id}\"")),
    )

    pods = [PodConfig(**item) for item in data.get("pods", [])]
    log_types = [LogTypeConfig(**item) for item in data.get("log_types", [])]
    return AppConfig(ar=ar, pods=pods, log_types=log_types)
