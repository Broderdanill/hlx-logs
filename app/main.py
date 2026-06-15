from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import logging
import os
from pathlib import Path
import uuid
from urllib.parse import urlencode
import re
import io
import zipfile
import gzip

from fastapi import FastAPI, Form, Request, HTTPException, BackgroundTasks, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .ar_client import ArClient, ArRestError, _entry_id_from_links, LOG_CONTROL_DEFINITIONS, LOG_SETTING_TEMPLATES, log_control_key_for_filename
from .logging_config import configure_logging
from .parser import LogLine
from .log_analysis import rows_from_downloads, filter_rows, summarize_facets, build_flow, build_flow_from_dicts, build_mermaid_swimlane, workflow_event_kind, WORKFLOW_TYPE_ORDER, WORKFLOW_TYPE_LABELS
from .runtime_config import RuntimeConfig
from .settings import load_config, PodConfig, LogTypeConfig
from .storage import CollectionStore
from .log_classifier import classify_log_file

configure_logging()
logger = logging.getLogger(__name__)

CONFIG_PATH = os.getenv("CONFIG_PATH", "config.yaml")
config = load_config(CONFIG_PATH)
runtime_config = RuntimeConfig(config)
store = CollectionStore(config.storage)
store.cleanup()

APP_VERSION = "0.0.63"

app = FastAPI(title="hlx-logs", version=APP_VERSION)
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-only-change-me"), same_site="lax")
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

JOBS: dict[str, dict] = {}
DISCOVERY_LOCK = asyncio.Lock()


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




def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", value.strip()).strip("-._")
    return slug or "log"


def _guess_log_category(filename: str) -> str:
    name = filename.lower()
    if "error" in name or "exception" in name or "uncaught" in name:
        return "Errors"
    if "plugin" in name or "atrium" in name or "ard2p" in name:
        return "Plugin"
    if "api" in name or "filter" in name or "sql" in name or "esc" in name or "debug" in name:
        return "Trace"
    if "monitor" in name or "startup" in name or "health" in name or "probe" in name:
        return "Runtime"
    if "cache" in name or "fts" in name or "search" in name:
        return "Platform"
    return "Discovered"


def _guess_log_severity(filename: str) -> str:
    name = filename.lower()
    if "error" in name or "exception" in name or "uncaught" in name:
        return "critical"
    if "debug" in name or "trace" in name:
        return "debug"
    return "info"


def _file_size_is_zero(value: str) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"0", "0kb", "0 kb", "0.0 kb", "0 b", "0 bytes"}


