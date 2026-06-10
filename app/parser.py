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


def _parse_ar_ts(raw: str) -> datetime | None:
    try:
        fixed = raw
        if re.search(r"[+-]\d{4}$", fixed):
            fixed = fixed[:-5] + fixed[-5:-2] + ":" + fixed[-2:]
        # armonitor sample has unusual Mon Mar 30 2026 16:15:21.0696 order.
        m = re.fullmatch(r"([A-Z][a-z]{2})\s+([A-Z][a-z]{2})\s+(\d{1,2})\s+(\d{4})\s+(\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)", fixed)
        if m:
            fixed = f"{m.group(1)} {m.group(2)} {m.group(3)} {m.group(5)} {m.group(4)}"
        dt = dtparser.parse(fixed, fuzzy=True)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def parse_timestamp(line: str) -> tuple[datetime, str]:
    for pattern in (ISO_TS_RE, MONITOR_TS_RE, GENERIC_TS_RE, LEGACY_TS_RE):
        match = pattern.search(line)
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
        "plugin": ["plugin", "java", "arjavaplugin"],
        "startup": ["armonitor", "restart", "started", "stopped", "extension loaded", "executing process"],
        "exception": ["exception", "stacktrace", "caused by", "frameworkevent error"],
        "performance": ["elapsed time", "threshold", "seconds]", " api["],
        "server-group": ["server group", "arhgroup", "ranking", "operation ranking"],
    }
    for tag, needles in checks.items():
        if any(n in lower for n in needles):
            tags.append(tag)
    if fields.get("trid"):
        tags.append("transaction")
    return tags


def clean_message(line: str) -> str:
    # Remove leading timestamp, but keep the rest intact for readability.
    line = ISO_TS_RE.sub("", line, count=1).strip()
    return line or ""


def parse_log_text(text: str, pod: str, filename: str, source_path: str) -> list[LogLine]:
    rows: list[LogLine] = []
    current: LogLine | None = None
    for idx, raw in enumerate(text.splitlines(), start=1):
        ts, display = parse_timestamp(raw)
        fields = extract_fields(raw)
        monitor_level = fields.get("level", "") if filename == "armonitor.log" or raw.startswith("<MNTR>") else ""
        level = infer_level(raw, monitor_level)
        code_match = ARERR_RE.search(raw)
        ar_code = code_match.group("code") if code_match else ""
        duration_ms = infer_duration_ms(raw)
        transaction = fields.get("trid", "") or fields.get("transaction_id", "")
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
        exception_match = JAVA_EXCEPTION_RE.search(raw)
        tags = infer_tags(raw, filename, fields)
        if exception_match and "exception" not in tags:
            tags.append("exception")

        is_continuation = current and not display and (
            raw.startswith(" ") or raw.startswith("\t") or raw.startswith("at ") or raw.startswith("Caused by:") or raw == ""
        )
        if is_continuation:
            current.message += "\n" + raw
            current.raw += "\n" + raw
            if "exception" in tags and "exception" not in current.tags:
                current.tags.append("exception")
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
            tags=tags,
        )
        rows.append(current)
    return rows
