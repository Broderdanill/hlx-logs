from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import logging
import os
from pathlib import Path
import uuid

from fastapi import FastAPI, Form, Request, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .ar_client import ArClient, ArRestError
from .logging_config import configure_logging
from .parser import extract_zip_files, parse_log_text, LogLine
from .runtime_config import RuntimeConfig
from .settings import load_config, PodConfig, LogTypeConfig

configure_logging()
logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
config = load_config(CONFIG_PATH)
runtime_config = RuntimeConfig(config)

app = FastAPI(title="hlx-logs", version="0.1.0")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"), same_site="lax")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# In-memory session results. Good enough for the first version; use DB later if needed.
RESULTS: dict[str, dict] = {}


def require_jwt(request: Request) -> str:
    jwt = request.session.get("ar_jwt")
    if not jwt:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return jwt


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "base_url": runtime_config.get().ar.base_url})


@app.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    cfg = runtime_config.get()
    client = ArClient(cfg.ar)
    try:
        token = await client.login(username, password)
    except ArRestError as exc:
        logger.warning("Login failed for user %s: %s", username, exc)
        return templates.TemplateResponse("login.html", {"request": request, "error": str(exc), "base_url": cfg.ar.base_url}, status_code=401)
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
    availability = {
        log.id: [pod.id for pod in cfg.pods if pod.enabled and RuntimeConfig.is_log_available_on_pod(log, pod)]
        for log in cfg.log_types if log.enabled
    }
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "username": request.session.get("username"),
            "pods": cfg.pods,
            "log_types": cfg.log_types,
            "availability": availability,
            "ar": cfg.ar,
        },
    )


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    require_jwt(request)
    cfg = runtime_config.get()
    return templates.TemplateResponse("config.html", {"request": request, "pods": cfg.pods, "log_types": cfg.log_types})


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
        )
    )
    return RedirectResponse("/config", status_code=303)


@app.post("/collect")
async def collect(request: Request, pod_ids: list[str] = Form(default=[]), log_type_ids: list[str] = Form(default=[])):
    jwt = require_jwt(request)
    cfg = runtime_config.get()
    pods = [p for p in cfg.pods if p.id in pod_ids and p.enabled]
    log_types = [l for l in cfg.log_types if l.id in log_type_ids and l.enabled]
    pairs = [(p, l) for p in pods for l in log_types if RuntimeConfig.is_log_available_on_pod(l, p)]
    if not pairs:
        return templates.TemplateResponse("error.html", {"request": request, "message": "No valid pod/log combinations selected."}, status_code=400)

    transaction_id = uuid.uuid4().hex
    client = ArClient(cfg.ar, jwt)
    raw_downloads: dict[str, bytes] = {}
    parsed: list[LogLine] = []
    request_meta: list[dict] = []

    try:
        for pod, log_type in pairs:
            entry_id = await client.create_log_request(pod.id, log_type.directory, log_type.filename, transaction_id)
            request_meta.append({"pod": asdict(pod), "log_type": asdict(log_type), "entry_id": entry_id})

        # The custom filter may create/update attachments asynchronously. Poll entries for this transaction.
        deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
        entries: list[dict] = []
        while asyncio.get_event_loop().time() < deadline:
            entries = await client.query_entries_by_transaction(transaction_id)
            if len(entries) >= len(pairs):
                break
            await asyncio.sleep(cfg.ar.poll_interval_seconds)

        logger.info("Transaction %s returned %s entries", transaction_id, len(entries))
        if not entries:
            raise ArRestError(f"No result entries found for TransactionId {transaction_id}")

        for entry in entries:
            values = entry.get("values", {})
            entry_id = values.get("Request ID") or entry.get("_links", {}).get("self", [{}])[0].get("href", "").rstrip("/").split("/")[-1]
            pod = values.get("Pod", "unknown")
            filename = values.get("Filename", "unknown.log")
            directory = values.get("Directory", "")
            if not entry_id:
                logger.warning("Skipping entry without entry id: %s", entry)
                continue
            try:
                blob = await client.download_attachment(entry_id)
            except ArRestError as exc:
                logger.warning("Could not download attachment for entry %s: %s", entry_id, exc)
                continue
            key = f"{pod}__{filename}__{entry_id}.zip"
            raw_downloads[key] = blob
            for zip_name, text in extract_zip_files(blob).items():
                parsed.extend(parse_log_text(text, pod=pod, filename=filename, source_path=f"{directory}/{filename}::{zip_name}"))
    except ArRestError as exc:
        logger.exception("Collection failed")
        return templates.TemplateResponse("error.html", {"request": request, "message": str(exc)}, status_code=500)
    finally:
        await client.close()

    parsed.sort(key=lambda row: (row.sort_ts, row.pod, row.filename, row.line_number))
    RESULTS[transaction_id] = {
        "transaction_id": transaction_id,
        "created_at": datetime.now(timezone.utc),
        "requests": request_meta,
        "rows": parsed,
        "downloads": raw_downloads,
    }
    return RedirectResponse(f"/results/{transaction_id}", status_code=303)


@app.get("/results/{transaction_id}", response_class=HTMLResponse)
async def results(request: Request, transaction_id: str, q: str = "", level: str = ""):
    require_jwt(request)
    result = RESULTS.get(transaction_id)
    if not result:
        return templates.TemplateResponse("error.html", {"request": request, "message": "Result not found in this running session."}, status_code=404)
    rows: list[LogLine] = result["rows"]
    filtered = rows
    if q:
        q_lower = q.lower()
        filtered = [r for r in filtered if q_lower in r.raw.lower() or q_lower in r.pod.lower() or q_lower in r.filename.lower()]
    if level:
        filtered = [r for r in filtered if r.level == level.upper()]
    levels = sorted({r.level for r in rows if r.level})
    return templates.TemplateResponse(
        "results.html",
        {
            "request": request,
            "result": result,
            "rows": filtered[:5000],
            "total_rows": len(rows),
            "shown_rows": min(len(filtered), 5000),
            "filtered_rows": len(filtered),
            "q": q,
            "level": level,
            "levels": levels,
        },
    )


@app.get("/results/{transaction_id}/download/{name}")
async def download(transaction_id: str, name: str, request: Request):
    require_jwt(request)
    result = RESULTS.get(transaction_id)
    if not result or name not in result["downloads"]:
        raise HTTPException(status_code=404, detail="Download not found")
    return Response(
        result["downloads"][name],
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )
