from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
import io
import json
import logging
import re
import zipfile
from dateutil import parser as dtparser

logger = logging.getLogger(__name__)

ISO_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?[+-]\d{4})")
GENERIC_TS_RE = re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)")
MONITOR_TS_RE = re.compile(r"/\*\s*(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{4}\s+\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\s*\*/")
LEGACY_TS_RE = re.compile(r"(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})")

LEVEL_RE = re.compile(r"\b(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\b", re.IGNORECASE)
ARERR_RE = re.compile(r"\b(?P<code>AR(?:ERR|WARN|NOTE)\s*\[?\d+\]?|ARERR\s+\d+|ARNOTE\s+\d+|ARWARN\s+\d+)\b", re.IGNORECASE)
ANGLE_RE = re.compile(r"<(?P<key>[A-Za-z0-9 _-]+):\s*(?P<val>[^>]*)>")
MONITOR_HEAD_RE = re.compile(r"^<(?P<source>[A-Z]+)>\s*<TNAME:\s*(?P<tname>[^>]*)>\s*<(?P<level>[^>]*)>\s*<(?P<component>[^>]*)>\s*<(?P<location>[^>]*)>")
DURATION_RE = re.compile(r"\b(?P<kind>API|SQL|Filter|Escalation)\[(?P<duration>[0-9.]+)\s*(?P<unit>seconds?|secs?|ms|milliseconds?)?\]", re.IGNORECASE)
JAVA_EXCEPTION_RE = re.compile(r"\b(?P<exception>[A-Za-z0-9_.]+(?:Exception|Error))\b")
AR_OPERATION_RE = re.compile(r"\b(GetListEntry|GetEntry|SetEntry|CreateEntry|DeleteEntry|MergeEntry|ExecuteProcess|ServiceEntry|Query|Login|Logout)\b", re.IGNORECASE)

# Client-side Active Link and workflow log patterns (Mid Tier and Progressive UI)
PIPE_PREFIX_RE = re.compile(r"^(?P<tx>[A-Za-z0-9_.:-]+)\s*\|\s*(?P<body>.*)$")
MIDTIER_TRID_RE = re.compile(r"^>>>>\s*TrID:\s*(?P<tx>[^>]+?)\s*>>>>")
CLIENT_EVENT_RE = re.compile(r"EVENT\s+(?P<phase>Start|End):-\s*(?P<event>[^|]+)\|\s*(?P<field>[^|]*)\|\s*(?P<context>.*?)\s+(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?[+-]\d{4})\s*$", re.I)
AL_START_RE = re.compile(r"ActiveLink\s+(?P<phase>Start|End):-\s*(?P<name>[^|]+)\|\s*(?P<context>.*?)(?:\|\s*\d+\s*-|\s*-)?\s*(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?[+-]\d{4})\s*$", re.I)
ACTION_RE = re.compile(r"^\s*action\s+(?P<num>\d+)\s*$", re.I)
FOR_FIELD_RE = re.compile(r"For field --\s*(?P<field>.+?)(?:\(\d+\))?\s*$", re.I)
CHANGE_FIELD_RE = re.compile(r"Change Field\s+\"(?P<field>[^\"]+)\"\s+(?P<change>.+)$", re.I)
REFRESH_FIELD_RE = re.compile(r"Refresh(?: Table Action Called On)? Field\s+\"(?P<field>[^\"]+)\"", re.I)
GUIDE_CALLED_RE = re.compile(r"Guide Called\s+(?P<guide>.+?)(?:\s+On Server\s+(?P<server>\S+))?\s*$", re.I)
EXIT_GUIDE_RE = re.compile(r"Exiting Guide\s*:\s*(?P<guide>.+)$", re.I)
OPEN_WINDOW_RE = re.compile(r"Open(?: Query)? Window", re.I)
SERVER_ACTION_RE = re.compile(r"^\s*(?P<num>\d+)\s*:\s*(?P<action>Set Fields|Push Fields|Notify|Run Process|Service Action|Message|SQL|Call Guide|Go To|Exit Guide)", re.I)
SCHEMA_RE = re.compile(r"^\s*Schema:\s*(?P<form>.+?)\s*$", re.I)
BACKCHANNEL_RE = re.compile(r"BackChannel\s+(?P<kind>Request|Response):\s*(?:(?P<ts>\d{4}-\d{2}-\d{2}T[^:]+)\s*:\s*)?(?P<body>.*)$", re.I)
BC_FORM_RE = re.compile(r"(?:sourceForm|form):\s*([^,}\]]+)", re.I)
BC_INVALUES_USER_RE = re.compile(r"inValues:\s*\[([^,\]]+)", re.I)
SETFIELD_ASSIGN_RE = re.compile(r"^\s*(?P<field>[A-Za-z0-9_:$ .\-]+(?:\(\d+\))?)\s*=\s*(?P<value>.*)$")
FILTER_PROCESS_RE = re.compile(r"(?P<kind>Start|End)(?: of)? filter processing \(phase (?P<phase>\d+)\).*?Operation - (?P<op>\w+) on (?P<form>.+?)(?: - (?P<request>\S+))?$", re.I)
FILTER_CHECK_RE = re.compile(r"<Filter Level:(?P<level>\d+) Number Of Filters:(?P<number>\d+)>\s*Checking\s+\"(?P<filter>[^\"]+)\"", re.I)
QUALIFICATION_RE = re.compile(r"--?>\s*(?P<result>Passed|Failed)(?: qualification)?(?: -- perform (?:else )?actions)?", re.I)



