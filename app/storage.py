from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
import logging
from pathlib import Path
import re
import shutil
from typing import Iterable
import zipfile

from .parser import LogLine
from .settings import StorageSettings

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def safe_name(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or "item"


class CollectionStore:
    def __init__(self, settings: StorageSettings):
        self.settings = settings
        self.root = Path(settings.data_dir)
        self.collections_dir = self.root / "collections"
        self.collections_dir.mkdir(parents=True, exist_ok=True)
        self._cache: dict[str, tuple[float, dict]] = {}

    def cleanup(self) -> None:
        cutoff = utc_now() - timedelta(days=self.settings.retention_days)
        for path in self._iter_collection_dirs():
            meta = self._read_json(path / "meta.json")
            if not meta:
                continue
            try:
                created = datetime.fromisoformat(meta.get("created_at", "").replace("Z", "+00:00"))
            except Exception:
                continue
            if created < cutoff:
                logger.info("Removing expired collection %s", path)
                shutil.rmtree(path, ignore_errors=True)
                self._cache.pop(str(path), None)

    def _iter_collection_dirs(self):
        if not self.collections_dir.exists():
            return []
        return [p for p in self.collections_dir.iterdir() if p.is_dir()]

    def path_for(self, transaction_id: str) -> Path:
        return self.collections_dir / safe_name(transaction_id)

    def save_collection(self, transaction_id: str, owner: str, requests: list[dict], rows: list[LogLine], downloads: dict[str, bytes], failures: list[dict] | None = None) -> None:
        target = self.path_for(transaction_id)
        target.mkdir(parents=True, exist_ok=True)
        downloads_dir = target / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        download_meta = []
        for name, blob in downloads.items():
            safe = safe_name(name)
            path = downloads_dir / safe
            path.write_bytes(blob)
            download_meta.append({"name": safe, "bytes": len(blob)})
        with (target / "rows.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row.to_json() + "\n")
        counts = self._summarize_rows(rows)
        meta = {
            "transaction_id": transaction_id,
            "owner": owner,
            "created_at": utc_now().isoformat(),
            "requests": requests,
            "downloads": download_meta,
            "failures": failures or [],
            "failed_count": len(failures or []),
            "row_count": len(rows),
            "summary": counts,
        }
        (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._cache.pop(str(target), None)

    def list_collections(self, *, owner: str | None = None, limit: int = 50) -> list[dict]:
        items = []
        for path in self._iter_collection_dirs():
            meta = self._read_json(path / "meta.json")
            if not meta:
                continue
            meta_owner = meta.get("owner")
            if owner and meta_owner and meta_owner != owner:
                continue
            if owner and not meta_owner:
                # Pre-0.0.9 collections had no owner. Keep them visible rather
                # than orphaning existing test data.
                pass
            items.append(meta)
        items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
        return items[:limit]

    def load_collection(self, transaction_id: str, *, owner: str | None = None) -> dict | None:
        target = self.path_for(transaction_id)
        meta_path = target / "meta.json"
        meta = self._read_json(meta_path)
        if not meta:
            return None
        meta_owner = meta.get("owner")
        if owner and meta_owner and meta_owner != owner:
            return None
        cache_key = str(target)
        try:
            mtime = meta_path.stat().st_mtime
        except OSError:
            mtime = 0
        cached = self._cache.get(cache_key)
        if cached and cached[0] == mtime:
            return cached[1]
        rows = []
        rows_path = target / "rows.jsonl"
        if rows_path.exists():
            with rows_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(LogLine.from_dict(json.loads(line)))
        downloads = {d["name"]: target / "downloads" / d["name"] for d in meta.get("downloads", [])}
        loaded = {"meta": meta, "rows": rows, "downloads": downloads}
        self._cache[cache_key] = (mtime, loaded)
        return loaded

    def delete_collection(self, transaction_id: str, *, owner: str | None = None) -> bool:
        loaded = self.load_collection(transaction_id, owner=owner)
        if not loaded:
            return False
        target = self.path_for(transaction_id)
        shutil.rmtree(target, ignore_errors=True)
        self._cache.pop(str(target), None)
        return True

    def download_path(self, transaction_id: str, name: str, *, owner: str | None = None) -> Path | None:
        if not self.load_collection(transaction_id, owner=owner):
            return None
        path = self.path_for(transaction_id) / "downloads" / name
        if path.exists() and path.is_file():
            return path
        return None

    def build_all_logs_zip(self, transaction_id: str, *, owner: str | None = None) -> bytes | None:
        loaded = self.load_collection(transaction_id, owner=owner)
        if not loaded:
            return None
        downloads_dir = self.path_for(transaction_id) / "downloads"
        raw_files = [downloads_dir / d["name"] for d in loaded["meta"].get("downloads", [])]
        extracted: list[tuple[str, bytes, str]] = []
        for raw_zip in raw_files:
            if not raw_zip.exists():
                continue
            try:
                with zipfile.ZipFile(raw_zip, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        leaf = Path(info.filename).name
                        if not leaf:
                            continue
                        extracted.append((leaf, zf.read(info), raw_zip.stem))
            except zipfile.BadZipFile:
                extracted.append((raw_zip.name, raw_zip.read_bytes(), raw_zip.stem))
        if not extracted:
            return None
        counts = Counter(name for name, _, _ in extracted)
        used = Counter()
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for name, blob, source_stem in extracted:
                if counts[name] > 1:
                    archive_name = f"{source_stem}__{name}"
                else:
                    archive_name = name
                used[archive_name] += 1
                if used[archive_name] > 1:
                    archive_name = f"{Path(archive_name).stem}_{used[archive_name]}{Path(archive_name).suffix}"
                zf.writestr(archive_name, blob)
        return out.getvalue()

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