async def refresh_available_logs(jwt: str, *, force: bool = False) -> dict:
    """Refresh pods/log files from AR REST discovery forms.

    Discovery uses the user's AR-JWT, so it runs after login and whenever a
    user opens the collect/config pages and the cache is stale. This avoids any
    separate service credential while still keeping the available pod/log list
    fresh for all users of the running hlx-logs pod.
    """
    cfg = runtime_config.get()
    status = runtime_config.discovery_status()
    discovery = cfg.discovery
    if not discovery.enabled:
        return status

    if not force and status.get("last_success_at"):
        try:
            last = datetime.fromisoformat(status["last_success_at"])
            if datetime.now(timezone.utc) - last < timedelta(seconds=discovery.refresh_interval_seconds):
                return status
        except Exception:
            pass

    async with DISCOVERY_LOCK:
        cfg = runtime_config.get()
        discovery = cfg.discovery
        status = runtime_config.discovery_status()
        if not force and status.get("last_success_at"):
            try:
                last = datetime.fromisoformat(status["last_success_at"])
                if datetime.now(timezone.utc) - last < timedelta(seconds=discovery.refresh_interval_seconds):
                    return status
            except Exception:
                pass

        client = ArClient(cfg.ar, jwt)
        try:
            pod_names = await client.discover_pods(
                form_name=discovery.pod_form_name,
                query=discovery.pod_query,
                value_field=discovery.pod_value_field,
            )
            pod_names = sorted(pod_names, key=lambda n: n.lower())
            pods = [PodConfig(id=name, label=name, tags=["discovered"]) for name in pod_names]
            by_filename: dict[str, LogTypeConfig] = {}
            for pod in pods:
                log_files = await client.discover_log_files_for_pod(
                    pod.id,
                    form_name=discovery.log_form_name,
                    server_field=discovery.log_server_field,
                    filename_field=discovery.log_filename_field,
                    size_field=discovery.log_size_field,
                )
                for item in log_files:
                    filename = item.get("filename", "").strip()
                    if not filename:
                        continue
                    file_size = item.get("file_size", "")
                    if not discovery.include_zero_byte_logs and _file_size_is_zero(file_size):
                        continue
                    key = filename.lower()
                    log_type = by_filename.get(key)
                    if not log_type:
                        classification = classify_log_file(filename, file_size)
                        log_type = LogTypeConfig(
                            id=_slug(filename.lower()),
                            label=filename,
                            filename=filename,
                            directory=discovery.default_directory,
                            available_on_pods=[],
                            enabled=True,
                            parser=classification.parser,
                            category=classification.category,
                            severity=classification.severity,
                            tags=classification.tags,
                            description=classification.description,
                            file_sizes_by_pod={},
                        )
                        by_filename[key] = log_type
                    if pod.id not in log_type.available_on_pods:
                        log_type.available_on_pods.append(pod.id)
                    log_type.file_sizes_by_pod[pod.id] = file_size
            category_order = {
                "Core AR": 0,
                "Performance / Exceptions": 1,
                "Combined Trace": 2,
                "API Trace": 3,
                "Filter Trace": 4,
                "SQL Trace": 5,
                "Runtime Monitor": 6,
                "Java Plug-in Server": 7,
                "Plug-ins": 8,
                "REST API": 9,
                "Web / Jetty": 10,
                "Search / FTS / AI": 11,
                "Deployment": 12,
                "CMDB / Atrium": 13,
                "Configuration / Cache": 14,
                "Email / Notification": 15,
                "Runtime Services": 16,
                "Other": 99,
            }
            # Keep the collect page predictable: pods and log files are sorted alphabetically.
            log_types = sorted(by_filename.values(), key=lambda l: l.filename.lower())
            runtime_config.replace_discovered(pods, log_types)
            logger.info("Discovered %s pod(s) and %s log file type(s)", len(pods), len(log_types))
        except Exception as exc:
            runtime_config.mark_discovery_error(str(exc))
            logger.warning("Log discovery failed: %s", exc, exc_info=True)
        finally:
            await client.close()
        return runtime_config.discovery_status()


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
    jwt = require_jwt(request)
    await refresh_available_logs(jwt)
    cfg = runtime_config.get()
    return templates.TemplateResponse(
        "index.html",
        {
            **base_context(request),
            "pods": cfg.pods,
            "log_types": cfg.log_types,
            "ar": cfg.ar,
            "storage": cfg.storage,
            "security": cfg.security,
            "discovery": cfg.discovery,
            "discovery_status": runtime_config.discovery_status(),
        },
    )


@app.get("/log-settings", response_class=HTMLResponse)
async def log_settings(request: Request, server: str = ""):
    """Separate AR server log-control page.

    Log settings controls future AR Server logging by updating the Debug-mode
    bitmask in AR System Configuration Component Setting. Collect only lists log
    files that already exist and can be fetched.
    """
    jwt = require_jwt(request)
    try:
        await refresh_available_logs(jwt)
    except Exception:
        logger.debug("Could not refresh discovery before Log settings", exc_info=True)
    cfg = runtime_config.get()
    server_names = sorted([p.id for p in cfg.pods if getattr(p, "enabled", True)], key=str.lower)
    selected_server = (server or "").strip() or (server_names[0] if server_names else "")
    if selected_server not in server_names and server_names:
        selected_server = server_names[0]
    current_debug_mode = 0
    debug_modes_by_server: dict[str, int | None] = {}
    current_filenames_by_key: dict[str, str] = {}
    restrict_log_users_enabled = False
    restrict_log_users = ""
    read_errors_by_server: dict[str, str] = {}
    read_error = ""
    if server_names:
        client = ArClient(cfg.ar, jwt)
        try:
            for server_name in server_names:
                try:
                    current = await client.get_server_debug_mode(server_name=server_name)
                    value = int(current.get("value", 0))
                    debug_modes_by_server[server_name] = value
                    if server_name == selected_server:
                        current_debug_mode = value
                except Exception as exc:
                    logger.warning("Could not read Debug-mode for %s: %s", server_name, exc, exc_info=True)
                    debug_modes_by_server[server_name] = None
                    read_errors_by_server[server_name] = str(exc)[:250]
            # Filename setting values are loaded from the selected/base server.
            # Save applies the currently shown values to all selected target pods.
            if selected_server:
                for key, definition in LOG_CONTROL_DEFINITIONS.items():
                    setting_name = str(definition.get("filename_setting") or "").strip()
                    if not setting_name:
                        continue
                    try:
                        row = await client.get_server_config_setting(server_name=selected_server, setting_name=setting_name)
                        filename = str(row.get("raw") or "").strip()
                        if filename:
                            current_filenames_by_key[key] = filename
                    except Exception as exc:
                        logger.debug("Could not read filename setting %s for %s: %s", setting_name, selected_server, exc, exc_info=True)
            if selected_server:
                try:
                    restrict_row = await client.get_server_config_setting(server_name=selected_server, setting_name="Restrict-Log-Users")
                    restrict_log_users = str(restrict_row.get("raw") or "").strip()
                    restrict_log_users_enabled = bool(restrict_log_users)
                except Exception as exc:
                    logger.debug("Could not read Restrict-Log-Users for %s: %s", selected_server, exc, exc_info=True)
            if read_errors_by_server:
                read_error = "; ".join(f"{name}: {msg}" for name, msg in read_errors_by_server.items())[:500]
        finally:
            await client.close()
    definitions = []
    for key, value in sorted(LOG_CONTROL_DEFINITIONS.items(), key=lambda item: int(item[1].get("bit_value", 0))):
        bit_value = int(value.get("bit_value", 0))
        definitions.append({
            "key": key,
            **value,
            "enabled": bool(current_debug_mode & bit_value),
            "filename": current_filenames_by_key.get(key) or value.get("default_filename", ""),
        })
    return templates.TemplateResponse(
        "log_settings.html",
        {
            **base_context(request),
            "ar": cfg.ar,
            "server_names": server_names,
            "selected_server": selected_server,
            "current_debug_mode": current_debug_mode,
            "debug_modes_by_server": debug_modes_by_server,
            "read_errors_by_server": read_errors_by_server,
            "log_setting_templates": LOG_SETTING_TEMPLATES,
            "log_control_definitions": definitions,
            "log_control_status": request.query_params.get("log_control", ""),
            "log_control_error": request.query_params.get("log_control_error", "") or read_error,
            "restrict_log_users_enabled": restrict_log_users_enabled,
            "restrict_log_users": restrict_log_users,
        },
    )