@dataclass
class LogLine:
    sort_ts: datetime
    display_ts: str
    level: str
    pod: str
    filename: str
    source_path: str
    line_number: int
    message: str
    raw: str
    ar_code: str = ""
    transaction: str = ""
    tid: str = ""
    rpc_id: str = ""
    queue: str = ""
    user: str = ""
    thread: str = ""
    operation: str = ""
    component: str = ""
    duration_ms: float | None = None
    form: str = ""
    workflow: str = ""
    field_name: str = ""
    event_type: str = ""
    tags: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        data = asdict(self)
        data["sort_ts"] = self.sort_ts.isoformat()
        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def from_dict(data: dict) -> "LogLine":
        data = dict(data)
        ts = data.get("sort_ts")
        if isinstance(ts, str):
            data["sort_ts"] = dtparser.parse(ts)
        return LogLine(**data)


def extract_zip_files(blob: bytes) -> dict[str, str]:
    """Return a mapping of filename -> decoded text. Non-zip payloads become payload.log."""
    if not blob:
        return {}
    if zipfile.is_zipfile(io.BytesIO(blob)):
        result: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for name in zf.namelist():
                if name.endswith("/"):
                    continue
                try:
                    result[name] = zf.read(name).decode("utf-8", errors="replace")
                except Exception:
                    logger.exception("Could not decode %s from zip", name)
        return result
    return {"payload.log": blob.decode("utf-8", errors="replace")}


