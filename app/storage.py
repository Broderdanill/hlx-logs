from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import io
import json
import logging
from pathlib import Path
import re
import shutil
import sqlite3
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
        self._write_index(target, rows)
        self._cache.pop(str(target), None)


    def list_owners(self) -> list[dict]:
        counts = Counter()
        for path in self._iter_collection_dirs():
            meta = self._read_json(path / "meta.json")
            if not meta:
                continue
            owner = meta.get("owner") or "unknown"
            counts[owner] += 1
        return [{"owner": owner, "count": count} for owner, count in sorted(counts.items(), key=lambda x: (x[0].lower()))]

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


    def extend_collection(self, transaction_id: str, *, owner: str | None, requests: list[dict], rows: list[LogLine], downloads: dict[str, bytes], failures: list[dict] | None = None) -> bool:
        loaded = self.load_collection(transaction_id, owner=owner)
        if not loaded:
            return False
        target = self.path_for(transaction_id)
        downloads_dir = target / "downloads"
        downloads_dir.mkdir(exist_ok=True)
        meta = loaded["meta"]
        existing_names = {d.get("name") for d in meta.get("downloads", [])}
        for name, blob in downloads.items():
            safe = safe_name(name)
            if safe in existing_names:
                p = Path(safe)
                idx = 2
                while f"{p.stem}_{idx}{p.suffix}" in existing_names:
                    idx += 1
                safe = f"{p.stem}_{idx}{p.suffix}"
            (downloads_dir / safe).write_bytes(blob)
            meta.setdefault("downloads", []).append({"name": safe, "bytes": len(blob)})
            existing_names.add(safe)
        if requests:
            meta.setdefault("requests", []).extend(requests)
        if failures:
            meta.setdefault("failures", []).extend(failures)
        all_rows = list(loaded.get("rows", [])) + list(rows)
        all_rows.sort(key=lambda r: (r.sort_ts, r.pod, r.filename, r.line_number))
        with (target / "rows.jsonl").open("w", encoding="utf-8") as f:
            for row in all_rows:
                f.write(row.to_json() + "\n")
        meta["row_count"] = len(all_rows)
        meta["failed_count"] = len(meta.get("failures", []))
        meta["summary"] = self._summarize_rows(all_rows)
        meta["updated_at"] = utc_now().isoformat()
        (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._write_index(target, all_rows)
        self._cache.pop(str(target), None)
        return True

    def reindex_collection(self, transaction_id: str, rows: list[LogLine], *, owner: str | None = None) -> bool:
        loaded = self.load_collection(transaction_id, owner=owner)
        if not loaded:
            return False
        target = self.path_for(transaction_id)
        meta = loaded["meta"]
        rows.sort(key=lambda r: (r.sort_ts, r.pod, r.filename, r.line_number))
        with (target / "rows.jsonl").open("w", encoding="utf-8") as f:
            for row in rows:
                f.write(row.to_json() + "\n")
        meta["row_count"] = len(rows)
        meta["summary"] = self._summarize_rows(rows)
        meta["updated_at"] = utc_now().isoformat()
        (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._write_index(target, rows)
        self._cache.pop(str(target), None)
        return True

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


    def rename_collection(self, transaction_id: str, name: str, *, owner: str | None = None) -> bool:
        loaded = self.load_collection_meta(transaction_id, owner=owner)
        if not loaded:
            return False
        target = self.path_for(transaction_id)
        meta = loaded["meta"]
        cleaned = (name or "").strip()
        if cleaned:
            meta["collection_name"] = cleaned[:160]
        else:
            meta.pop("collection_name", None)
        meta["updated_at"] = utc_now().isoformat()
        (target / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        self._cache.pop(str(target), None)
        return True

    def load_collection_meta(self, transaction_id: str, *, owner: str | None = None) -> dict | None:
        target = self.path_for(transaction_id)
        meta = self._read_json(target / "meta.json")
        if not meta:
            return None
        meta_owner = meta.get("owner")
        if owner and meta_owner and meta_owner != owner:
            return None
        downloads = {d["name"]: target / "downloads" / d["name"] for d in meta.get("downloads", [])}
        return {"meta": meta, "downloads": downloads}

    def _db_path(self, target: Path) -> Path:
        return target / "rows.sqlite3"

    def _connect(self, target: Path) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path(target))
        conn.row_factory = sqlite3.Row
        return conn

    def _write_index(self, target: Path, rows: list[LogLine]) -> None:
        """Persist a compact SQLite index for fast result filtering.

        rows.jsonl is kept as a portable fallback/export format, while this
        index is optimized for UI filtering. It lives in the temporary /data
        collection directory and is rebuilt whenever a collection is changed.
        """
        db_path = self._db_path(target)
        if db_path.exists():
            db_path.unlink()
        conn = self._connect(target)
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA temp_store=MEMORY")
            conn.execute("""
                CREATE TABLE rows (
                    id INTEGER PRIMARY KEY,
                    sort_ts REAL,
                    sort_ts_text TEXT,
                    display_ts TEXT,
                    level TEXT,
                    pod TEXT,
                    filename TEXT,
                    source_path TEXT,
                    line_number INTEGER,
                    message TEXT,
                    raw TEXT,
                    ar_code TEXT,
                    transaction_id TEXT,
                    tid TEXT,
                    rpc_id TEXT,
                    queue TEXT,
                    user_name TEXT,
                    thread TEXT,
                    operation TEXT,
                    component TEXT,
                    duration_ms REAL,
                    form_name TEXT,
                    workflow TEXT,
                    field_name TEXT,
                    event_type TEXT,
                    tags TEXT
                )
            """)
            payload = []
            for row in rows:
                ts = None if row.sort_ts.year == 9999 else row.sort_ts.timestamp()
                payload.append((
                    ts, row.sort_ts.isoformat(), row.display_ts, row.level, row.pod, row.filename,
                    row.source_path, row.line_number, row.message, row.raw, row.ar_code,
                    row.transaction, row.tid, row.rpc_id, row.queue, row.user, row.thread,
                    row.operation, row.component, row.duration_ms, getattr(row, "form", ""),
                    getattr(row, "workflow", ""), getattr(row, "field_name", ""),
                    getattr(row, "event_type", ""), json.dumps(row.tags or [], ensure_ascii=False),
                ))
            conn.executemany("""
                INSERT INTO rows (
                    sort_ts, sort_ts_text, display_ts, level, pod, filename, source_path, line_number,
                    message, raw, ar_code, transaction_id, tid, rpc_id, queue, user_name, thread,
                    operation, component, duration_ms, form_name, workflow, field_name, event_type, tags
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, payload)
            conn.execute("CREATE INDEX idx_rows_time ON rows(sort_ts)")
            conn.execute("CREATE INDEX idx_rows_user ON rows(user_name)")
            conn.execute("CREATE INDEX idx_rows_form ON rows(form_name)")
            conn.execute("CREATE INDEX idx_rows_file ON rows(filename)")
            conn.execute("CREATE INDEX idx_rows_level ON rows(level)")
            conn.execute("CREATE INDEX idx_rows_event ON rows(event_type)")
            conn.execute("CREATE INDEX idx_rows_tx ON rows(transaction_id, tid, rpc_id)")
            conn.commit()
        finally:
            conn.close()

    def ensure_index(self, transaction_id: str, *, owner: str | None = None) -> bool:
        target = self.path_for(transaction_id)
        meta = self._read_json(target / "meta.json")
        if not meta:
            return False
        meta_owner = meta.get("owner")
        if owner and meta_owner and meta_owner != owner:
            return False
        if self._db_path(target).exists():
            return True
        rows = []
        rows_path = target / "rows.jsonl"
        if rows_path.exists():
            with rows_path.open("r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        rows.append(LogLine.from_dict(json.loads(line)))
        self._write_index(target, rows)
        return True

    def query_rows(self, transaction_id: str, *, owner: str | None = None, q: str = "", user: str = "", form: str = "", start: str = "", end: str = "", file: str = "", level: str = "", ignore_failed: bool = False, tx: str = "", limit: int = 500) -> dict | None:
        if not self.ensure_index(transaction_id, owner=owner):
            return None
        target = self.path_for(transaction_id)
        where, args = self._build_where(q=q, user=user, form=form, start=start, end=end, file=file, level=level, ignore_failed=ignore_failed)
        if tx.strip():
            extra_clause = "(transaction_id = ? OR tid = ? OR rpc_id = ?)"
            if where:
                where += " AND " + extra_clause
            else:
                where = " WHERE " + extra_clause
            args.extend([tx.strip(), tx.strip(), tx.strip()])
        conn = self._connect(target)
        try:
            total = conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
            filtered = conn.execute(f"SELECT COUNT(*) FROM rows {where}", args).fetchone()[0]
            sql = f"SELECT * FROM rows {where} ORDER BY COALESCE(sort_ts, 32503680000), pod, filename, line_number LIMIT ?"
            row_args = list(args) + [limit]
            rows = [self._row_to_dict(r) for r in conn.execute(sql, row_args).fetchall()]
            return {"total": total, "filtered": filtered, "rows": rows}
        finally:
            conn.close()

    def query_facets(self, transaction_id: str, *, owner: str | None = None) -> dict | None:
        if not self.ensure_index(transaction_id, owner=owner):
            return None
        target = self.path_for(transaction_id)
        conn = self._connect(target)
        try:
            def top(field: str, limit: int = 300):
                return [(r[0], r[1]) for r in conn.execute(f"SELECT {field}, COUNT(*) c FROM rows WHERE {field} IS NOT NULL AND {field} != '' GROUP BY {field} ORDER BY c DESC, {field} LIMIT ?", (limit,)).fetchall()]
            return {
                "users": top("user_name", 200),
                "forms": top("form_name", 300),
                "files": top("filename", 300),
                "levels": top("level", 20),
                "transactions": [(r[0], r[1]) for r in conn.execute("""
                    SELECT tx, COUNT(*) c FROM (
                        SELECT transaction_id tx FROM rows WHERE transaction_id != ''
                        UNION ALL SELECT tid tx FROM rows WHERE tid != ''
                        UNION ALL SELECT rpc_id tx FROM rows WHERE rpc_id != ''
                    ) GROUP BY tx ORDER BY c DESC, tx LIMIT 200
                """).fetchall()],
                "event_types": top("event_type", 50),
            }
        finally:
            conn.close()

    def query_flow_rows(self, transaction_id: str, *, owner: str | None = None, q: str = "", user: str = "", form: str = "", start: str = "", end: str = "", file: str = "", level: str = "", ignore_failed: bool = False, tx: str = "", limit: int = 200, workflow_only: bool = False, filter_log_only: bool = False) -> list[dict] | None:
        if not self.ensure_index(transaction_id, owner=owner):
            return None
        target = self.path_for(transaction_id)
        where, args = self._build_where(q=q, user=user, form=form, start=start, end=end, file=file, level=level, ignore_failed=ignore_failed)
        if tx.strip():
            extra_clause = "(transaction_id = ? OR tid = ? OR rpc_id = ?)"
            if where:
                where += " AND " + extra_clause
            else:
                where = " WHERE " + extra_clause
            args.extend([tx.strip(), tx.strip(), tx.strip()])
        extra = " AND " if where else " WHERE "
        event_filter = "(event_type != '' OR transaction_id != '' OR tid != '' OR rpc_id != '' OR user_name != '' OR form_name != '' OR level IN ('ERROR','WARN'))"
        if workflow_only:
            workflow_filter = "("
            if filter_log_only:
                workflow_filter += "LOWER(filename) LIKE 'arfilter%' OR event_type IN ('Filter','Filter Guide','Set Field')"
            else:
                workflow_filter += "LOWER(filename) LIKE 'arfilter%' OR LOWER(filename) LIKE 'aresc%' OR LOWER(filename) LIKE '%escalation%' "
                workflow_filter += "OR LOWER(filename) LIKE '%active%link%' OR LOWER(filename) LIKE '%active_link%' OR LOWER(filename) LIKE '%progressive%' "
                workflow_filter += "OR event_type IN ('Filter','Filter Guide','Escalation','Active Link','Set Field')"
            workflow_filter += ")"
            event_filter = f"({event_filter} AND {workflow_filter})"
        conn = self._connect(target)
        try:
            sql = f"SELECT * FROM rows {where}{extra}{event_filter} ORDER BY COALESCE(sort_ts, 32503680000), pod, filename, line_number LIMIT ?"
            return [self._row_to_dict(r) for r in conn.execute(sql, list(args) + [limit]).fetchall()]
        finally:
            conn.close()

    def _build_where(self, *, q: str = "", user: str = "", form: str = "", start: str = "", end: str = "", file: str = "", level: str = "", ignore_failed: bool = False) -> tuple[str, list]:
        clauses: list[str] = []
        args: list = []
        if q.strip():
            clauses.append("(message LIKE ? OR raw LIKE ? OR workflow LIKE ? OR field_name LIKE ?)")
            like = f"%{q.strip()}%"
            args.extend([like, like, like, like])
        if user.strip():
            clauses.append("user_name LIKE ?")
            args.append(f"%{user.strip()}%")
        if form.strip():
            clauses.append("form_name = ?")
            args.append(form.strip())
        if file.strip():
            clauses.append("filename = ?")
            args.append(file.strip())
        if level.strip():
            clauses.append("level = ?")
            args.append(level.strip().upper())

        if ignore_failed:
            clauses.append("NOT (LOWER(message) LIKE ? OR LOWER(operation) LIKE ?)")
            args.extend(["%failed qualification%", "%failed qualification%"])
        for value, op in ((start, ">="), (end, "<=")):
            ts = self._parse_filter_ts(value)
            if ts is not None:
                clauses.append(f"sort_ts {op} ?")
                args.append(ts)
        return (" WHERE " + " AND ".join(clauses), args) if clauses else ("", args)

    @staticmethod
    def _parse_filter_ts(value: str) -> float | None:
        value = (value or "").strip()
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).timestamp()
        except Exception:
            return None

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict:
        data = dict(row)
        data["transaction"] = data.pop("transaction_id", "")
        data["user"] = data.pop("user_name", "")
        data["form"] = data.pop("form_name", "")
        try:
            data["tags"] = json.loads(data.get("tags") or "[]")
        except Exception:
            data["tags"] = []
        return data

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