@app.post("/logs/control/save")
async def save_single_log_control(request: Request):
    """Save AR Debug-mode bitmask and log filename settings for target pods."""
    jwt = require_jwt(request)
    form = await request.form()
    selected_servers = [str(v).strip() for v in form.getlist("server_names") if str(v).strip()]
    # Backward compatibility with older single-server form posts.
    if not selected_servers:
        server_name = str(form.get("server_name", "")).strip()
        if server_name:
            selected_servers = [server_name]
    enabled_keys = [str(v).strip().lower() for v in form.getlist("log_keys") if str(v).strip()]
    if not selected_servers:
        return RedirectResponse("/log-settings?log_control_error=No%20server%20or%20pod%20was%20selected", status_code=303)

    debug_mode = 0
    unknown: list[str] = []
    for key in enabled_keys:
        definition = LOG_CONTROL_DEFINITIONS.get(key)
        if not definition:
            unknown.append(key)
            continue
        debug_mode |= int(definition.get("bit_value", 0))

    filename_updates: dict[str, str] = {}
    for key in enabled_keys:
        definition = LOG_CONTROL_DEFINITIONS.get(key)
        if not definition:
            continue
        setting_name = str(definition.get("filename_setting") or "").strip()
        if not setting_name:
            continue
        filename = str(form.get(f"filename_{key}", "")).strip()
        original_filename = str(form.get(f"original_filename_{key}", "")).strip()
        # Filename settings are only saved for log types that are enabled.
        # They are also only posted when the displayed value changed, so Save
        # does not re-write every known filename setting on every submit.
        # Empty filename values are skipped so a blank UI field cannot
        # accidentally wipe an AR server setting.
        if filename and filename != original_filename:
            filename_updates[key] = filename

    restrict_users_enabled = str(form.get("restrict_log_users_enabled", "")).lower() in {"on", "true", "1", "yes"}
    restrict_users_value = str(form.get("restrict_log_users", "")).strip()
    original_restrict_users_value = str(form.get("original_restrict_log_users", "")).strip()
    restrict_changed = (restrict_users_enabled and restrict_users_value != original_restrict_users_value) or ((not restrict_users_enabled) and bool(original_restrict_users_value))

    from urllib.parse import quote_plus
    first_server = selected_servers[0]
    if unknown:
        return RedirectResponse(f"/log-settings?server={quote_plus(first_server)}&log_control_error={quote_plus('Unknown log setting(s): ' + ', '.join(unknown))}", status_code=303)

    cfg = runtime_config.get()
    client = ArClient(cfg.ar, jwt)
    results: list[dict] = []
    filename_results: list[dict] = []
    restrict_results: list[dict] = []
    errors: list[str] = []
    try:
        for server_name in selected_servers:
            try:
                result = await client.set_server_debug_mode(server_name=server_name, debug_mode=debug_mode)
                results.append(result)
            except Exception as exc:
                logger.warning("Failed to save AR Debug-mode for %s: %s", server_name, exc, exc_info=True)
                errors.append(f"{server_name}: Debug-mode: {str(exc)[:220]}")
                # Continue with filename settings for this server; they are
                # independent AR setting rows and may still be useful.
            for key, filename in filename_updates.items():
                definition = LOG_CONTROL_DEFINITIONS.get(key) or {}
                setting_name = str(definition.get("filename_setting") or "").strip()
                if not setting_name:
                    continue
                try:
                    filename_results.append(
                        await client.set_server_config_setting_value(
                            server_name=server_name,
                            setting_name=setting_name,
                            setting_value=filename,
                        )
                    )
                except Exception as exc:
                    logger.warning("Failed to save AR filename setting %s for %s: %s", setting_name, server_name, exc, exc_info=True)
                    errors.append(f"{server_name}: {setting_name}: {str(exc)[:180]}")
            if restrict_changed:
                try:
                    if restrict_users_enabled and restrict_users_value:
                        restrict_results.append(
                            await client.upsert_server_config_setting_value(
                                server_name=server_name,
                                setting_name="Restrict-Log-Users",
                                setting_value=restrict_users_value,
                            )
                        )
                    else:
                        restrict_results.append(
                            await client.delete_server_config_setting(
                                server_name=server_name,
                                setting_name="Restrict-Log-Users",
                            )
                        )
                except Exception as exc:
                    logger.warning("Failed to save Restrict-Log-Users for %s: %s", server_name, exc, exc_info=True)
                    errors.append(f"{server_name}: Restrict-Log-Users: {str(exc)[:180]}")
    finally:
        await client.close()

    if errors:
        ok_bits = f" Saved Debug-mode for {len(results)} server(s)." if results else ""
        ok_files = f" Saved {len(filename_results)} changed filename setting(s)." if filename_results else ""
        ok_restrict = f" Saved Restrict-Log-Users for {len(restrict_results)} server(s)." if restrict_results else ""
        return RedirectResponse(f"/log-settings?server={quote_plus(first_server)}&log_control_error={quote_plus(ok_bits + ok_files + ok_restrict + ' Failed: ' + '; '.join(errors))}", status_code=303)

    changed = ", ".join(f"{r['server_name']} {r['old_value']} -> {r['new_value']}" for r in results)
    filename_msg = f" Saved {len(filename_results)} changed filename setting(s)." if filename_results else ""
    restrict_msg = f" Saved Restrict-Log-Users for {len(restrict_results)} server(s)." if restrict_results else ""
    msg = f"saved Debug-mode {debug_mode} for {len(results)} server(s): {changed}.{filename_msg}{restrict_msg}"
    return RedirectResponse(f"/log-settings?server={quote_plus(first_server)}&log_control={quote_plus(msg)}", status_code=303)