def _fast_iso_parse(raw: str) -> datetime | None:
    try:
        fixed = raw
        if fixed.endswith("Z"):
            fixed = fixed[:-1] + "+00:00"
        elif re.search(r"[+-]\d{4}$", fixed):
            fixed = fixed[:-5] + fixed[-5:-2] + ":" + fixed[-2:]
        dt = datetime.fromisoformat(fixed)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_ar_ts(raw: str) -> datetime | None:
    # Hot path: AR server logs commonly use 2025-11-20T14:48:11.246+0000.
    if raw and raw[0].isdigit():
        dt = _fast_iso_parse(raw)
        if dt:
            return dt
    # armonitor sample has unusual Mon Mar 30 2026 16:15:21.0696 order.
    m = re.fullmatch(r"([A-Z][a-z]{2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+(\d{2}:\d{2}:\d{2})(?:\.(\d{1,6}))?", raw)
    if m:
        from datetime import datetime as _dt
        frac = (m.group(6) or "0")[:6].ljust(6, "0")
        try:
            dt = _dt.strptime(f"{m.group(2)} {m.group(3)} {m.group(4)} {m.group(5)} {frac}", "%b %d %Y %H:%M:%S %f")
            return dt.replace(tzinfo=timezone.utc)
        except Exception:
            return None
    # Last resort for uncommon formats. Avoid using this unless a timestamp-like
    # prefix was already detected because dateutil is expensive on large logs.
    try:
        fixed = raw
        if re.search(r"[+-]\d{4}$", fixed):
            fixed = fixed[:-5] + fixed[-5:-2] + ":" + fixed[-2:]
        dt = dtparser.parse(fixed, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_timestamp(line: str) -> tuple[datetime, str]:
    if not line:
        return datetime.max.replace(tzinfo=timezone.utc), ""
    # Fast ISO prefix.
    if len(line) >= 24 and line[0:4].isdigit() and line[4] == "-":
        m = ISO_TS_RE.search(line[:40])
        if m:
            raw = m.group("ts")
            dt = _fast_iso_parse(raw)
            if dt:
                return dt, raw
    # Timestamp inside AR trace prefixes can appear after a long <FLTR>/<API> header.
    m_any_iso = ISO_TS_RE.search(line[:500])
    if m_any_iso:
        raw = m_any_iso.group("ts")
        dt = _fast_iso_parse(raw)
        if dt:
            return dt, raw
    # Timestamp inside /* ... */ is common for API/monitor/plugin logs.
    m = MONITOR_TS_RE.search(line)
    if m:
        raw = m.group("ts")
        dt = _parse_ar_ts(raw)
        if dt:
            return dt, raw
    # Other timestamp-ish patterns.
    for pattern in (GENERIC_TS_RE, LEGACY_TS_RE):
        match = pattern.search(line[:90])
        if not match:
            continue
        raw = match.group("ts")
        dt = _parse_ar_ts(raw)
        if dt:
            return dt, raw
    return datetime.max.replace(tzinfo=timezone.utc), ""


def infer_level(line: str, monitor_level: str = "") -> str:
    if monitor_level.strip():
        level = monitor_level.strip().upper()
        return "WARN" if level == "WARNING" else level
    m = LEVEL_RE.search(line)
    if m:
        level = m.group("level").upper()
        return "WARN" if level == "WARNING" else level
    upper = line.upper()
    lower = line.lower()
    if "ARERR" in upper or " exception" in lower or lower.endswith("exception") or "frameworkevent error" in lower:
        return "ERROR"
    if "ARWARN" in upper or "warning" in lower:
        return "WARN"
    if "ARNOTE" in upper or "success" in lower or "started" in lower:
        return "INFO"
    return ""


def extract_fields(line: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in ANGLE_RE.finditer(line):
        key = m.group("key").strip().lower().replace(" ", "_").replace("-", "_")
        fields[key] = m.group("val").strip()
    head = MONITOR_HEAD_RE.search(line)
    if head:
        fields.setdefault("source", head.group("source").strip())
        fields.setdefault("tname", head.group("tname").strip())
        fields.setdefault("level", head.group("level").strip())
        fields.setdefault("component", head.group("component").strip())
        fields.setdefault("location", head.group("location").strip())
    return fields


def infer_duration_ms(line: str) -> float | None:
    m = DURATION_RE.search(line)
    if not m:
        return None
    value = float(m.group("duration"))
    unit = (m.group("unit") or "seconds").lower()
    if unit.startswith("ms") or unit.startswith("millisecond"):
        return value
    return value * 1000


def infer_tags(line: str, filename: str, fields: dict[str, str]) -> list[str]:
    lower = f"{filename} {line}".lower()
    tags: list[str] = []
    checks = {
        "sql": ["sql", "select ", "update ", "insert ", "delete "],
        "filter": ["filter", "workflow"],
        "api": ["<api", " api[", "client_rpc", "rpc id", "getlist", "getentry", "setentry"],
        "plugin": ["plugin", "java", "arjavaplugin", "pluginsvr", "classnotfoundexception"],
        "startup": ["armonitor", "restart", "started", "stopped", "extension loaded", "executing process", "processmonitor"],
        "exception": ["exception", "stacktrace", "caused by", "frameworkevent error", "classnotfoundexception", "illegalstateexception"],
        "performance": ["elapsed time", "threshold", "seconds]", " api[", "duration", "exceeded"],
        "auth": ["login", "authentication", "token", "auth-token"],
        "fts": ["fts", "full text", "elasticsearch", "index queue"],
        "server-group": ["server group", "arhgroup", "ranking", "operation ranking"],
    }
    for tag, needles in checks.items():
        if any(n in lower for n in needles):
            tags.append(tag)
    if fields.get("trid"):
        tags.append("transaction")
    return tags


def clean_message(line: str) -> str:
    # Remove noisy AR trace prefixes and leading timestamps, but keep action text.
    line = re.sub(r"^<[^>]+>\s*(?:<[^>]+>\s*)*?/\*\s*" + ISO_TS_RE.pattern.replace("(?P<ts>", "(") + r"\s*\*/\s*", "", line, count=1)
    line = ISO_TS_RE.sub("", line, count=1).strip()
    return line or ""



def _context_form(context: str) -> str:
    context = (context or "").strip()
    if not context:
        return ""
    # Context is normally Form/View. Keep only the form/schema part for facets.
    return context.split("/")[0].strip()


def _strip_client_prefix(raw: str) -> tuple[str, str]:
    """Return (transaction, body) for progressive or midtier active link logs."""
    m = PIPE_PREFIX_RE.match(raw)
    if m:
        return m.group("tx").strip(), m.group("body")
    m = MIDTIER_TRID_RE.match(raw)
    if m:
        return m.group("tx").strip(), raw
    return "", raw


def _client_workflow_fields(body: str, filename: str) -> dict[str, str]:
    """Classify Mid Tier / Progressive active link trace rows."""
    out = {"event_type": "", "workflow": "", "form": "", "field_name": "", "user": "", "operation": ""}
    line = body.strip("\n")
    compact_line = line.strip()
    m = CLIENT_EVENT_RE.search(compact_line)
    if m:
        out["event_type"] = f"Event {m.group('phase').title()}"
        out["workflow"] = m.group("event").strip()
        out["form"] = _context_form(m.group("context"))
        field = m.group("field").strip()
        if field and field != "()":
            out["field_name"] = field.strip()
        out["operation"] = "Client Event"
        return out
    m = AL_START_RE.search(compact_line)
    if m:
        out["event_type"] = f"Active Link {m.group('phase').title()}"
        out["workflow"] = m.group("name").strip()
        out["form"] = _context_form(m.group("context"))
        out["operation"] = "Active Link"
        return out
    if re.fullmatch(r"True actions:|False actions:", compact_line, re.I):
        out["event_type"] = "Branch"
        out["operation"] = compact_line.rstrip(":")
        return out
    m = ACTION_RE.match(compact_line)
    if m:
        out["event_type"] = "Action"
        out["operation"] = f"action {m.group('num')}"
        return out
    if compact_line == "SetFields:":
        out["event_type"] = "Set Fields"
        out["operation"] = "Set Fields"
        return out
    m = SERVER_ACTION_RE.search(compact_line)
    if m:
        action = m.group("action").title()
        out["event_type"] = action
        out["operation"] = action
        return out
    m = SETFIELD_ASSIGN_RE.match(line)
    if m and ("=" in line) and not compact_line.startswith("<"):
        out["event_type"] = "Set Field"
        out["field_name"] = m.group("field").strip()
        out["operation"] = "Set Field"
        return out
    m = FOR_FIELD_RE.search(compact_line)
    if m:
        out["event_type"] = "Field Action"
        out["field_name"] = m.group("field").strip()
        out["operation"] = "Field Action"
        return out
    m = CHANGE_FIELD_RE.search(compact_line)
    if m:
        out["event_type"] = "Change Field"
        out["field_name"] = m.group("field").strip()
        out["operation"] = "Change Field"
        return out
    m = REFRESH_FIELD_RE.search(compact_line)
    if m:
        out["event_type"] = "Refresh Field"
        out["field_name"] = m.group("field").strip()
        out["operation"] = "Refresh Field"
        return out
    m = GUIDE_CALLED_RE.search(compact_line)
    if m:
        out["event_type"] = "Guide Call"
        out["workflow"] = m.group("guide").strip()
        out["operation"] = "Guide"
        return out
    m = EXIT_GUIDE_RE.search(compact_line)
    if m:
        out["event_type"] = "Guide Return"
        out["workflow"] = m.group("guide").strip()
        out["operation"] = "Guide"
        return out
    if OPEN_WINDOW_RE.search(compact_line):
        out["event_type"] = "Open Window"
        out["operation"] = "Open Window"
        return out
    m = SCHEMA_RE.search(compact_line)
    if m:
        out["event_type"] = "Target Form"
        out["form"] = m.group("form").strip()
        out["operation"] = "Form"
        return out
    if compact_line == "ServiceAction":
        out["event_type"] = "Service Action"
        out["operation"] = "Service Action"
        return out
    m = BACKCHANNEL_RE.search(compact_line)
    if m:
        out["event_type"] = f"BackChannel {m.group('kind').title()}"
        out["operation"] = "BackChannel"
        body = m.group("body") or ""
        fm = BC_FORM_RE.search(body)
        if fm:
            out["form"] = fm.group(1).strip()
        um = BC_INVALUES_USER_RE.search(body)
        if um:
            out["user"] = um.group(1).strip()
        return out
    if compact_line.startswith("DEFAULT_MESSAGE") or compact_line.startswith("Error -"):
        out["event_type"] = "Error"
        out["operation"] = "Error"
        return out
    m = FILTER_PROCESS_RE.search(compact_line)
    if m:
        out["event_type"] = "Filter Phase Start" if m.group("kind").lower() == "start" else "Filter Phase End"
        out["form"] = m.group("form").strip()
        out["workflow"] = "Filter processing"
        out["operation"] = m.group("op").strip().upper()
        return out
    m = FILTER_CHECK_RE.search(compact_line)
    if m:
        out["event_type"] = "Filter Check"
        out["workflow"] = m.group("filter").strip()
        out["operation"] = "Filter"
        return out
    m = QUALIFICATION_RE.search(compact_line)
    if m:
        out["event_type"] = "Qualification"
        out["operation"] = f"{m.group('result').title()} qualification"
        return out
    return out

def parse_log_text(text: str, pod: str, filename: str, source_path: str) -> list[LogLine]:
    rows: list[LogLine] = []
    current: LogLine | None = None
    current_client_tx = ""
    current_form = ""
    current_workflow = ""
    current_field = ""
    for idx, raw in enumerate(text.splitlines(), start=1):
        client_tx, body = _strip_client_prefix(raw)
        if client_tx:
            current_client_tx = client_tx
        ts, display = parse_timestamp(body or raw)
        fields = extract_fields(raw)
        client_meta = _client_workflow_fields(body or raw, filename)
        if client_meta.get("form"):
            current_form = client_meta["form"]
        if client_meta.get("workflow") and client_meta.get("event_type") not in {"Action", "Branch", "Qualification"}:
            current_workflow = client_meta["workflow"]
        if client_meta.get("field_name"):
            current_field = client_meta["field_name"]
        monitor_level = fields.get("level", "") if filename == "armonitor.log" or raw.startswith("<MNTR>") else ""
        level = infer_level(raw, monitor_level)
        code_match = ARERR_RE.search(raw)
        ar_code = code_match.group("code") if code_match else ""
        duration_ms = infer_duration_ms(raw)
        transaction = fields.get("trid", "") or fields.get("transaction_id", "") or current_client_tx
        tid = fields.get("tid", "") or fields.get("tname", "")
        rpc_id = fields.get("rpc_id", "")
        queue = fields.get("queue", "")
        user = fields.get("user", "")
        component = fields.get("component", "") or fields.get("source", "")
        thread = fields.get("tname", "") or tid
        operation = ""
        if "<API" in raw or " API[" in raw:
            operation = "API"
        elif "SQL" in raw.upper():
            operation = "SQL"
        elif "Filter" in raw:
            operation = "Filter"
        else:
            op_match = AR_OPERATION_RE.search(raw)
            if op_match:
                operation = op_match.group(1)
        exception_match = JAVA_EXCEPTION_RE.search(raw)
        tags = infer_tags(raw, filename, fields)
        if exception_match and "exception" not in tags:
            tags.append("exception")
        if client_meta.get("event_type"):
            et = client_meta["event_type"].lower()
            if "active link" in et and "active-link" not in tags:
                tags.append("active-link")
            if "guide" in et and "guide" not in tags:
                tags.append("guide")
            if "filter" in et and "filter" not in tags:
                tags.append("filter")
            if "backchannel" in et and "backchannel" not in tags:
                tags.append("backchannel")
            if "field" in et and "field" not in tags:
                tags.append("field")

        # AR logs often wrap Java exceptions and stack traces across lines. Treat
        # no-timestamp rows as continuations when they look like stack trace lines
        # or when they directly follow a timestamped row in known multi-line logs.
        is_stackish = (
            raw.startswith(" ") or raw.startswith("\t") or raw.startswith("at ") or
            raw.startswith("Caused by:") or raw.startswith("Suppressed:") or raw == "" or
            bool(exception_match)
        )
        known_multiline = filename in {"ardebug.log", "arexception.log", "arjavaplugin.log", "aruser.log", "armonitor.log"}
        is_client_workflow_log = bool(current_client_tx) or bool(client_meta.get("event_type")) or ("active" in filename.lower())
        is_new_header = raw.startswith("<") or bool(GENERIC_TS_RE.search(raw)) or bool(ISO_TS_RE.search(raw)) or bool(MONITOR_TS_RE.search(raw))
        is_continuation = bool(current and not display and not is_client_workflow_log and (is_stackish or (known_multiline and not is_new_header)))
        if is_continuation:
            if len(current.message) < 12000:
                current.message += "\n" + raw
            elif not current.message.endswith("\n… [continued]"):
                current.message += "\n… [continued]"
            if len(current.raw) < 12000:
                current.raw += "\n" + raw
            elif not current.raw.endswith("\n… [continued]"):
                current.raw += "\n… [continued]"
            if "exception" in tags and "exception" not in current.tags:
                current.tags.append("exception")
            if duration_ms and current.duration_ms is None:
                current.duration_ms = duration_ms
            if ar_code and not current.ar_code:
                current.ar_code = ar_code
            continue

        message = clean_message(raw)
        current = LogLine(
            sort_ts=ts,
            display_ts=display,
            level=level,
            pod=pod,
            filename=filename,
            source_path=source_path,
            line_number=idx,
            message=message or raw,
            raw=raw,
            ar_code=ar_code,
            transaction=transaction,
            tid=tid,
            rpc_id=rpc_id,
            queue=queue,
            user=user,
            thread=thread,
            operation=operation,
            component=component,
            duration_ms=duration_ms,
            form=client_meta.get("form") or fields.get("form", "") or fields.get("schema", "") or current_form,
            workflow=client_meta.get("workflow") or fields.get("filter", "") or fields.get("actl", "") or fields.get("active_link", "") or fields.get("escalation", "") or current_workflow,
            field_name=client_meta.get("field_name") or fields.get("field", "") or current_field,
            event_type=client_meta.get("event_type", ""),
            tags=tags,
        )
        rows.append(current)
    return rows
