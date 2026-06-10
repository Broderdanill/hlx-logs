from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import logging
import os
from pathlib import Path
import uuid
from urllib.parse import urlencode
import io
import zipfile
import gzip

from fastapi import FastAPI, Form, Request, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .ar_client import ArClient, ArRestError, _entry_id_from_links
from .logging_config import configure_logging
from .parser import extract_zip_files, parse_log_text, LogLine
from .runtime_config import RuntimeConfig
from .settings import load_config, PodConfig, LogTypeConfig
from .storage import CollectionStore

configure_logging()
logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
config = load_config(CONFIG_PATH)
runtime_config = RuntimeConfig(config)
store = CollectionStore(config.storage)
store.cleanup()

APP_VERSION = "0.0.12"

app = FastAPI(title="hlx-logs", version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"), same_site="lax")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

JOBS: dict[str, dict] = {}


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(Path(__file__).parent / "static" / "favicon.png")


def require_jwt(request: Request) -> str:
    jwt = request.session.get("ar_jwt")
    if not jwt:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return jwt


def base_context(request: Request) -> dict:
    return {
        "request": request,
        "version": APP_VERSION,
        "username": request.session.get("username"),
    }


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "version": APP_VERSION,
        "retention_days": runtime_config.get().storage.retention_days,
        "time": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {**base_context(request), "base_url": runtime_config.get().ar.base_url})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    cfg = runtime_config.get()
    client = ArClient(cfg.ar)
    try:
        token = await client.login(username, password)
        if cfg.security.require_admin_group:
            is_admin = await client.user_is_member_of_group(
                username=username,
                user_form=cfg.security.user_form,
                login_field=cfg.security.login_field,
                group_list_field=cfg.security.group_list_field,
                group_id=cfg.security.admin_group_id,
            )
            if not is_admin:
                raise ArRestError(f"User {username!r} is not a member of required AR group id {cfg.security.admin_group_id}.")
    except ArRestError as exc:
        logger.warning("Login failed for user %s: %s", username, exc)
        return templates.TemplateResponse("login.html", {**base_context(request), "error": str(exc), "base_url": cfg.ar.base_url}, status_code=401)
    finally:
        await client.close()
    request.session["ar_jwt"] = token
    request.session["username"] = username
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    jwt = request.session.get("ar_jwt")
    cfg = runtime_config.get()
    if jwt:
        client = ArClient(cfg.ar, jwt)
        try:
            await client.logout()
        except Exception:
            logger.debug("AR logout failed; clearing local session anyway", exc_info=True)
        finally:
            await client.close()
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    require_jwt(request)
    cfg = runtime_config.get()
    recent = store.list_collections(owner=request.session.get("username"), limit=8)
    return templates.TemplateResponse(
        "index.html",
        {
            **base_context(request),
            "pods": cfg.pods,
            "log_types": cfg.log_types,
            "ar": cfg.ar,
            "storage": cfg.storage,
            "security": cfg.security,
            "recent": recent,
        },
    )


@app.get("/collections", response_class=HTMLResponse)
async def collections_page(request: Request):
    require_jwt(request)
    return templates.TemplateResponse("collections.html", {**base_context(request), "collections": store.list_collections(owner=request.session.get("username"), limit=100), "storage": runtime_config.get().storage})


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    require_jwt(request)
    cfg = runtime_config.get()
    return templates.TemplateResponse("config.html", {**base_context(request), "pods": cfg.pods, "log_types": cfg.log_types, "storage": cfg.storage})


@app.post("/config/pods")
async def add_pod(request: Request, pod_id: str = Form(...), label: str = Form(...), tags: str = Form("")):
    require_jwt(request)
    runtime_config.add_pod(PodConfig(id=pod_id.strip(), label=label.strip(), tags=[t.strip() for t in tags.split(",") if t.strip()]))
    return RedirectResponse("/config", status_code=303)


@app.post("/config/log-types")
async def add_log_type(
    request: Request,
    log_id: str = Form(...),
    label: str = Form(...),
    filename: str = Form(...),
    directory: str = Form(...),
    available_on_tags: str = Form(""),
    available_on_pods: str = Form(""),
):
    require_jwt(request)
    runtime_config.add_log_type(
        LogTypeConfig(
            id=log_id.strip(),
            label=label.strip(),
            filename=filename.strip(),
            directory=directory.strip(),
            available_on_tags=[t.strip() for t in available_on_tags.split(",") if t.strip()],
            available_on_pods=[p.strip() for p in available_on_pods.split(",") if p.strip()],
            category="Custom",
            description="Added at runtime",
        )
    )
    return RedirectResponse("/config", status_code=303)


