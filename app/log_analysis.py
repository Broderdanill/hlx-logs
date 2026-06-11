from __future__ import annotations

from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import io
import zipfile
import gzip
import re
from typing import Iterable

from .parser import LogLine, parse_log_text

FORM_PATTERNS = [
    re.compile(r"<\s*(?:FORM|Form|SCHEMA|Schema|schema|form)\s*:\s*([^>]+)>", re.I),
    re.compile(r"\b(?:form|schema|schemaName|formName)\s*[=:]\s*['\"]?([A-Za-z0-9_:$ .-]{2,120})", re.I),
    re.compile(r"\b(?:on|for)\s+(?:form|schema)\s+['\"]([^'\"]+)['\"]", re.I),
]
USER_PATTERNS = [
    re.compile(r"<\s*USER\s*:\s*([^>]+)>", re.I),
    re.compile(r"\b(?:USER|User|user|loginName|Login Name)\s*[=:]\s*['\"]?([A-Za-z0-9_.@\\-]{2,120})", re.I),
]
WORKFLOW_PATTERNS = [
    re.compile(r"\b(?:Filter|Active Link|Escalation|Guide|Filter Guide)\s*[:=]?\s*['\"]?([A-Za-z0-9_:$ .\\/-]{2,160})", re.I),
    re.compile(r"<\s*(?:FILTER|FLTR|ACTL|ACTIVE LINK|ESCALATION|GUIDE)\s*:\s*([^>]+)>", re.I),
]
FIELD_PATTERNS = [
    re.compile(r"\b(?:set|setting|updated?)\s+(?:field|field id)?\s*['\"]?([A-Za-z0-9_:$ .-]{1,100})", re.I),
    re.compile(r"<\s*FIELD\s*:\s*([^>]+)>", re.I),
]
TRANSACTION_PATTERNS = [
    re.compile(r"<\s*TrID\s*:\s*([^>]+)>", re.I),
    re.compile(r"\b(?:TrID|TransactionId|transaction id)\s*[=:]\s*['\"]?([A-Za-z0-9_.:-]{2,80})", re.I),
]

ACTIVE_LINK_LINE_RE = re.compile(r"ActiveLink\s+(Start|End):-\s*([^|]+)", re.I)
CLIENT_EVENT_LINE_RE = re.compile(r"EVENT\s+(Start|End):-\s*([^|]+)", re.I)
GUIDE_CALLED_LINE_RE = re.compile(r"Guide Called\s+(.+?)(?:\s+On Server\s+\S+)?$", re.I)
FILTER_CHECK_LINE_RE = re.compile(r"Checking\s+\"([^\"]+)\"", re.I)
FILTER_PHASE_LINE_RE = re.compile(r"(Start|End) of filter processing \(phase (\d+)\).*?Operation - (\w+) on (.+?)(?: - \S+)?$", re.I)
FIELD_FROM_MESSAGE_RE = re.compile(r"(?:For field --|Change Field|Refresh(?: Table Action Called On)? Field)\s+\"?([^\"\n]+?)(?:\(\d+\))?\"?(?:\s|$)", re.I)


def _first(patterns: list[re.Pattern], text: str) -> str:
    for pat in patterns:
        m = pat.search(text)
        if m:
            value = (m.group(1) or "").strip().strip('"\'')
            # Avoid swallowing too much when matching free text.
            value = re.split(r"\s{2,}|[,;]", value)[0].strip()
            if value:
                return value[:160]
    return ""