@app.post("/logs/toggle-all")
async def toggle_all_logs(request: Request, action: str = Form(...)):
    jwt = require_jwt(request)
    action_normalized = action.strip().lower()
    if action_normalized not in {"enable", "disable"}:
        return RedirectResponse("/?log_control_error=Invalid%20log%20control%20action", status_code=303)
    cfg = runtime_config.get()
    client = ArClient(cfg.ar, jwt)
    try:
        await client.set_all_server_logs(enable=(action_normalized == "enable"))
    except Exception as exc:
        logger.warning("Failed to %s all AR logs: %s", action_normalized, exc, exc_info=True)
        from urllib.parse import quote_plus
        return RedirectResponse(f"/?log_control_error={quote_plus(str(exc)[:500])}", status_code=303)
    finally:
        await client.close()
    return RedirectResponse(f"/?log_control={action_normalized}", status_code=303)


@app.post("/api/log-control/all")
async def api_toggle_all_logs(request: Request):
    jwt = require_jwt(request)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    action_normalized = str(payload.get("action", "")).strip().lower()
    if action_normalized not in {"enable", "disable", "on", "off"}:
        raise HTTPException(status_code=400, detail="Use JSON body {\"action\": \"enable\"} or {\"action\": \"disable\"}.")
    enable = action_normalized in {"enable", "on"}
    cfg = runtime_config.get()
    client = ArClient(cfg.ar, jwt)
    try:
        result = await client.set_all_server_logs(enable=enable)
    finally:
        await client.close()
    return {"ok": True, "action": "enable" if enable else "disable", "result": result}


@app.get("/collections", response_class=HTMLResponse)
async def collections_page(request: Request, owner: str = ""):
    require_jwt(request)
    owners = store.list_owners()
    selected_owner = owner.strip()
    collections = store.list_collections(owner=selected_owner or None, limit=300)
    return templates.TemplateResponse(
        "collections.html",
        {
            **base_context(request),
            "collections": collections,
            "owners": owners,
            "selected_owner": selected_owner,
            "storage": runtime_config.get().storage,
        },
    )


@app.get("/config", response_class=HTMLResponse)
async def config_page(request: Request):
    jwt = require_jwt(request)
    await refresh_available_logs(jwt)
    cfg = runtime_config.get()
    return templates.TemplateResponse(
        "config.html",
        {
            **base_context(request),
            "pods": cfg.pods,
            "log_types": cfg.log_types,
            "storage": cfg.storage,
            "discovery": cfg.discovery,
            "discovery_status": runtime_config.discovery_status(),
        },
    )


