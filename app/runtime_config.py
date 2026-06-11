from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock

from .settings import AppConfig, PodConfig, LogTypeConfig


class RuntimeConfig:
    def __init__(self, config: AppConfig):
        self._config = config
        self._lock = RLock()
        self._discovery = {
            "last_refresh_at": None,
            "last_success_at": None,
            "last_error": None,
            "source": "static config",
            "pod_count": len(config.pods),
            "log_count": len(config.log_types),
        }

    def get(self) -> AppConfig:
        with self._lock:
            return deepcopy(self._config)

    def discovery_status(self) -> dict:
        with self._lock:
            return deepcopy(self._discovery)

    def mark_discovery_error(self, error: str) -> None:
        with self._lock:
            self._discovery["last_refresh_at"] = datetime.now(timezone.utc).isoformat()
            self._discovery["last_error"] = error

    def replace_discovered(self, pods: list[PodConfig], log_types: list[LogTypeConfig], *, source: str = "AR REST discovery") -> None:
        with self._lock:
            self._config.pods = pods
            self._config.log_types = log_types
            now = datetime.now(timezone.utc).isoformat()
            self._discovery.update({
                "last_refresh_at": now,
                "last_success_at": now,
                "last_error": None,
                "source": source,
                "pod_count": len(pods),
                "log_count": len(log_types),
            })

    def add_pod(self, pod: PodConfig) -> None:
        with self._lock:
            self._config.pods = [p for p in self._config.pods if p.id != pod.id]
            self._config.pods.append(pod)

    def add_log_type(self, log_type: LogTypeConfig) -> None:
        with self._lock:
            self._config.log_types = [l for l in self._config.log_types if l.id != log_type.id]
            self._config.log_types.append(log_type)

    @staticmethod
    def is_log_available_on_pod(log_type: LogTypeConfig, pod: PodConfig) -> bool:
        if log_type.available_on_pods and pod.id in log_type.available_on_pods:
            return True
        if log_type.available_on_tags and set(log_type.available_on_tags).intersection(pod.tags):
            return True
        return not log_type.available_on_pods and not log_type.available_on_tags