def enrich_row(row: LogLine) -> LogLine:
    raw = (row.raw or row.message or "")[:3500]
    msg = row.message or raw
    # Backwards-compatible: set dynamic attrs even if older JSON objects lack them.
    form = getattr(row, "form", "") or _first(FORM_PATTERNS, raw)
    user = row.user or _first(USER_PATTERNS, raw)
    workflow = getattr(row, "workflow", "") or _first(WORKFLOW_PATTERNS, raw)
    field_name = getattr(row, "field_name", "") or _first(FIELD_PATTERNS, raw)
    transaction = row.transaction or _first(TRANSACTION_PATTERNS, raw)
    event_type = getattr(row, "event_type", "") or ""

    # Client-side Active Link logs expose the most useful workflow info in plain text.
    if not workflow:
        for pat in (ACTIVE_LINK_LINE_RE, CLIENT_EVENT_LINE_RE, GUIDE_CALLED_LINE_RE, FILTER_CHECK_LINE_RE):
            m = pat.search(msg)
            if m:
                workflow = m.group(2 if pat in (ACTIVE_LINK_LINE_RE, CLIENT_EVENT_LINE_RE) else 1).strip()
                break
    if not field_name:
        m = FIELD_FROM_MESSAGE_RE.search(msg)
        if m:
            field_name = m.group(1).strip().strip('"')
    if not form:
        # Common client context: ActiveLink ... | Form/View - timestamp or EVENT ... | field | Form/View timestamp
        for pat in [r"\|\s*([^|\n]+?)/[^|\n]+\s*-?\s*\d{4}-", r"\|\s*[^|]*\|\s*([^|\n]+?)/[^|\n]+\s+\d{4}-"]:
            m = re.search(pat, msg)
            if m:
                form = m.group(1).strip()
                break

    if not event_type:
        event_type = infer_event_type(row, raw)
    row.form = form
    row.user = user
    row.workflow = workflow
    row.field_name = field_name
    row.transaction = transaction
    row.event_type = event_type
    if form and "form" not in row.tags:
        row.tags.append("form")
    if workflow and "workflow" not in row.tags:
        row.tags.append("workflow")
    return row


def infer_event_type(row: LogLine, raw: str) -> str:
    raw = (raw or "")[:3500]
    msg = row.message or raw
    lower = f"{row.filename} {msg}".lower()
    et = (getattr(row, "event_type", "") or "").strip()
    if et:
        return et
    if msg.strip().startswith(">>>>"):
        return "Transaction Marker"
    if CLIENT_EVENT_LINE_RE.search(msg):
        m = CLIENT_EVENT_LINE_RE.search(msg)
        return f"Event {m.group(1).title()}"
    if ACTIVE_LINK_LINE_RE.search(msg):
        m = ACTIVE_LINK_LINE_RE.search(msg)
        return f"Active Link {m.group(1).title()}"
    if re.fullmatch(r"\s*(True|False) actions:\s*", msg, re.I):
        return "Branch"
    if re.fullmatch(r"\s*action\s+\d+\s*", msg, re.I):
        return "Action"
    if "serviceaction" == msg.strip().lower():
        return "Service Action"
    if "backchannel request" in lower:
        return "BackChannel Request"
    if "backchannel response" in lower:
        return "BackChannel Response"
    if GUIDE_CALLED_LINE_RE.search(msg):
        return "Guide Call"
    if "exiting guide" in lower:
        return "Guide Return"
    if "setfields" == msg.strip().lower():
        return "Set Fields"
    if re.search(r"^\s*[A-Za-z0-9_:$ .-]+\(\d+\)\s*=", msg):
        return "Set Field"
    if "push fields" in lower or "push field" in lower:
        return "Push Fields"
    if "open query window" in lower or "open window" in lower:
        return "Open Window"
    if "call guide" in lower or "guide called" in lower:
        return "Guide Call"
    if "run process" in lower or "aractprocess" in lower or "command:" in lower:
        return "Run Process"
    if "change field" in lower:
        return "Change Field"
    if "refresh table action" in lower or "refresh field" in lower:
        return "Refresh Field"
    if FILTER_PHASE_LINE_RE.search(msg):
        return "Filter Phase"
    if FILTER_CHECK_LINE_RE.search(msg):
        return "Filter Check"
    if "qualification" in lower and ("failed" in lower or "passed" in lower):
        return "Qualification"
    if msg.strip().lower().startswith("error -") or "default_message" in lower:
        return "Error"
    if "filter guide" in lower:
        return "Filter Guide"
    if "filter" in lower or "<fltr" in lower:
        return "Filter"
    if "escalation" in lower or "aresc" in lower:
        return "Escalation"
    if "sql" in lower or re.search(r"\b(select|update|insert|delete)\b", lower):
        return "SQL"
    if "<api" in lower or " api[" in lower or row.operation.upper() == "API":
        return "API"
    if "plugin" in lower:
        return "Plugin"
    if "exception" in lower or row.level.upper() in {"ERROR", "FATAL", "SEVERE"}:
        return "Error"
    return row.operation or "Log"