@app.post("/config/refresh")
async def refresh_config(request: Request):
    jwt = require_jwt(request)
    await refresh_available_logs(jwt, force=True)
    return RedirectResponse("/config", status_code=303)


@app.post("/collect")
async def collect(request: Request, background_tasks: BackgroundTasks, pod_ids: list[str] = Form(default=[]), log_type_ids: list[str] = Form(default=[])):
    jwt = require_jwt(request)
    await refresh_available_logs(jwt)
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

    async def process_pair(pod_data: dict, log_data: dict) -> tuple[str, bytes] | None:
        nonlocal step
        pod_id = pod_data["id"]
        filename = log_data["filename"]
        directory = log_data.get("directory", "")
        meta: dict = {"pod": pod_data, "log_type": log_data, "entry_id": None}
        async with semaphore:
            _job_update(transaction_id, message=f"Requesting {filename} from {pod_id}", step=step, total=total)
            try:
                entry_id = await client.create_log_request(pod_id, directory, filename, transaction_id)
                meta["entry_id"] = entry_id
                request_meta.append(meta)
            except Exception as exc:
                add_failure("request", pod_id, filename, exc, directory)
                step += 3
                _job_update(transaction_id, message=f"Skipped {filename} from {pod_id}: {exc}", step=min(step, total))
                return None
            step += 1
            _job_update(transaction_id, step=min(step, total))

            entry_id = meta.get("entry_id")
            if not entry_id:
                add_failure("read", pod_id, filename, "Missing entry id", directory)
                step += 2
                _job_update(transaction_id, step=min(step, total))
                return None

            try:
                _job_update(transaction_id, message=f"Reading {filename} entry {entry_id}", step=step)
                entry = await client.get_entry(entry_id)
                entry["_hlx_entry_id"] = entry_id
                entry["_hlx_request_meta"] = meta
            except Exception as exc:
                add_failure("read", pod_id, filename, exc, directory, entry_id)
                # Continue anyway: the attachment URL only needs the entry id.
                entry = {"values": {"Pod": pod_id, "Filename": filename, "Directory": directory}, "_hlx_entry_id": entry_id, "_hlx_request_meta": meta}
            step += 1
            _job_update(transaction_id, step=min(step, total))

            values = entry.get("values", {})
            # Trust the request metadata first. Some concurrent service-call
            # responses may echo stale/current field values from another request,
            # especially when the same filename is requested from multiple pods.
            pod = pod_id
            filename_for_store = filename
            directory_for_store = directory
            _job_update(transaction_id, message=f"Downloading {filename_for_store} from {pod}", step=step)
            blob = None
            deadline = asyncio.get_event_loop().time() + cfg.ar.poll_timeout_seconds
            last_error = None
            while asyncio.get_event_loop().time() < deadline:
                try:
                    blob = await client.download_attachment(str(entry_id))
                    break
                except Exception as exc:
                    last_error = exc
                    await asyncio.sleep(cfg.ar.poll_interval_seconds)
            if blob is None:
                add_failure("download", pod, filename_for_store, last_error or "Attachment did not become available", directory_for_store, str(entry_id))
                step += 1
                _job_update(transaction_id, message=f"Skipped {filename_for_store} from {pod}: attachment unavailable", step=min(step, total))
                return None

            key = f"{pod}__{filename_for_store}__{entry_id}.zip"
            step += 1
            _job_update(transaction_id, step=min(step, total))
            return key, blob

    semaphore = asyncio.Semaphore(max(1, int(getattr(cfg.ar, "collect_concurrency", 4))))
    try:
        results = await asyncio.gather(*(process_pair(pod_data, log_data) for pod_data, log_data in pair_dicts))
        for item in results:
            if item:
                raw_downloads[item[0]] = item[1]

        if not raw_downloads and failures:
            raise ArRestError(f"No logs could be downloaded. {len(failures)} item(s) failed. First error: {failures[0]['error']}")
        _job_update(transaction_id, message="Saving downloaded log packages...", step=total, total=total)
        # Keep collection fast: only persist downloaded files first. Parsing/indexing is
        # started explicitly from the result page when the user wants analysis.
        store.save_collection(transaction_id, username, request_meta, [], raw_downloads, failures=failures, analysis_status="pending")
        if failures:
            _job_update(
                transaction_id,
                status="complete_with_warnings",
                message=f"Downloaded with {len(failures)} warning(s)",
                step=total,
                result_url=f"/results/{transaction_id}?tab=downloads",
            )
        else:
            _job_update(transaction_id, status="complete", message="Downloaded logs", step=total, result_url=f"/results/{transaction_id}?tab=downloads")
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
    files: list[UploadFile] = File(default=[]),
    collection_name: str = Form(""),
    default_pod: str = Form("uploaded"),
    pasted_log_text: str = Form(""),
    pasted_log_name: str = Form("pasted.log"),
    pasted_log_type: str = Form("auto"),
):
    require_jwt(request)
    username = request.session.get("username", "unknown")
    transaction_id = uuid.uuid4().hex
    downloads: dict[str, bytes] = {}
    requests: list[dict] = []
    failures: list[dict] = []
    seen_names: dict[str, int] = {}

    def unique_name(name: str) -> str:
        safe = name.replace("/", "__").replace("\\", "__").strip() or "uploaded.log"
        count = seen_names.get(safe, 0) + 1
        seen_names[safe] = count
        if count == 1:
            return safe
        p = Path(safe)
        return f"{p.stem}_{count}{p.suffix}"

    for upload in files:
        original_name = upload.filename or ""
        try:
            blob = await upload.read()
            # Browsers often submit an empty file part when only pasted text is provided.
            # Treat that as no file, not as a failed upload.
            if not blob:
                if original_name:
                    failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": "Uploaded file was empty"})
                continue
            original_name = original_name or "uploaded.log"
            name = unique_name(original_name)
            downloads[name] = blob
            requests.append({
                "source": "upload",
                "original_upload": original_name,
                "stored_as": name,
                "pod": default_pod or "uploaded",
                "bytes": len(blob),
            })
        except Exception as exc:
            logger.warning("Upload processing failed for %s: %s", original_name, exc, exc_info=True)
            failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": str(exc)})

    if pasted_log_text.strip():
        name = unique_name(pasted_log_name or "pasted.log")
        downloads[name] = pasted_log_text.encode("utf-8", errors="replace")
        requests.append({"source": "paste", "original_upload": pasted_log_name, "stored_as": name, "pod": default_pod or "uploaded", "log_type_hint": pasted_log_type, "bytes": len(downloads[name])})

    if not downloads:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "No readable files were found in the upload."}, status_code=400)
    # Store uploads first; analysis/indexing is explicit so creating a collection stays fast.
    store.save_collection(transaction_id, username, requests, [], downloads, failures=failures, analysis_status="pending")
    meta_path = store.path_for(transaction_id) / "meta.json"
    try:
        import json
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["source"] = "upload"
        meta["collection_name"] = collection_name.strip() or "Uploaded files"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        store._cache.pop(str(store.path_for(transaction_id)), None)
    except Exception:
        logger.debug("Could not patch uploaded collection metadata", exc_info=True)
    return RedirectResponse(f"/results/{transaction_id}?tab=downloads", status_code=303)


