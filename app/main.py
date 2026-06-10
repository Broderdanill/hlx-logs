from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import uuid

from fastapi import FastAPI, Form, Request, HTTPException, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse, JSONResponse
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

APP_VERSION = "0.0.8"

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
    recent = store.list_collections(limit=8)
    return templates.TemplateResponse(
        "index.html",
        {
            **base_context(request),
            "pods": cfg.pods,
            "log_types": cfg.log_types,
            "ar": cfg.ar,
            "storage": cfg.storage,
            "recent": recent,
        },
    )


@app.get("/collections", response_class=HTMLResponse)
async def collections_page(request: Request):
    require_jwt(request)
    return templates.TemplateResponse("collections.html", {**base_context(request), "collections": store.list_collections(limit=100), "storage": runtime_config.get().storage})


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
        "status": "queued",
        "message": "Queued",
        "current": 0,
        "total": max(len(pairs) * 3, 1),
        "result_url": None,
        "error": None,
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    background_tasks.add_task(_collect_job, transaction_id, jwt, [(asdict(p), asdict(l)) for p, l in pairs])
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


async def _collect_job(transaction_id: str, jwt: str, pair_dicts: list[tuple[dict, dict]]):
    cfg = runtime_config.get()
    store.cleanup()
    client = ArClient(cfg.ar, jwt)
    raw_downloads: dict[str, bytes] = {}
    parsed: list[LogLine] = []
    request_meta: list[dict] = []
    step = 0
    total = max(len(pair_dicts) * 3, 1)
    _job_update(transaction_id, status="running", message="Fetching logs...", step=step, total=total)
    try:
        for pod_data, log_data in pair_dicts:
            pod_id = pod_data["id"]
            filename = log_data["filename"]
            _job_update(transaction_id, message=f"Requesting {filename} from {pod_id}", step=step, total=total)
            entry_id = await client.create_log_request(pod_id, log_data["directory"], filename, transaction_id)
            request_meta.append({"pod": pod_data, "log_type": log_data, "entry_id": entry_id})
            step += 1
            _job_update(transaction_id, step=step)

        entries: list[dict] = []
        for meta in request_meta:
            entry_id = meta.get("entry_id")
            if not entry_id:
                continue
            try:
                _job_update(transaction_id, message=f"Reading entry {entry_id}", step=step)
                entry = await client.get_entry(entry_id)
                entry["_hlx_entry_id"] = entry_id
                entry["_hlx_request_meta"] = meta
                entries.append(entry)
            except ArRestError as exc:
                logger.warning("Could not read created entry %s: %s", entry_id, exc)
            step += 1
            _job_update(transaction_id, step=step)

        if len(entries) < len(pair_dicts):
            deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
            while asyncio.get_event_loop().time() < deadline:
                queried = await client.query_entries_by_transaction(transaction_id)
                if len(queried) >= len(pair_dicts):
                    entries = queried
                    break
                _job_update(transaction_id, message=f"Waiting for generated attachments ({len(queried)}/{len(pair_dicts)})", step=step)
                await asyncio.sleep(cfg.ar.poll_interval_seconds)

        if not entries:
            raise ArRestError(f"No entries found for TransactionId {transaction_id}")

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
                logger.warning("Skipping entry without entry id: %s", entry)
                continue

            _job_update(transaction_id, message=f"Downloading {filename} from {pod}", step=step)
            blob = None
            deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
            last_error = None
            while asyncio.get_event_loop().time() < deadline:
                try:
                    blob = await client.download_attachment(entry_id)
                    break
                except ArRestError as exc:
                    last_error = exc
                    await asyncio.sleep(cfg.ar.poll_interval_seconds)
            if blob is None:
                logger.warning("Could not download attachment for entry %s: %s", entry_id, last_error)
                continue

            key = f"{pod}__{filename}__{entry_id}.zip"
            raw_downloads[key] = blob
            for zip_name, text in extract_zip_files(blob).items():
                parsed.extend(parse_log_text(text, pod=pod, filename=filename, source_path=f"{directory}/{filename}::{zip_name}"))
            step += 1
            _job_update(transaction_id, step=step)

        parsed.sort(key=lambda row: (row.sort_ts, row.pod, row.filename, row.line_number))
        store.save_collection(transaction_id, request_meta, parsed, raw_downloads)
        _job_update(transaction_id, status="complete", message="Done", step=total, result_url=f"/results/{transaction_id}")
    except Exception as exc:
        logger.exception("Collection failed")
        _job_update(transaction_id, status="error", message="Collection failed", error=str(exc))
    finally:
        await client.close()


@app.get("/progress/{transaction_id}", response_class=HTMLResponse)
async def progress_page(request: Request, transaction_id: str):
    require_jwt(request)
    job = JOBS.get(transaction_id)
    if not job and store.load_collection(transaction_id):
        return RedirectResponse(f"/results/{transaction_id}", status_code=303)
    if not job:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Job not found."}, status_code=404)
    return templates.TemplateResponse("progress.html", {**base_context(request), "job": job})


@app.get("/api/jobs/{transaction_id}")
async def job_status(request: Request, transaction_id: str):
    require_jwt(request)
    job = JOBS.get(transaction_id)
    if not job and store.load_collection(transaction_id):
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
):
    require_jwt(request)
    loaded = store.load_collection(transaction_id)
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

    levels = sorted({r.level for r in rows if r.level})
    files = sorted({r.filename for r in rows})
    pods = sorted({r.pod for r in rows})
    transactions = []
    tx_counts: dict[str, int] = {}
    for r in rows:
        if r.transaction:
            tx_counts[r.transaction] = tx_counts.get(r.transaction, 0) + 1
    transactions = sorted(tx_counts.items(), key=lambda item: item[1], reverse=True)[:100]

    by_file: dict[str, list[LogLine]] = {}
    if mode == "file":
        for row in filtered[:5000]:
            by_file.setdefault(row.filename, []).append(row)
    by_transaction: dict[str, list[LogLine]] = {}
    if mode == "transaction":
        for row in filtered[:5000]:
            key = row.transaction or "No transaction id"
            by_transaction.setdefault(key, []).append(row)

    return templates.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "result": loaded["meta"],
            "downloads": loaded["downloads"],
            "rows": filtered[:5000],
            "total_rows": len(rows),
            "shown_rows": min(len(filtered), 5000),
            "filtered_rows": len(filtered),
            "q": q,
            "level": level,
            "mode": mode,
            "selected_transaction": transaction,
            "selected_file": file,
            "selected_pod": pod,
            "levels": levels,
            "files": files,
            "pods": pods,
            "transactions": transactions,
            "by_file": by_file,
            "by_transaction": by_transaction,
        },
    )


@app.get("/results/{transaction_id}/download/{name}")
async def download(transaction_id: str, name: str, request: Request):
    require_jwt(request)
    path = store.download_path(transaction_id, name)
    if not path:
        raise HTTPException(status_code=404, detail="Download not found")
    return FileResponse(path, media_type="application/zip", filename=name)