@app.post("/collect")
async def collect(request: Request, background_tasks: BackgroundTasks, pod_ids: list[str] = Form(default=[]), log_type_ids: list[str] = Form(default=[])):
    jwt = require_jwt(request)
    cfg = runtime_config.get()
    pods = [p for p in cfg.pods if p.id in pod_ids and p.enabled]
    log_types = [l for l in cfg.log_types if l.id in log_type_ids and l.enabled]
    pairs = [(p, l) for p in pods for l in log_types if RuntimeConfig.is_log_available_on_pod(l, p)]
    if not pairs:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "No valid pod/log combinations selected."}, status_code=400)

    transaction_id = uuid.uuid4().hex
    JOBS[transaction_id] = {
        "transaction_id": transaction_id,
        "owner": request.session.get("username", "unknown"),
        "status": "queued",
        "message": "Queued",
        "current": 0,
        "total": max(len(pairs) * 3, 1),
        "result_url": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    background_tasks.add_task(_collect_job, transaction_id, request.session.get("username", "unknown"), jwt, [(asdict(p), asdict(l)) for p, l in pairs])
    return RedirectResponse(f"/progress/{transaction_id}", status_code=303)


def _job_update(transaction_id: str, *, status: str | None = None, message: str | None = None, step: int | None = None, total: int | None = None, error: str | None = None, result_url: str | None = None):
    job = JOBS.setdefault(transaction_id, {"transaction_id": transaction_id})
    if status is not None:
        job["status"] = status
    if message is not None:
        job["message"] = message
    if step is not None:
        job["current"] = step
    if total is not None:
        job["total"] = total
    if error is not None:
        job["error"] = error
    if result_url is not None:
        job["result_url"] = result_url
    job["updated_at"] = datetime.now(timezone.utc).isoformat()


async def _collect_job(transaction_id: str, username: str, jwt: str, pair_dicts: list[tuple[dict, dict]]):
    cfg = runtime_config.get()
    store.cleanup()
    client = ArClient(cfg.ar, jwt)
    raw_downloads: dict[str, bytes] = {}
    parsed: list[LogLine] = []
    request_meta: list[dict] = []
    failures: list[dict] = []
    step = 0
    total = max(len(pair_dicts) * 3, 1)
    _job_update(transaction_id, status="running", message="Fetching logs...", step=step, total=total)

    def add_failure(stage: str, pod_id: str, filename: str, error: Exception | str, directory: str = "", entry_id: str = ""):
        message = str(error)
        logger.warning("Collection item failed stage=%s pod=%s file=%s entry=%s: %s", stage, pod_id, filename, entry_id, message)
        failures.append({
            "stage": stage,
            "pod": pod_id,
            "filename": filename,
            "directory": directory,
            "entry_id": entry_id,
            "error": message,
        })

    try:
        for pod_data, log_data in pair_dicts:
            pod_id = pod_data["id"]
            filename = log_data["filename"]
            directory = log_data.get("directory", "")
            _job_update(transaction_id, message=f"Requesting {filename} from {pod_id}", step=step, total=total)
            try:
                entry_id = await client.create_log_request(pod_id, directory, filename, transaction_id)
                request_meta.append({"pod": pod_data, "log_type": log_data, "entry_id": entry_id})
            except Exception as exc:
                add_failure("request", pod_id, filename, exc, directory)
                # Treat this item as consumed for request/read/download progress.
                step += 3
                _job_update(transaction_id, message=f"Skipped {filename} from {pod_id}: {exc}", step=min(step, total))
                continue
            step += 1
            _job_update(transaction_id, step=min(step, total))

        entries: list[dict] = []
        for meta in request_meta:
            entry_id = meta.get("entry_id")
            pod_id = (meta.get("pod") or {}).get("id", "unknown")
            log_type = meta.get("log_type") or {}
            filename = log_type.get("filename", "unknown.log")
            directory = log_type.get("directory", "")
            if not entry_id:
                add_failure("read", pod_id, filename, "Missing entry id", directory)
                step += 2
                _job_update(transaction_id, step=min(step, total))
                continue
            try:
                _job_update(transaction_id, message=f"Reading {filename} entry {entry_id}", step=step)
                entry = await client.get_entry(entry_id)
                entry["_hlx_entry_id"] = entry_id
                entry["_hlx_request_meta"] = meta
                entries.append(entry)
            except Exception as exc:
                add_failure("read", pod_id, filename, exc, directory, entry_id)
                # Still keep a minimal entry; direct attachment retrieval may work after the filter completes.
                entries.append({"values": {"Pod": pod_id, "Filename": filename, "Directory": directory}, "_hlx_entry_id": entry_id, "_hlx_request_meta": meta})
            step += 1
            _job_update(transaction_id, step=min(step, total))

        if len(entries) < len(request_meta):
            deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
            while asyncio.get_event_loop().time() < deadline:
                try:
                    queried = await client.query_entries_by_transaction(transaction_id)
                except Exception as exc:
                    add_failure("query", "*", "*", exc)
                    queried = []
                if len(queried) >= len(request_meta):
                    entries = queried
                    break
                _job_update(transaction_id, message=f"Waiting for generated attachments ({len(queried)}/{len(request_meta)})", step=min(step, total))
                await asyncio.sleep(cfg.ar.poll_interval_seconds)

        for entry in entries:
            values = entry.get("values", {})
            meta = entry.get("_hlx_request_meta") or {}
            meta_pod = (meta.get("pod") or {}).get("id") if isinstance(meta.get("pod"), dict) else None
            meta_log_type = meta.get("log_type") or {}
            entry_id = entry.get("_hlx_entry_id") or _entry_id_from_links(entry) or meta.get("entry_id")
            pod = values.get("Pod") or meta_pod or "unknown"
            filename = values.get("Filename") or meta_log_type.get("filename") or "unknown.log"
            directory = values.get("Directory") or meta_log_type.get("directory") or ""
            if not entry_id:
                add_failure("download", pod, filename, "Entry has no id", directory)
                step += 1
                _job_update(transaction_id, step=min(step, total))
                continue

            _job_update(transaction_id, message=f"Downloading {filename} from {pod}", step=step)
            blob = None
            deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
            last_error = None
            while asyncio.get_event_loop().time() < deadline:
                try:
                    blob = await client.download_attachment(entry_id)
                    break
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(cfg.ar.poll_interval_seconds)
            if blob is None:
                add_failure("download", pod, filename, last_error or "Attachment did not become available", directory, entry_id)
                step += 1
                _job_update(transaction_id, message=f"Skipped {filename} from {pod}: attachment unavailable", step=min(step, total))
                continue

            key = f"{pod}__{filename}__{entry_id}.zip"
            raw_downloads[key] = blob
            try:
                extracted = extract_zip_files(blob)
                if not extracted:
                    add_failure("parse", pod, filename, "Downloaded payload contained no readable files", directory, entry_id)
                for zip_name, text in extracted.items():
                    parsed.extend(parse_log_text(text, pod=pod, filename=filename, source_path=f"{directory}/{filename}::{zip_name}"))
            except Exception as exc:
                add_failure("parse", pod, filename, exc, directory, entry_id)
            step += 1
            _job_update(transaction_id, step=min(step, total))

        parsed.sort(key=lambda row: (row.sort_ts, row.pod, row.filename, row.line_number))
        if not raw_downloads and failures:
            raise ArRestError(f"No logs could be downloaded. {len(failures)} item(s) failed. First error: {failures[0]['error']}")
        store.save_collection(transaction_id, username, request_meta, parsed, raw_downloads, failures=failures)
        if failures:
            _job_update(
                transaction_id,
                status="complete_with_warnings",
                message=f"Done with {len(failures)} warning(s)",
                step=total,
                result_url=f"/results/{transaction_id}",
            )
        else:
            _job_update(transaction_id, status="complete", message="Done", step=total, result_url=f"/results/{transaction_id}")
    except Exception as exc:
        logger.exception("Collection failed")
        _job_update(transaction_id, status="error", message="Collection failed", error=str(exc))
    finally:
        await client.close()




def _known_log_filename(name: str) -> str:
    """Return the best log filename for parser metadata based on config filenames and the uploaded path."""
    leaf = Path(name).name
    cfg = runtime_config.get()
    known = {lt.filename.lower(): lt.filename for lt in cfg.log_types}
    lower_leaf = leaf.lower()
    if lower_leaf in known:
        return known[lower_leaf]
    # App downloads often use pod__filename__entry.zip. Detect the filename part.
    for key, canonical in known.items():
        if key in name.lower():
            return canonical
    return leaf or "uploaded.log"


def _guess_uploaded_pod(name: str, default_pod: str) -> str:
    leaf = Path(name).name
    if "__" in leaf:
        first = leaf.split("__", 1)[0].strip()
        if first:
            return first
    return default_pod or "uploaded"


def _iter_uploaded_log_files(name: str, blob: bytes, depth: int = 0):
    """Yield (display_name, bytes) for log files from raw uploads or zip archives.

    Supports one level of nested zip files, which covers downloaded collections
    and AR attachment zips without letting accidental recursive archives explode.
    """
    if depth > 2:
        return
    if name.lower().endswith(".gz") and not zipfile.is_zipfile(io.BytesIO(blob)):
        try:
            unzipped = gzip.decompress(blob)
            out_name = name[:-3] or "uploaded.log"
            yield (out_name, unzipped)
            return
        except Exception:
            pass
    if zipfile.is_zipfile(io.BytesIO(blob)):
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                child_name = info.filename
                child_blob = zf.read(info)
                if zipfile.is_zipfile(io.BytesIO(child_blob)) and depth < 2:
                    yield from _iter_uploaded_log_files(f"{name}/{child_name}", child_blob, depth + 1)
                else:
                    yield (child_name, child_blob)
    else:
        yield (name, blob)


@app.get("/upload", response_class=HTMLResponse)
async def upload_page(request: Request):
    require_jwt(request)
    return templates.TemplateResponse("upload.html", {**base_context(request), "storage": runtime_config.get().storage})


@app.post("/upload")
async def upload_collection(
    request: Request,
    files: list[UploadFile] = File(...),
    collection_name: str = Form(""),
    default_pod: str = Form("uploaded"),
):
    require_jwt(request)
    username = request.session.get("username", "unknown")
    transaction_id = uuid.uuid4().hex
    rows: list[LogLine] = []
    downloads: dict[str, bytes] = {}
    requests: list[dict] = []
    failures: list[dict] = []
    seen_names: dict[str, int] = {}

    def unique_name(name: str) -> str:
        safe = name.replace("/", "__").replace("\\\\", "__").strip() or "uploaded.log"
        count = seen_names.get(safe, 0) + 1
        seen_names[safe] = count
        if count == 1:
            return safe
        p = Path(safe)
        return f"{p.stem}_{count}{p.suffix}"

    for upload in files:
        original_name = upload.filename or "uploaded.log"
        try:
            blob = await upload.read()
            if not blob:
                failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": "Uploaded file was empty"})
                continue
            for inner_name, inner_blob in _iter_uploaded_log_files(original_name, blob):
                leaf = Path(inner_name).name or original_name
                filename = _known_log_filename(inner_name or original_name)
                pod = _guess_uploaded_pod(original_name, default_pod)
                # Try to decode as text; binary files are skipped but reported.
                try:
                    text = inner_blob.decode("utf-8", errors="replace")
                except Exception as exc:
                    failures.append({"stage": "parse", "pod": pod, "filename": leaf, "error": f"Could not decode uploaded file: {exc}"})
                    continue
                if "\x00" in text[:4096]:
                    failures.append({"stage": "parse", "pod": pod, "filename": leaf, "error": "Skipped likely binary file"})
                    continue
                archive_name = unique_name(f"{pod}__{filename}")
                downloads[archive_name] = inner_blob
                parsed = parse_log_text(text, pod=pod, filename=filename, source_path=f"upload::{original_name}::{inner_name}")
                rows.extend(parsed)
                requests.append({
                    "source": "upload",
                    "original_upload": original_name,
                    "filename": filename,
                    "stored_as": archive_name,
                    "pod": pod,
                    "row_count": len(parsed),
                })
        except Exception as exc:
            logger.warning("Upload processing failed for %s: %s", original_name, exc, exc_info=True)
            failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": str(exc)})

    if not rows and not downloads:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "No readable log files were found in the upload."}, status_code=400)
    rows.sort(key=lambda row: (row.sort_ts, row.pod, row.filename, row.line_number))
    # Use normal collection storage so uploaded collections behave exactly like fetched collections.
    store.save_collection(transaction_id, username, requests, rows, downloads, failures=failures)
    # Patch metadata with friendly name and source marker.
    meta_path = store.path_for(transaction_id) / "meta.json"
    try:
        import json
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["source"] = "upload"
        meta["collection_name"] = collection_name.strip() or "Uploaded collection"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        store._cache.pop(str(store.path_for(transaction_id)), None)
    except Exception:
        logger.debug("Could not patch uploaded collection metadata", exc_info=True)
    return RedirectResponse(f"/results/{transaction_id}", status_code=303)