@app.get("/progress/{transaction_id}", response_class=HTMLResponse)
async def progress_page(request: Request, transaction_id: str):
    require_jwt(request)
    job = JOBS.get(transaction_id)
    if job and job.get("owner") and job.get("owner") != request.session.get("username"):
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Job not found."}, status_code=404)
    if not job and store.load_collection(transaction_id, owner=None):
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
    if not job and store.load_collection(transaction_id, owner=None):
        return {"transaction_id": transaction_id, "status": "complete", "current": 1, "total": 1, "message": "Done", "result_url": f"/results/{transaction_id}"}
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JSONResponse(job)


@app.post("/results/{transaction_id}/analyze")
async def analyze_collection(transaction_id: str, request: Request, background_tasks: BackgroundTasks):
    require_jwt(request)
    username = request.session.get("username", "unknown")
    loaded = store.load_collection_meta(transaction_id, owner=None)
    if not loaded:
        raise HTTPException(status_code=404, detail="Collection not found")
    JOBS[transaction_id] = {
        "transaction_id": transaction_id,
        "owner": username,
        "status": "running",
        "current": 0,
        "total": max(len(loaded.get("downloads", {})), 1),
        "message": "Analyzing and indexing log data...",
    }
    background_tasks.add_task(_analyze_collection_job, transaction_id, username)
    return RedirectResponse(f"/progress/{transaction_id}", status_code=303)