def iter_text_payloads(name: str, blob: bytes, depth: int = 0):
    """Yield (name, decoded_text) from raw logs, zip archives and gz files."""
    if depth > 3 or not blob:
        return
    lower = name.lower()
    if lower.endswith(".gz") and not zipfile.is_zipfile(io.BytesIO(blob)):
        try:
            text = gzip.decompress(blob).decode("utf-8", errors="replace")
            yield (name[:-3] or "payload.log", text)
            return
        except Exception:
            pass
    if zipfile.is_zipfile(io.BytesIO(blob)):
        try:
            with zipfile.ZipFile(io.BytesIO(blob)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    child_name = info.filename
                    child_blob = zf.read(info)
                    yield from iter_text_payloads(child_name, child_blob, depth + 1)
            return
        except Exception:
            pass
    yield (name, blob.decode("utf-8", errors="replace"))


def rows_from_downloads(downloads: dict[str, bytes], requests: list[dict] | None = None, *, default_pod: str = "unknown") -> list[LogLine]:
    rows: list[LogLine] = []
    requests = requests or []
    by_file: dict[str, dict] = {}
    for req in requests:
        log_type = req.get("log_type") if isinstance(req.get("log_type"), dict) else {}
        pod_data = req.get("pod") if isinstance(req.get("pod"), dict) else {}
        filename = log_type.get("filename") or req.get("filename") or req.get("original_upload") or ""
        if filename:
            by_file[filename.lower()] = req
            if pod_data.get("id"):
                by_file[f"{pod_data['id']}__{filename}".lower()] = req

    for stored_name, blob in downloads.items():
        for inner_name, text in iter_text_payloads(stored_name, blob):
            filename = Path(inner_name).name or Path(stored_name).name
            pod = default_pod
            # App fetch downloads use pod__filename__entry.zip.
            if "__" in stored_name:
                parts = stored_name.split("__")
                if len(parts) >= 2:
                    pod = parts[0] or pod
                    filename = parts[1] or filename
            req = by_file.get(filename.lower()) or by_file.get(f"{pod}__{filename}".lower())
            if req:
                pod_data = req.get("pod") if isinstance(req.get("pod"), dict) else {}
                log_type = req.get("log_type") if isinstance(req.get("log_type"), dict) else {}
                pod = pod_data.get("id") or req.get("pod") or pod
                filename = log_type.get("filename") or req.get("filename") or filename
            parsed = parse_log_text(text, str(pod), filename, inner_name)
            for row in parsed:
                enrich_row(row)
            rows.extend(parsed)
    rows.sort(key=lambda r: (r.sort_ts, r.pod, r.filename, r.line_number))
    return rows


def filter_rows(rows: Iterable[LogLine], *, q: str = "", user: str = "", form: str = "", start: str = "", end: str = "", file: str = "", level: str = "") -> list[LogLine]:
    ql = q.strip().lower()
    ul = user.strip().lower()
    fl = form.strip().lower()
    file_l = file.strip().lower()
    level_l = level.strip().upper()
    start_dt = _parse_dt_input(start)
    end_dt = _parse_dt_input(end)
    out: list[LogLine] = []
    for row in rows:
        enrich_row(row)
        if ql and ql not in (row.raw or row.message).lower():
            continue
        if ul and ul not in (row.user or "").lower():
            continue
        if fl and fl != (getattr(row, "form", "") or "").lower():
            continue
        if file_l and file_l != row.filename.lower():
            continue
        if level_l and level_l != (row.level or "").upper():
            continue
        if start_dt and row.sort_ts != datetime.max.replace(tzinfo=timezone.utc) and row.sort_ts < start_dt:
            continue
        if end_dt and row.sort_ts != datetime.max.replace(tzinfo=timezone.utc) and row.sort_ts > end_dt:
            continue
        out.append(row)
    return out


def _parse_dt_input(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        # datetime-local submits YYYY-MM-DDTHH:MM.
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def summarize_facets(rows: Iterable[LogLine]) -> dict:
    users, forms, files, levels, transactions = Counter(), Counter(), Counter(), Counter(), Counter()
    event_types = Counter()
    for row in rows:
        enrich_row(row)
        if row.user:
            users[row.user] += 1
        form = getattr(row, "form", "")
        if form:
            forms[form] += 1
        if row.filename:
            files[row.filename] += 1
        if row.level:
            levels[row.level] += 1
        tx = row.transaction or row.tid or row.rpc_id
        if tx:
            transactions[tx] += 1
        event_types[getattr(row, "event_type", "Log") or "Log"] += 1
    return {
        "users": users.most_common(200),
        "forms": forms.most_common(300),
        "files": files.most_common(300),
        "levels": levels.most_common(20),
        "transactions": transactions.most_common(200),
        "event_types": event_types.most_common(50),
    }


def build_flow(rows: Iterable[LogLine], limit: int = 200) -> list[dict]:
    events: list[dict] = []
    for row in rows:
        enrich_row(row)
        event_type = getattr(row, "event_type", "Log") or "Log"
        if event_type == "Log" and not (row.transaction or row.user or getattr(row, "form", "") or row.level in {"ERROR", "WARN"}):
            continue
        events.append({
            "time": row.display_ts or ("" if row.sort_ts.year == 9999 else row.sort_ts.isoformat()),
            "type": event_type,
            "pod": row.pod,
            "file": row.filename,
            "level": row.level,
            "user": row.user,
            "form": getattr(row, "form", ""),
            "workflow": getattr(row, "workflow", ""),
            "field_name": getattr(row, "field_name", ""),
            "transaction": row.transaction or row.tid or row.rpc_id,
            "message": compact(row.message, 260),
            "line": row.line_number,
            "tags": row.tags[:6],
        })
    return events[:limit]



WORKFLOW_TYPE_LABELS = {
    "client": "Client/Form",
    "active_link": "Active Link",
    "guide": "Guide",
    "filter": "Filter",
    "escalation": "Escalation",
    "service": "Service/BackChannel",
    "error": "Error/Warning",
}

WORKFLOW_TYPE_ORDER = ["client", "active_link", "guide", "filter", "escalation", "service", "error"]


def workflow_event_kind(ev: dict) -> str:
    """Classify a parsed workflow event into the visual legend/filter types."""
    event_type = (ev.get("type") or ev.get("event_type") or ev.get("operation") or "").lower()
    msg = " ".join(str(ev.get(k) or "") for k in ["message", "workflow", "form", "file", "filename"]).lower()
    filename = (ev.get("file") or ev.get("filename") or "").lower()
    level = (ev.get("level") or "").upper()
    if level in {"ERROR", "FATAL", "SEVERE"} or "error" in event_type or "exception" in msg or "arerr" in msg or "default_message" in msg:
        return "error"
    if "backchannel" in event_type or "service action" in event_type or "serviceaction" in msg or "web service" in msg:
        return "service"
    if "guide" in event_type or "guide" in msg:
        return "guide"
    if "active link" in event_type or "activelink" in event_type or "active link" in msg or "activelink" in msg or "active" in filename or "progressive" in filename:
        return "active_link"
    if "filter" in event_type or filename.startswith("arfilter") or "filter:" in msg:
        return "filter"
    if "escalation" in event_type or filename.startswith("aresc") or "escalation" in msg:
        return "escalation"
    if event_type.startswith("event") or "window loaded" in msg or "window open" in msg or "menu choice" in msg or "button/menu" in msg:
        return "client"
    # Field-level operations normally belong to client-side workflow unless the source file says otherwise.
    if any(x in event_type for x in ["set field", "change field", "refresh field", "action", "branch", "qualification"]):
        if filename.startswith("arfilter"):
            return "filter"
        if filename.startswith("aresc"):
            return "escalation"
        return "active_link"
    return "client"


def compact(value: str, limit: int = 220) -> str:
    value = re.sub(r"\s+", " ", value or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def row_get(row, key: str, default=""):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def build_flow_from_dicts(rows: Iterable[dict], limit: int = 200) -> list[dict]:
    events: list[dict] = []
    for row in rows:
        event_type = row_get(row, "event_type") or row_get(row, "operation") or "Log"
        if event_type == "Log" and not (row_get(row, "transaction") or row_get(row, "user") or row_get(row, "form") or row_get(row, "level") in {"ERROR", "WARN"}):
            continue
        events.append({
            "time": row_get(row, "display_ts") or row_get(row, "sort_ts_text"),
            "type": event_type,
            "pod": row_get(row, "pod"),
            "file": row_get(row, "filename"),
            "level": row_get(row, "level"),
            "user": row_get(row, "user"),
            "form": row_get(row, "form"),
            "workflow": row_get(row, "workflow"),
            "field_name": row_get(row, "field_name"),
            "transaction": row_get(row, "transaction") or row_get(row, "tid") or row_get(row, "rpc_id"),
            "message": compact(row_get(row, "message"), 260),
            "line": row_get(row, "line_number"),
            "tags": (row_get(row, "tags") or [])[:6],
        })
    return events[:limit]


def mermaid_escape(value: str, limit: int = 80) -> str:
    value = compact(value or "", limit)
    return value.replace('"', "'").replace("[", "(").replace("]", ")").replace("\n", " ")


def build_mermaid_swimlane(events: list[dict], limit: int = 180) -> str:
    """Build an AR workflow trace diagram similar to Remedy log trace views.

    The diagram is a sequence diagram where each lane is a real workflow object
    or target form when possible. It understands common Mid Tier / Progressive
    active-link traces and server-side arfilter traces: event start/end, active
    link start/end, guide calls/returns, set/change/refresh field, service
    action, backchannel, filter phase/check/qualification and errors.
    """
    source = [e for e in events if not _is_noise_event(e)][:limit]
    if not source:
        return "sequenceDiagram\n  participant SYS as System\n  Note over SYS: No workflow events matched the current filters"

    def etype(ev: dict) -> str:
        return (ev.get("type") or ev.get("event_type") or "Log").strip()

    def text(ev: dict) -> str:
        return " ".join(str(ev.get(k) or "") for k in ["type", "workflow", "form", "field_name", "message", "file"]).strip()

    def kind(ev: dict) -> str:
        t = etype(ev).lower()
        msg = text(ev).lower()
        fn = (ev.get("file") or "").lower()
        lvl = (ev.get("level") or "").upper()
        if t.startswith("event"):
            return "client"
        if "active link" in t or "active" in fn or "progressive" in fn:
            return "active_link"
        if "guide" in t or "guide" in msg:
            return "guide"
        if "filter" in t or fn.startswith("arfilter"):
            return "filter"
        if "escalation" in t or fn.startswith("aresc"):
            return "escalation"
        if "backchannel" in t or "service action" in t or "serviceaction" in msg:
            return "service"
        if "sql" in t or re.search(r"\b(select|update|insert|delete)\b", msg):
            return "sql"
        if lvl in {"ERROR", "FATAL", "SEVERE"} or "error" in t or "exception" in msg or "arerr" in msg:
            return "error"
        if any(x in t for x in ["set field", "change field", "refresh field", "branch", "action", "qualification"]):
            # These belong to the current workflow lane when possible.
            wf = ev.get("workflow") or ""
            if wf and not wf.lower().startswith(("true actions", "false actions")):
                if "filter" in fn or fn.startswith("arfilter"):
                    return "filter"
                return "active_link"
        if ev.get("form"):
            return "data"
        return "system"

    def label(ev: dict, k: str) -> str:
        wf = (ev.get("workflow") or "").strip()
        form = (ev.get("form") or "").strip()
        field = (ev.get("field_name") or "").strip()
        msg = ev.get("message") or ""
        t = etype(ev)
        if k == "client":
            return form or "Client / User"
        if k in {"active_link", "filter", "guide", "escalation"}:
            if wf and wf.lower() not in {"true actions", "false actions", "action"}:
                return wf
        if k == "service":
            return form or "Service / BackChannel"
        if k == "data":
            return form or "Form / Data"
        if k == "sql":
            return "Database / SQL"
        if k == "error":
            return "Errors / Warnings"
        if field:
            return field
        # Target form lines from client logs.
        m = re.search(r"Schema:\s*(.+)$", msg, re.I)
        if m:
            return m.group(1).strip()
        return wf or form or t or "System"

    def aid_for(label_value: str, k: str) -> str:
        base = re.sub(r"[^A-Za-z0-9]+", "_", f"{k}_{label_value}").strip("_").lower()[:48]
        return base or k

    actors: dict[str, dict] = {}
    normalized: list[tuple[dict, str, str]] = []
    current_by_tx: dict[str, str] = {}
    current_global = ""

    for ev in source:
        k = kind(ev)
        tx = ev.get("transaction") or "_global"
        t = etype(ev)
        # Branch/action/field/qualification rows are rendered on current workflow lane.
        if t in {"Branch", "Action", "Set Fields", "Set Field", "Change Field", "Refresh Field", "Qualification"} and current_by_tx.get(tx):
            aid = current_by_tx[tx]
            k = actors.get(aid, {}).get("kind", k)
        else:
            lab = clean_mermaid_label(label(ev, k), 66)
            aid = aid_for(lab, k)
            actors.setdefault(aid, {"label": lab, "kind": k})
        if k in {"active_link", "filter", "guide", "escalation", "client"} and t not in {"Branch", "Action"}:
            current_by_tx[tx] = aid
            current_global = aid
        elif not current_by_tx.get(tx) and current_global:
            current_by_tx[tx] = current_global
        normalized.append((ev, aid, k))

    preferred = {"client":0,"escalation":1,"active_link":2,"guide":3,"filter":4,"service":5,"data":6,"sql":7,"error":8,"system":9}
    ordered_actor_ids = sorted(actors, key=lambda a: (preferred.get(actors[a]["kind"], 99), list(actors).index(a)))
    visible_ids = ordered_actor_ids[:22]
    visible = set(visible_ids)
    if len(ordered_actor_ids) > len(visible_ids):
        actors.setdefault("system_other", {"label": "Other workflow", "kind": "system"})
        visible.add("system_other")
        visible_ids.append("system_other")

    lines = ["sequenceDiagram", "  autonumber"]
    box_info = {
        "client": ("rgba(0,185,246,0.12)", "Client / Form"),
        "active_link": ("rgba(73,196,73,0.14)", "Active Link"),
        "guide": ("rgba(255,204,51,0.13)", "Guide"),
        "filter": ("rgba(110,168,255,0.14)", "Filter"),
        "escalation": ("rgba(201,122,255,0.14)", "Escalation"),
        "service": ("rgba(255,159,67,0.14)", "Service / BackChannel"),
        "data": ("rgba(0,185,246,0.08)", "Form / Data"),
        "sql": ("rgba(255,204,51,0.10)", "SQL"),
        "error": ("rgba(171,13,2,0.18)", "Errors / Warnings"),
        "system": ("rgba(145,162,188,0.10)", "System"),
    }
    current_box = None
    for aid in visible_ids:
        akind = actors[aid].get("kind", "system")
        if akind != current_box:
            if current_box is not None:
                lines.append("  end")
            color, title = box_info.get(akind, box_info["system"])
            lines.append(f"  box {color} {mermaid_sequence_escape(title, 40)}")
            current_box = akind
        lines.append(f"  participant {aid} as {mermaid_sequence_escape(actors[aid]['label'], 76)}")
    if current_box is not None:
        lines.append("  end")

    last_by_tx: dict[str, str] = {}
    for ev, aid, k in normalized:
        if aid not in visible:
            aid = "system_other"
        tx = ev.get("transaction") or "_global"
        prev = last_by_tx.get(tx)
        t = etype(ev)
        msg = ev.get("message") or ""
        lbl = event_label(ev)
        if not prev:
            lines.append(f"  Note over {aid}: {mermaid_sequence_escape(lbl, 150)}")
        elif t.endswith("End") or t in {"Guide Return"}:
            lines.append(f"  {aid}-->>{prev}: {mermaid_sequence_escape(lbl, 120)}")
        elif t in {"Branch", "Action", "Set Fields", "Set Field", "Change Field", "Refresh Field", "Qualification"} or prev == aid:
            arrow = "-->>" if t == "Qualification" and "failed" in msg.lower() else "->>"
            lines.append(f"  {aid}{arrow}{aid}: {mermaid_sequence_escape(lbl, 130)}")
        else:
            lines.append(f"  {prev}->>{aid}: {mermaid_sequence_escape(lbl, 130)}")
        note = event_note(ev)
        if note:
            lines.append(f"  Note over {aid}: {mermaid_sequence_escape(note, 220)}")
        if not t.endswith("End"):
            last_by_tx[tx] = aid
    return "\n".join(lines)


def _is_noise_event(ev: dict) -> bool:
    t = (ev.get("type") or "").lower()
    msg = (ev.get("message") or "").strip()
    if not msg:
        return True
    if msg.startswith(">>>>"):
        return True
    if t in {"transaction marker", "log"} and not ev.get("workflow"):
        return True
    # Avoid letting failed checks dominate; show them only when search/filter made the set small.
    return False


def clean_mermaid_label(value: str, limit: int = 64) -> str:
    value = re.sub(r"\s+", " ", value or "").strip().strip('"')
    value = value.replace(":", " -")
    return compact(value or "Workflow", limit)


def event_label(ev: dict) -> str:
    t = ev.get("type") or "Log"
    msg = ev.get("message") or ""
    wf = ev.get("workflow") or ""
    field = ev.get("field_name") or ""
    lower = msg.lower()
    if t.startswith("Event"):
        return f"{t.replace('Event ', '')}: {wf or compact(msg, 40)}"
    if t.startswith("Active Link"):
        return f"{t.replace('Active Link ', '')}: {wf}"
    if t == "Guide Call":
        return f"Call guide {wf}"
    if t == "Guide Return":
        return f"Return from guide {wf}"
    if t == "Branch":
        return (ev.get("operation") or compact(msg, 70)).replace(":", "")
    if t == "Action":
        return ev.get("operation") or compact(msg, 70)
    if t in {"Set Fields", "Set Field"}:
        return f"Set {field}" if field else "Set fields"
    if t in {"Change Field", "Refresh Field", "Push Fields", "Run Process", "Service Action", "Notify", "Message", "Call Guide", "Go To", "Exit Guide", "Open Window"}:
        return f"{t}: {field}" if field and t in {"Change Field", "Refresh Field"} else t
    if t == "Change Field":
        return f"Change {field}" if field else "Change field"
    if t == "Refresh Field":
        return f"Refresh {field}" if field else "Refresh field"
    if t == "Service Action":
        return "Service action"
    if t == "BackChannel Request":
        return f"Request {ev.get('form') or 'service'}"
    if t == "BackChannel Response":
        return "Response"
    if t == "Filter Phase Start":
        return f"Start filter phase on {ev.get('form') or 'form'}"
    if t == "Filter Phase End":
        return f"End filter phase on {ev.get('form') or 'form'}"
    if t == "Filter Check":
        return f"Check {wf}"
    if t == "Qualification":
        if "failed" in lower:
            return "Failed qualification"
        if "passed" in lower:
            return "Passed qualification"
        return "Qualification"
    if t == "Error":
        return compact(msg.replace("DEFAULT_MESSAGE |", ""), 110)
    return compact(wf or msg or t, 110)


def event_note(ev: dict) -> str:
    parts = []
    for label_, key in [("time", "time"), ("user", "user"), ("form", "form"), ("tx", "transaction")]:
        val = ev.get(key)
        if val:
            parts.append(f"{label_}: {val}")
    field = ev.get("field_name")
    if field:
        parts.append(f"field: {field}")
    # Try to expose important value flow without showing every raw line.
    raw_msg = ev.get("message") or ""
    m_val = re.search(r"([^=\n]{1,80})\s*=\s*(.{0,140})", raw_msg)
    if m_val:
        left = compact(m_val.group(1).strip(), 80)
        right = compact(m_val.group(2).strip(), 140)
        if right:
            parts.append(f"value: {left} = {right}")
    msg = compact(raw_msg, 140)
    if msg and msg not in parts:
        parts.append(msg)
    return " | ".join(parts[:5])

def mermaid_sequence_escape(value: str, limit: int = 120) -> str:
    value = compact(value or "", limit)
    value = value.replace("\n", " ").replace("\r", " ")
    # Mermaid sequence text is sensitive to colons and semicolons in some renderers.
    value = value.replace(":", " -").replace(";", ",")
    value = value.replace("-->", "→").replace("->", "→")
    value = value.replace("<", "(").replace(">", ")")
    return value