@app.get("/progress/{transaction_id}", response_class=HTMLResponse)
async def progress_page(request: Request, transaction_id: str):
    require_jwt(request)
    job = JOBS.get(transaction_id)
    if job and job.get("owner") and job.get("owner") != request.session.get("username"):
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Job not found."}, status_code=404)
    if not job and store.load_collection(transaction_id, owner=request.session.get("username")):
        return RedirectResponse(f"/results/{transaction_id}", status_code=303)
    if not job:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Job not found."}, status_code=404)
    return templates.TemplateResponse("progress.html", {**base_context(request), "job": job})


@app.get("/api/jobs/{transaction_id}")
async def job_status(request: Request, transaction_id: str):
    require_jwt(request)
    job = JOBS.get(transaction_id)
    if job and job.get("owner") and job.get("owner") != request.session.get("username"):
        raise HTTPException(status_code=404, detail="Job not found")
    if not job and store.load_collection(transaction_id, owner=request.session.get("username")):
        return {"transaction_id": transaction_id, "status": "complete", "current": 1, "total": 1, "message": "Done", "result_url": f"/results/{transaction_id}"}
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(job)


@app.get("/results/{transaction_id}", response_class=HTMLResponse)
async def results(
    request: Request,
    transaction_id: str,
    q: str = "",
    level: str = "",
    mode: str = "combined",
    transaction: str = "",
    file: str = "",
    pod: str = "",
    tag: str = "",
    user: str = "",
    start_time: str = "",
    end_time: str = "",
    around_time: str = "",
    around_before_minutes: int = 5,
    around_after_minutes: int = 5,
    limit: int = 1000,
    offset: int = 0,
):
    require_jwt(request)
    loaded = store.load_collection(transaction_id, owner=request.session.get("username"))
    if not loaded:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Result not found. It may have expired based on retention settings."}, status_code=404)
    rows: list[LogLine] = loaded["rows"]
    filtered = rows
    if q:
        q_lower = q.lower()
        filtered = [r for r in filtered if q_lower in r.raw.lower() or q_lower in r.pod.lower() or q_lower in r.filename.lower() or q_lower in r.transaction.lower() or q_lower in r.user.lower()]
    if level:
        filtered = [r for r in filtered if r.level == level.upper()]
    if transaction:
        filtered = [r for r in filtered if r.transaction == transaction]
    if file:
        filtered = [r for r in filtered if r.filename == file]
    if pod:
        filtered = [r for r in filtered if r.pod == pod]
    if tag:
        filtered = [r for r in filtered if tag in r.tags or tag == r.operation.lower() or tag == r.component.lower()]
    if user:
        user_lower = user.lower()
        filtered = [r for r in filtered if user_lower in (r.user or "").lower()]

    def _parse_user_time(value: str):
        if not value:
            return None
        try:
            # datetime-local input arrives without timezone. Treat it as UTC because AR log parsing normalizes to UTC.
            normalized = value.strip()
            if "T" in normalized and len(normalized) == 16:
                return datetime.fromisoformat(normalized).replace(tzinfo=timezone.utc)
            parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except Exception:
            try:
                from dateutil import parser as dtparser
                parsed = dtparser.parse(value)
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.astimezone(timezone.utc)
            except Exception:
                return None

    start_dt = _parse_user_time(start_time)
    end_dt = _parse_user_time(end_time)
    around_dt = _parse_user_time(around_time)
    if around_dt:
        before = max(0, min(int(around_before_minutes or 0), 1440))
        after = max(0, min(int(around_after_minutes or 0), 1440))
        start_dt = around_dt - timedelta(minutes=before)
        end_dt = around_dt + timedelta(minutes=after)
    if start_dt:
        filtered = [r for r in filtered if r.sort_ts != datetime.max.replace(tzinfo=timezone.utc) and r.sort_ts >= start_dt]
    if end_dt:
        filtered = [r for r in filtered if r.sort_ts != datetime.max.replace(tzinfo=timezone.utc) and r.sort_ts <= end_dt]

    levels = sorted({r.level for r in rows if r.level})
    files = sorted({r.filename for r in rows})
    pods = sorted({r.pod for r in rows})
    tags = sorted({t for r in rows for t in r.tags})
    users = sorted({r.user for r in rows if r.user})[:300]
    transactions = []
    tx_counts: dict[str, int] = {}
    for r in rows:
        if r.transaction:
            tx_counts[r.transaction] = tx_counts.get(r.transaction, 0) + 1
    transactions = sorted(tx_counts.items(), key=lambda item: item[1], reverse=True)[:100]

    limit = max(100, min(limit, 2000))
    offset = max(0, offset)
    paged = filtered[offset:offset + limit]
    next_offset = offset + limit if offset + limit < len(filtered) else None
    prev_offset = max(0, offset - limit) if offset > 0 else None

    def page_url(new_offset: int | None) -> str:
        if new_offset is None:
            return ""
        params = {
            "q": q, "level": level, "mode": mode, "transaction": transaction,
            "file": file, "pod": pod, "tag": tag, "user": user,
            "start_time": start_time, "end_time": end_time,
            "around_time": around_time, "around_before_minutes": str(around_before_minutes),
            "around_after_minutes": str(around_after_minutes),
            "limit": str(limit), "offset": str(new_offset),
        }
        return f"/results/{transaction_id}?" + urlencode({k: v for k, v in params.items() if v not in ("", None)})

    by_file: dict[str, list[LogLine]] = {}
    if mode == "file":
        for row in paged:
            by_file.setdefault(row.filename, []).append(row)
    by_transaction: dict[str, list[LogLine]] = {}
    if mode == "transaction":
        for row in paged:
            key = row.transaction or "No transaction id"
            by_transaction.setdefault(key, []).append(row)

    return templates.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "result": loaded["meta"],
            "downloads": loaded["downloads"],
            "rows": paged,
            "total_rows": len(rows),
            "shown_rows": len(paged),
            "filtered_rows": len(filtered),
            "q": q,
            "level": level,
            "mode": mode,
            "selected_transaction": transaction,
            "selected_file": file,
            "selected_pod": pod,
            "selected_tag": tag,
            "selected_user": user,
            "start_time": start_time,
            "end_time": end_time,
            "around_time": around_time,
            "around_before_minutes": around_before_minutes,
            "around_after_minutes": around_after_minutes,
            "levels": levels,
            "files": files,
            "pods": pods,
            "tags": tags,
            "users": users,
            "transactions": transactions,
            "limit": limit,
            "offset": offset,
            "next_url": page_url(next_offset),
            "prev_url": page_url(prev_offset),
            "by_file": by_file,
            "by_transaction": by_transaction,
        },
    )


@app.get("/results/{transaction_id}/download/{name}")
async def download(transaction_id: str, name: str, request: Request):
    require_jwt(request)
    path = store.download_path(transaction_id, name, owner=request.session.get("username"))
    if not path:
        raise HTTPException(status_code=404, detail="Download not found")
    return FileResponse(path, media_type="application/zip", filename=name)


@app.get("/results/{transaction_id}/download-all")
async def download_all(transaction_id: str, request: Request):
    require_jwt(request)
    blob = store.build_all_logs_zip(transaction_id, owner=request.session.get("username"))
    if not blob:
        raise HTTPException(status_code=404, detail="No downloadable logs found")
    filename = f"hlx-logs-{transaction_id[:12]}.zip"
    return StreamingResponse(io.BytesIO(blob), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/collections/{transaction_id}/delete")
async def delete_collection(transaction_id: str, request: Request):
    require_jwt(request)
    ok = store.delete_collection(transaction_id, owner=request.session.get("username"))
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found")
    referer = request.headers.get("referer") or "/collections"
    if f"/results/{transaction_id}" in referer:
        referer = "/collections"
    return RedirectResponse(referer, status_code=303)