async def _analyze_collection_job(transaction_id: str, username: str):
    try:
        loaded = store.load_collection_meta(transaction_id, owner=None)
        if not loaded:
            raise RuntimeError("Collection not found")
        downloads_paths = loaded.get("downloads", {})
        total = max(len(downloads_paths), 1)
        _job_update(transaction_id, status="running", message="Reading downloaded log packages...", step=0, total=total)
        downloads_bytes: dict[str, bytes] = {}
        for idx, (name, path) in enumerate(downloads_paths.items(), start=1):
            if path.exists():
                _job_update(transaction_id, message=f"Reading {name}", step=idx - 1, total=total)
                downloads_bytes[name] = path.read_bytes()
                await asyncio.sleep(0)
        if not downloads_bytes:
            raise RuntimeError("No downloaded files were available for analysis")
        _job_update(transaction_id, message="Parsing log files...", step=total, total=total + 2)
        rows = rows_from_downloads(downloads_bytes, loaded["meta"].get("requests", []))
        _job_update(transaction_id, message="Building searchable index...", step=total + 1, total=total + 2)
        store.reindex_collection(transaction_id, rows, owner=None)
        _job_update(transaction_id, status="complete", message=f"Analysis ready: {len(rows)} parsed row(s)", step=total + 2, total=total + 2, result_url=f"/results/{transaction_id}?tab=logs")
    except Exception as exc:
        logger.exception("Analysis failed for %s", transaction_id)
        _job_update(transaction_id, status="error", message="Analysis failed", error=str(exc))


@app.get("/results/{transaction_id}", response_class=HTMLResponse)
async def results(request: Request, transaction_id: str):
    require_jwt(request)
    username = request.session.get("username")
    loaded = store.load_collection_meta(transaction_id, owner=None)
    if not loaded:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "Collection not found. It may have expired based on retention settings."}, status_code=404)

    meta = loaded["meta"]
    analysis_ready = (meta.get("analysis_status") == "ready") or int(meta.get("row_count") or 0) > 0

    params = request.query_params
    active_tab = params.get("tab", "logs")
    if not analysis_ready and active_tab in {"logs", "visual"}:
        active_tab = "downloads"
    q = params.get("q", "")
    user = params.get("user", "")
    form_name = params.get("form", "")
    start = params.get("start", "")
    end = params.get("end", "")
    file_name = params.get("file", "")
    level = params.get("level", "")
    ignore_failed = params.get("ignore_failed") in {"1", "true", "on", "yes"}
    flow_tx = params.get("tx", "")
    raw_wf_types = params.get("wf_types", "")
    if raw_wf_types == "none":
        selected_workflow_types = []
    else:
        selected_workflow_types = [t for t in raw_wf_types.split(",") if t in WORKFLOW_TYPE_ORDER]
        if not selected_workflow_types:
            selected_workflow_types = list(WORKFLOW_TYPE_ORDER)
    try:
        limit = min(max(int(params.get("limit", "500")), 50), 5000)
    except ValueError:
        limit = 500
    allowed_columns = [
        "time", "level", "user", "form", "event", "pod_file", "transaction", "workflow", "field", "message"
    ]
    raw_cols = params.get("cols", "time,transaction,message")
    selected_columns = [c for c in raw_cols.split(",") if c in allowed_columns]
    if not selected_columns:
        selected_columns = ["time", "transaction", "message"]

    if analysis_ready:
        facets = store.query_facets(transaction_id, owner=None) or {"users": [], "forms": [], "files": [], "levels": [], "transactions": [], "event_types": []}
        query = store.query_rows(
            transaction_id,
            owner=None,
            q=q,
            user=user,
            form=form_name,
            start=start,
            end=end,
            file=file_name,
            level=level,
            ignore_failed=ignore_failed,
            tx=flow_tx,
            limit=limit,
        ) or {"total": int(meta.get("row_count") or 0), "filtered": 0, "rows": []}
    else:
        facets = {"users": [], "forms": [], "files": [], "levels": [], "transactions": [], "event_types": []}
        query = {"total": 0, "filtered": 0, "rows": []}
    flow = []
    flow_source = []
    visual_user = ""
    mermaid_code = "flowchart TD\n  Hint[Open Log view and click a filter TrID]"
    if active_tab == "visual" and flow_tx:
        # Visual flow is intentionally scoped only by the chosen AR filter TrID.
        # The log-table filters are not applied here, otherwise the workflow can
        # become incomplete or misleading after navigating back from filtered views.
        flow_source = store.query_flow_rows(
            transaction_id,
            owner=None,
            q="",
            user="",
            form="",
            start="",
            end="",
            file="",
            level="",
            ignore_failed=False,
            tx=flow_tx,
            limit=900,
            workflow_only=True,
            filter_log_only=True,
        ) or []
        for item in flow_source:
            candidate = str(item.get("user") or "").strip()
            if candidate:
                visual_user = candidate
                break
        flow = build_flow_from_dicts(flow_source, limit=420)
        mermaid_code = build_mermaid_swimlane(flow, limit=180, hide_not_triggered=ignore_failed)

    return templates.TemplateResponse(
        "results.html",
        {
            **base_context(request),
            "result": loaded["meta"],
            "downloads": loaded["downloads"],
            "rows": query["rows"],
            "total_rows": query["total"],
            "filtered_count": query["filtered"],
            "facets": facets,
            "flow": flow,
            "mermaid_code": mermaid_code,
            "visual_user": visual_user,
            "visual_row_count": len(flow_source),
            "active_tab": active_tab,
            "filters": {"q": q, "user": user, "form": form_name, "start": start, "end": end, "file": file_name, "level": level, "limit": limit, "ignore_failed": ignore_failed, "wf_types": ",".join(selected_workflow_types), "tx": flow_tx},
            "workflow_type_order": WORKFLOW_TYPE_ORDER,
            "workflow_type_labels": WORKFLOW_TYPE_LABELS,
            "selected_workflow_types": selected_workflow_types,
            "allowed_columns": allowed_columns,
            "selected_columns": selected_columns,
            "log_type_options": runtime_config.get().log_types,
            "analysis_ready": analysis_ready,
        },
    )


