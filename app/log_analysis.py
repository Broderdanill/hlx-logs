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
    if re.match(r"^\s*\d+\s*:", msg):
        return "Action"
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
            # Prefer the exact pod+filename request metadata. When the same
            # filename is downloaded from multiple pods, a filename-only lookup
            # would otherwise assign all parsed rows to whichever request was
            # stored last.
            req = by_file.get(f"{pod}__{filename}".lower()) or by_file.get(filename.lower())
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


def build_mermaid_swimlane(events: list[dict], limit: int = 220, hide_not_triggered: bool = False) -> str:
    """Build a readable Mermaid overview for one AR filter TrID.

    BMC's filter log is a linear execution trace: each AR transaction contains
    one or more filter-processing frames, and inside each frame the server checks
    filters in order. A filter can match the Run If branch, run Else actions, or
    simply be checked and skipped. Filter guides and Service actions show up as
    additional checks/frames in the same AR TrID, often repeating the same filter
    series. The diagram therefore groups the trace by filter-processing frame and
    then shows only the meaningful executed workflow blocks in order.
    """
    source = [e for e in events if not _is_noise_event(e)]
    if not source:
        return "flowchart TD\n  empty[\"No workflow events matched the selected filter transaction\"]\n  classDef empty fill:#09243b,stroke:#00b9f6,color:#f7f7f7\n  class empty empty"

    def etype(ev: dict) -> str:
        return (ev.get("type") or ev.get("event_type") or "Log").strip()

    def msg(ev: dict) -> str:
        return (ev.get("message") or "").strip()

    def low(ev: dict) -> str:
        return " ".join(str(ev.get(k) or "") for k in ["type", "workflow", "form", "field_name", "message", "file"]).lower()

    def label_text(value: str, limit_: int = 76) -> str:
        value = compact(value or "", limit_)
        value = value.replace("\\", "/")
        # Keep Mermaid labels boring. Long/smart punctuation is a frequent cause
        # of hard-to-debug bombs in generated diagrams.
        for a, b in {
            '"': "'", "[": "(", "]": ")", "{": "(", "}": ")", "<": "(", ">": ")",
            "|": "/", "`": "'", ";": ",", "→": "->", "•": "-", "\n": " ", "\r": " "
        }.items():
            value = value.replace(a, b)
        value = re.sub(r"\s+", " ", value).strip()
        return value or "-"

    def label_lines(parts: list[str], limit_: int = 76) -> str:
        clean = [label_text(p, limit_) for p in parts if str(p or "").strip()]
        return "<br/>".join(clean) if clean else "-"

    def parse_operation(ev: dict) -> dict:
        text = msg(ev)
        m = re.search(r"(?:Start filter processing|End of filter processing) \(phase (\d+)\).*?Operation - ([A-Z]+) on (.+?)(?: - (\S+))?$", text, re.I)
        if m:
            form = m.group(3).strip()
            return {
                "phase": m.group(1),
                "op": m.group(2).upper(),
                "form": form,
                "record": (m.group(4) or "").strip(),
                "label": f"Phase {m.group(1)} - {m.group(2).upper()} on {form}",
            }
        form = ev.get("form") or "form/request"
        return {"phase": "", "op": "", "form": form, "record": "", "label": compact(text or form, 90)}

    def parse_filter_name(ev: dict) -> str:
        text = msg(ev)
        m = re.search(r"Checking\s+\"([^\"]+)\"", text, re.I)
        if m:
            return m.group(1).strip()
        return (ev.get("workflow") or text.replace("Checking", "").strip(' \"') or "Filter").strip()

    def filter_level(ev: dict) -> str:
        m = re.search(r"<Filter Level\s*:\s*(\d+)\s+Number Of Filters\s*:\s*(\d+)>", msg(ev), re.I)
        if m:
            return f"L{m.group(1)} #{m.group(2)}"
        return ""

    def action_summary(ev: dict) -> str:
        t = etype(ev)
        text = msg(ev)
        lower = text.lower()
        field = ev.get("field_name") or ""

        # Action rows are the most useful if we normalize them into intent.
        if t == "Action" or re.match(r"^\s*\d+\s*:", text):
            cleaned = re.sub(r"^\s*\d+\s*:\s*", "", text).strip()
            if "service on schema" in lower:
                m = re.search(r"Service on schema\s*[-=]*>?\s*\"?([^\"\n]+)\"?", text, re.I)
                return f"Service: {compact(m.group(1).strip(), 64)}" if m else "Service action"
            if "call guide" in lower:
                m = re.search(r"Call Guide\s+\"?([^\"\n]+)\"?", text, re.I)
                return f"Call guide: {compact(m.group(1).strip(), 64)}" if m else "Call guide"
            if "exit guide" in lower:
                return "Exit guide"
            if "set fields" in lower:
                return "Set fields"
            if "push fields" in lower:
                return "Push fields"
            if "notify" in lower:
                return "Notify"
            if "run process" in lower:
                return "Run process"
            return compact(cleaned or text, 80)

        if "call guide" in lower and "return" in lower:
            m = re.search(r"Call Guide\s+\"?([^\"\n]+)\"?", text, re.I)
            return f"Guide returned: {compact(m.group(1).strip(), 64)}" if m else "Guide returned"
        if "perform-action-add-attachment" in lower:
            m = re.search(r"PERFORM-ACTION-ADD-ATTACHMENT\s+\d+\s+\"([^\"]+)\"", text, re.I)
            return f"Add attachment: {compact(m.group(1), 64)}" if m else "Add attachment"
        if t in {"Set Field", "Set Fields"}:
            m = re.search(r"([^=\n]{1,90})\s*=\s*(.{0,180})", text)
            if m:
                return f"Set {compact(m.group(1).strip(), 38)} = {compact(m.group(2).strip(), 46)}"
            return f"Set {compact(field or text, 68)}"
        if lower.startswith("exit code"):
            return compact(text.replace("Exit code", "Output"), 84)
        if t in {"Push Fields", "Open Window", "Run Process", "Guide Call", "Guide Return", "Service Action", "BackChannel Request", "BackChannel Response", "Change Field", "Refresh Field"}:
            return compact(text or t, 84)
        if t == "Error" or ev.get("level") in {"ERROR", "FATAL", "SEVERE"}:
            return f"Error: {compact(text, 78)}"
        if t == "Filter":
            if lower.startswith("exit code"):
                return compact(text.replace("Exit code", "Output"), 84)
            # Detail lines after Set Fields often contain the field name before
            # the next line reports the value/exit code. Keep them short.
            return f"Detail: {compact(text, 72)}"
        return compact(text or t, 84)

    def action_priority(line: str) -> int:
        l = line.lower()
        if any(x in l for x in ["error", "service", "call guide", "guide returned", "add attachment", "push fields"]):
            return 0
        if any(x in l for x in ["set ", "output", "run process", "notify"]):
            return 1
        return 2

    frames: list[dict] = []
    current_frame: dict | None = None
    current_filter: dict | None = None

    def new_frame(meta: dict, ev: dict) -> dict:
        return {
            "meta": meta,
            "user": ev.get("user") or "",
            "time": ev.get("time") or "",
            "filters": [],
            "skipped": [],
            "raw_count": 0,
        }

    def flush_filter():
        nonlocal current_filter, current_frame
        if not current_filter:
            return
        # Deduplicate action lines but keep usefulness/order.
        seen: set[str] = set()
        actions: list[str] = []
        for item in current_filter.get("actions", []):
            key = item.strip().lower()
            if not key or key in seen:
                continue
            seen.add(key)
            actions.append(item)
        actions.sort(key=action_priority)
        current_filter["actions"] = actions[:4]
        extra = max(0, len(actions) - 4)
        if extra:
            current_filter["more"] = extra
        status = current_filter.get("status") or "unknown"
        if current_frame is None:
            current_frame = new_frame({"phase": "", "op": "", "form": "", "record": "", "label": "Workflow outside explicit phase"}, {})
        if status == "skipped":
            current_frame["skipped"].append(current_filter.get("name") or "Filter")
            if not hide_not_triggered:
                current_frame["filters"].append(current_filter)
        else:
            current_frame["filters"].append(current_filter)
        current_filter = None

    def flush_frame():
        nonlocal current_frame
        flush_filter()
        if current_frame:
            # When not-triggered checks are hidden, don't draw frames that only
            # contain skipped filters; summarize them at the end instead.
            if current_frame["filters"] or (current_frame["skipped"] and not hide_not_triggered):
                frames.append(current_frame)
        current_frame = None

    for ev in source:
        t = etype(ev)
        text_low = low(ev)
        if t == "Transaction Marker":
            continue
        if t == "Filter Phase Start":
            flush_frame()
            current_frame = new_frame(parse_operation(ev), ev)
            continue
        if t == "Filter Phase End":
            flush_frame()
            continue
        if current_frame:
            current_frame["raw_count"] += 1

        if t == "Filter Check":
            flush_filter()
            if current_frame is None:
                current_frame = new_frame({"phase": "", "op": "", "form": ev.get("form") or "", "record": "", "label": "Workflow outside explicit phase"}, ev)
            current_filter = {
                "name": parse_filter_name(ev),
                "level": filter_level(ev),
                "status": "unknown",
                "actions": [],
                "line": ev.get("line") or "",
            }
            continue
        if t == "Qualification" and current_filter:
            # AR logs distinguish plain failed qualifications from failed checks
            # where Else actions are actually performed.
            if "failed" in text_low and "perform else actions" in text_low:
                current_filter["status"] = "else"
            elif "failed" in text_low:
                current_filter["status"] = "skipped"
            elif "passed" in text_low:
                current_filter["status"] = "if"
            continue
        if t in {"Action", "Set Fields", "Set Field", "Change Field", "Refresh Field", "Field Action", "Push Fields", "Open Window", "Run Process", "Guide Call", "Guide Return", "Service Action", "BackChannel Request", "BackChannel Response", "Error", "Filter"} or ev.get("level") in {"ERROR", "FATAL", "SEVERE"}:
            if current_filter:
                current_filter.setdefault("actions", []).append(action_summary(ev))
            elif current_frame is not None:
                # Standalone guide returns/output are useful but not worth their own large node.
                current_frame.setdefault("standalone", []).append(action_summary(ev))
            continue

    flush_frame()

    # Prefer frames with real executed filters. If the selected transaction only
    # contains failed checks, keep a few frames so the user can see that nothing ran.
    if not frames:
        return "flowchart TD\n  empty[\"No filter checks found for this AR TrID\"]\n  classDef empty fill:#09243b,stroke:#00b9f6,color:#f7f7f7\n  class empty empty"

    tx = next((e.get("transaction") for e in source if e.get("transaction")), "selected AR TrID")
    user = next((e.get("user") for e in source if e.get("user")), "")
    total_skipped = sum(len(f.get("skipped", [])) for f in frames)
    total_filters = sum(len(f.get("filters", [])) for f in frames)

    # Limit in human terms, not raw log rows. Extra frames are summarized.
    visible_frames = frames[: min(len(frames), 14)]
    hidden_frames = max(0, len(frames) - len(visible_frames))

    out: list[str] = [
        "flowchart TD",
        "  %% AR filter transaction story: grouped by filter-processing frame. Top-to-bottom is execution order.",
        "  classDef start fill:#06192b,stroke:#00b9f6,color:#f7f7f7,stroke-width:2px",
        "  classDef frame fill:#082138,stroke:#165a9b,color:#f7f7f7,stroke-width:1.5px",
        "  classDef ifrun fill:#0b2b49,stroke:#49c449,color:#f7f7f7,stroke-width:1.5px",
        "  classDef elserun fill:#2d2544,stroke:#ffd166,color:#f7f7f7,stroke-width:1.5px",
        "  classDef skipped fill:#261d26,stroke:#8aa0b6,color:#d8e1ea,stroke-dasharray: 4 3",
        "  classDef note fill:#09243b,stroke:#00b9f6,color:#dceafa",
    ]

    def add_node(nid: str, label: list[str], cls: str):
        out.append(f"  {nid}[\"{label_lines(label)}\"]")
        out.append(f"  class {nid} {cls}")

    add_node("start", ["Start", f"AR TrID {tx}", f"User {user}" if user else "", f"Frames {len(frames)} / filters {total_filters}"], "start")
    prev = "start"
    edge_counter = 0
    node_counter = 0

    for frame_index, frame in enumerate(visible_frames, 1):
        meta = frame.get("meta") or {}
        op_label = meta.get("label") or "Filter processing"
        phase = meta.get("phase") or "?"
        form = meta.get("form") or ""
        rec = meta.get("record") or ""
        skipped_count = len(frame.get("skipped", []))
        filters = frame.get("filters", [])

        frame_in = f"f{frame_index}_in"
        frame_out = f"f{frame_index}_out"
        title = label_text(f"{frame_index}. Phase {phase} - {meta.get('op') or 'Operation'}", 54)
        out.append(f"  subgraph frame{frame_index}[\"{title}\"]")
        out.append("    direction TB")
        # Nodes inside subgraph need extra indentation only for readability.
        out.append(f"    {frame_in}[\"{label_lines(['Input', op_label, f'Record {rec}' if rec else ''], 70)}\"]")
        out.append(f"    class {frame_in} frame")
        local_prev = frame_in

        for filt_index, flt in enumerate(filters[:10], 1):
            node_counter += 1
            nid = f"f{frame_index}_n{filt_index}"
            status = flt.get("status") or "unknown"
            cls = "elserun" if status == "else" else ("skipped" if status == "skipped" else "ifrun")
            branch = "ELSE actions" if status == "else" else ("Not triggered" if status == "skipped" else "IF actions")
            actions = flt.get("actions") or []
            lines = [f"{frame_index}.{filt_index} {branch}", flt.get("name") or "Filter"]
            if flt.get("level"):
                lines.append(flt.get("level"))
            if actions:
                lines.append("Output: " + actions[0])
                for action in actions[1:3]:
                    lines.append("+ " + action)
                if flt.get("more"):
                    lines.append(f"+ {flt['more']} more")
            elif status == "skipped":
                lines.append("Output: none")
            else:
                lines.append("Output: no action line logged")
            out.append(f"    {nid}[\"{label_lines(lines, 72)}\"]")
            out.append(f"    class {nid} {cls}")
            edge_counter += 1
            out.append(f"    {local_prev} -->|{edge_counter}| {nid}")
            local_prev = nid

        if len(filters) > 10:
            more_id = f"f{frame_index}_more"
            out.append(f"    {more_id}[\"{label_lines(['More executed filters', f'{len(filters)-10} additional blocks hidden in diagram'], 70)}\"]")
            out.append(f"    class {more_id} note")
            edge_counter += 1
            out.append(f"    {local_prev} -->|{edge_counter}| {more_id}")
            local_prev = more_id

        if skipped_count and hide_not_triggered:
            skip_id = f"f{frame_index}_skip"
            out.append(f"    {skip_id}[\"{label_lines(['Hidden not-triggered checks', str(skipped_count)], 70)}\"]")
            out.append(f"    class {skip_id} note")
            edge_counter += 1
            out.append(f"    {local_prev} -.->|{edge_counter}| {skip_id}")
            local_prev = skip_id

        standalone = frame.get("standalone") or []
        if standalone:
            stand_id = f"f{frame_index}_standalone"
            stand = standalone[:3]
            out.append(f"    {stand_id}[\"{label_lines(['Other output'] + stand, 70)}\"]")
            out.append(f"    class {stand_id} note")
            edge_counter += 1
            out.append(f"    {local_prev} -->|{edge_counter}| {stand_id}")
            local_prev = stand_id

        out.append(f"    {frame_out}[\"{label_lines(['End frame', form], 70)}\"]")
        out.append(f"    class {frame_out} frame")
        edge_counter += 1
        out.append(f"    {local_prev} -->|{edge_counter}| {frame_out}")
        out.append("  end")
        edge_counter += 1
        out.append(f"  {prev} -->|{edge_counter}| {frame_in}")
        prev = frame_out

    if hidden_frames:
        add_node("hidden_frames", ["Additional frames hidden", f"{hidden_frames} more filter-processing frames", "Open Log view for full raw trace"], "note")
        edge_counter += 1
        out.append(f"  {prev} -->|{edge_counter}| hidden_frames")
        prev = "hidden_frames"

    add_node("finish", ["End", f"Skipped checks total {total_skipped}" if total_skipped else "No skipped checks in visible trace"], "note")
    edge_counter += 1
    out.append(f"  {prev} -->|{edge_counter}| finish")
    return "\n".join(out)

def mermaid_flow_label(value: str, limit: int = 110) -> str:
    value = compact(value or "", limit)
    value = value.replace("\\", "/")
    value = value.replace('"', "'")
    value = value.replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")")
    value = value.replace("<", "(").replace(">", ")").replace("|", "/")
    value = value.replace("`", "'").replace(";", ",")
    value = re.sub(r"\s+", " ", value).strip()
    return value or "Workflow"


def mermaid_flow_edge(value: str, limit: int = 46) -> str:
    value = compact(value or "", limit)
    value = value.replace('"', "'").replace("|", "/").replace("<", "(").replace(">", ")")
    value = value.replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")")
    value = value.replace("`", "'").replace(";", ",").replace(":", " -")
    return re.sub(r"\s+", " ", value).strip()

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
