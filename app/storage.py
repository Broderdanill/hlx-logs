from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import shutil
from typing import Iterable

from .parser import LogLine
from .settings import StorageSettings

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class CollectionStore:
    def __init__(self, settings: StorageSettings):
        self.root = Path(settings.data_dir)
        self.collections_dir = self.root / "collections"
        self.retention_days = settings.retention_days
        self.collections_dir.mkdir(parents=True, exist_ok=True)

    def cleanup(self) -> int:
        cutoff = utc_now() - timedelta(days=self.retention_days)
        removed = 0
        for path in self.collections_dir.iterdir() if self.collections_dir.exists() else []:
            if not path.is_dir():
                continue
            meta = self._read_json(path / "meta.json") or {}
            created_raw = meta.get("created_at")
            try:
                created = datetime.fromisoformat(created_raw) if created_raw else datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            except Exception:
                created = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            if created < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        if removed:
            logger.info("Removed %s expired collections older than %s days", removed, self.retention_days)
        return removed

    def path_for(self, transaction_id: str) -> Path:
        safe = "".join(c for c in transaction_id if c.isalnum() or c in "-_")
        return self.collections_dir / safe

    def save_collection(self, transaction_id: str, requests: list[dict], rows: list[LogLine], downloads: dict[str, bytes]) -> None:
        target = self.path_for(transaction_id)
        downloads_dir = target / "downloads"
        target.mkdir(parents=True, exist_ok=True)
        downloads_dir.mkdir(parents=True, exist_ok=True)
        download_meta = []
        for name, blob in downloads.items():
            safe_name = name.replace("/", "_").replace("\\", "_")
            (downloads_dir / safe_name).write_bytes(blob)
            download_meta.append({"name": safe_name, "bytes": len(blob)})
        with (target / "rows.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row.to_json() + "\n")
        counts = self._summarize_rows(rows)
        meta = {
            "transaction_id": transaction_id,
            "created_at": utc_now().isoformat(),
            "requests": requests,
            "downloads": download_meta,
            "row_count": len(rows),
            "summary": counts,
        }
        (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_collections(self, limit: int = 50) -> list[dict]:
        items = []
        for path in self.collections_dir.iterdir() if self.collections_dir.exists() else []:
            if not path.is_dir():
                continue
            meta = self._read_json(path / "meta.json")
            if meta:
                items.append(meta)
        items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return items[:limit]

    def load_collection(self, transaction_id: str) -> dict | None:
        target = self.path_for(transaction_id)
        meta = self._read_json(target / "meta.json")
        if not meta:
            return None
        rows = []
        rows_path = target / "rows.jsonl"
        if rows_path.exists():
            with rows_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(LogLine.from_dict(json.loads(line)))
        downloads = {d["name"]: target / "downloads" / d["name"] for d in meta.get("downloads", [])}
        return {"meta": meta, "rows": rows, "downloads": downloads}

    def download_path(self, transaction_id: str, name: str) -> Path | None:
        path = self.path_for(transaction_id) / "downloads" / name
        if path.exists() and path.is_file():
            return path
        return None

    @staticmethod
    def _read_json(path: Path) -> dict | None:
        try:
            if not path.exists():
                return None
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Could not read %s", path, exc_info=True)
            return None

    @staticmethod
    def _summarize_rows(rows: Iterable[LogLine]) -> dict:
        levels = Counter()
        files = Counter()
        pods = Counter()
        transactions = Counter()
        for row in rows:
            if row.level:
                levels[row.level] += 1
            files[row.filename] += 1
            pods[row.pod] += 1
            if row.transaction:
                transactions[row.transaction] += 1
        return {
            "levels": dict(levels),
            "files": dict(files),
            "pods": dict(pods),
            "transactions": len(transactions),
            "top_transactions": transactions.most_common(25),
        }