@app.post("/results/{transaction_id}/upload-extra")
async def upload_extra_logs(
    transaction_id: str,
    request: Request,
    files: list[UploadFile] = File(default=[]),
    default_pod: str = Form("uploaded"),
    pasted_log_text: str = Form(""),
    pasted_log_name: str = Form("pasted.log"),
    pasted_log_type: str = Form("auto"),
):
    require_jwt(request)
    username = request.session.get("username", "unknown")
    downloads: dict[str, bytes] = {}
    requests: list[dict] = []
    failures: list[dict] = []
    seen: dict[str, int] = {}

    def unique_name(name: str) -> str:
        safe = name.replace("/", "__").replace("\\", "__").strip() or "uploaded.log"
        count = seen.get(safe, 0) + 1
        seen[safe] = count
        if count == 1:
            return safe
        pp = Path(safe)
        return f"{pp.stem}_{count}{pp.suffix}"

    for upload in files:
        original_name = upload.filename or ""
        try:
            blob = await upload.read()
            # Browsers often submit an empty file part when only pasted text is provided.
            # Treat that as no file, not as a failed upload.
            if not blob:
                if original_name:
                    failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": "Uploaded file was empty"})
                continue
            original_name = original_name or "uploaded.log"
            name = unique_name(original_name)
            downloads[name] = blob
            requests.append({"source": "upload-extra", "original_upload": original_name, "stored_as": name, "pod": default_pod or "uploaded", "bytes": len(blob)})
        except Exception as exc:
            failures.append({"stage": "upload", "pod": default_pod, "filename": original_name, "error": str(exc)})

    if pasted_log_text.strip():
        name = unique_name(pasted_log_name or "pasted.log")
        downloads[name] = pasted_log_text.encode("utf-8", errors="replace")
        requests.append({"source": "paste-extra", "original_upload": pasted_log_name, "stored_as": name, "pod": default_pod or "uploaded", "log_type_hint": pasted_log_type, "bytes": len(downloads[name])})

    if not downloads:
        return templates.TemplateResponse("error.html", {**base_context(request), "message": "No readable uploaded or pasted log content was provided."}, status_code=400)

    # Adding files invalidates any existing index; re-run analysis explicitly afterwards.
    if not store.extend_collection(transaction_id, owner=None, requests=requests, rows=[], downloads=downloads, failures=failures, reset_analysis=True):
        raise HTTPException(status_code=404, detail="Collection not found")
    return RedirectResponse(f"/results/{transaction_id}?tab=downloads", status_code=303)


@app.get("/results/{transaction_id}/download/{name}")
async def download(transaction_id: str, name: str, request: Request):
    require_jwt(request)
    path = store.download_path(transaction_id, name, owner=None)
    if not path:
        raise HTTPException(status_code=404, detail="Download not found")
    return FileResponse(path, media_type="application/zip", filename=name)


@app.get("/results/{transaction_id}/download-all")
async def download_all(transaction_id: str, request: Request):
    require_jwt(request)
    blob = store.build_all_logs_zip(transaction_id, owner=None)
    if not blob:
        raise HTTPException(status_code=404, detail="No downloadable logs found")
    filename = f"hlx-logs-{transaction_id[:12]}.zip"
    return StreamingResponse(io.BytesIO(blob), media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/collections/{transaction_id}/rename")
async def rename_collection(transaction_id: str, request: Request, collection_name: str = Form("")):
    require_jwt(request)
    ok = store.rename_collection(transaction_id, collection_name.strip(), owner=None)
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found")
    return RedirectResponse(f"/results/{transaction_id}", status_code=303)


@app.post("/collections/{transaction_id}/delete")
async def delete_collection(transaction_id: str, request: Request):
    require_jwt(request)
    ok = store.delete_collection(transaction_id, owner=None)
    if not ok:
        raise HTTPException(status_code=404, detail="Collection not found")
    referer = request.headers.get("referer") or "/collections"
    if f"/results/{transaction_id}" in referer:
        referer = "/collections"
    return RedirectResponse(referer, status_code=303)
