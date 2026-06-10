from __future__ import annotations

from copy import deepcopy
from threading import RLock

from .settings import AppConfig, PodConfig, LogTypeConfig


class RuntimeConfig:
    def __init__(self, config: AppConfig):
        self._config = config
        self._lock = RLock()

    def get(self) -> AppConfig:
        with self._lock:
            return deepcopy(self._config)

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
