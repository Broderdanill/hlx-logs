from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import io
import logging
import re
import zipfile
from dateutil import parser as dtparser

logger = logging.getLogger(__name__)

TIMESTAMP_PATTERNS = [
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?(?:Z|[+-]\d{2}:?\d{2})?)"),
    re.compile(r"(?P<ts>\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}(?:[.,]\d{1,6})?)"),
    re.compile(r"(?P<ts>[A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2}\s+\d{4})"),
]
LEVEL_RE = re.compile(r"\b(?P<level>TRACE|DEBUG|INFO|WARN|WARNING|ERROR|FATAL|SEVERE)\b", re.IGNORECASE)


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


def extract_zip_files(blob: bytes) -> dict[str, str]:
    """Return a mapping of filename -> decoded text. Non-zip payloads become payload.log."""
    if not blob:
        return {}
    if zipfile.is_zipfile(io.BytesIO(blob)):
        result: dict[str, str] = {}
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                raw = zf.read(info.filename)
                text = decode_bytes(raw)
                result[info.filename] = text
        return result
    return {"payload.log": decode_bytes(blob)}


def decode_bytes(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_timestamp(line: str) -> tuple[datetime, str]:
    for pattern in TIMESTAMP_PATTERNS:
        match = pattern.search(line)
        if not match:
            continue
        text = match.group("ts")
        try:
            dt = dtparser.parse(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt, text
        except Exception:
            logger.debug("Failed to parse timestamp %s", text, exc_info=True)
    return datetime.max.replace(tzinfo=timezone.utc), ""


def parse_level(line: str) -> str:
    match = LEVEL_RE.search(line)
    return match.group("level").upper() if match else ""


def parse_log_text(text: str, *, pod: str, filename: str, source_path: str) -> list[LogLine]:
    rows: list[LogLine] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        ts, display_ts = parse_timestamp(line)
        rows.append(
            LogLine(
                sort_ts=ts,
                display_ts=display_ts,
                level=parse_level(line),
                pod=pod,
                filename=filename,
                source_path=source_path,
                line_number=idx,
                message=line,
                raw=line,
            )
        )
    return rows
