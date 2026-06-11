from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class LogClassification:
    category: str
    tags: list[str] = field(default_factory=list)
    severity: str = "info"
    parser: str = "generic"
    description: str = ""


def _has_any(name: str, tokens: list[str]) -> bool:
    return any(token in name for token in tokens)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _is_zero_size(value: str | None) -> bool:
    normalized = (value or "").strip().lower()
    return normalized in {"0", "0kb", "0 kb", "0.0 kb", "0 b", "0 bytes"}


def classify_log_file(filename: str, file_size: str | None = None) -> LogClassification:
    """Classify AR/Helix log files from discovered filename metadata.

    The AR System Server Group Logs form gives us filename and size, but not
    content. These rules are based on observed AR Server, plug-in, Jetty, CMDB,
    deployment and Helix custom log names. A single file can intentionally get
    several tags, for example ardebug.log is a combined trace file and can
    contain API, Filter, SQL, Escalation and User trace entries depending on
    AR Server logging settings.
    """
    raw = filename or ""
    name = raw.lower()
    tags: list[str] = []
    category = "Other"
    severity = "info"
    parser = "generic"
    description = "Discovered log file."

    # Rotated/date-suffixed files inherit from the base name.
    base = re.sub(r"\.\d+$", "", name)
    base = re.sub(r"-\d{4}-\d{2}-\d{2}.*(?=\.log$)", "", base)
    base = re.sub(r"\.log\.\d+$", ".log", base)

    if _is_zero_size(file_size):
        tags.append("empty")

    # Core AR Server and high-value diagnostics.
    if base in {"arerror.log"}:
        category = "Core AR"
        tags += ["core", "error", "startup", "configuration", "fts"]
        severity = "critical"
        parser = "ar_timestamp"
        description = "Main AR Server events, startup messages, warnings, errors and ARNOTE/ARERR-style diagnostics."

    elif base in {"arexception.log"}:
        category = "Performance / Exceptions"
        tags += ["performance", "exception", "api", "threshold", "transaction"]
        severity = "critical"
        parser = "ar_trace"
        description = "Slow API threshold events and exception/performance traces with TrID, TID, RPC ID, Queue and USER context."

    elif base in {"ardebug.log"}:
        category = "Combined Trace"
        tags += ["trace", "combined", "api", "filter", "sql", "escalation", "user", "workflow", "debug"]
        severity = "debug"
        parser = "ar_trace"
        description = "Combined AR debug output. Depending on enabled server logging, this may contain API, Filter, SQL, Escalation and User trace content."

    elif base in {"armonitor.log"}:
        category = "Runtime Monitor"
        tags += ["runtime", "monitor", "thread", "process", "performance", "stack"]
        severity = "debug"
        parser = "ar_monitor"
        description = "AR Monitor/process monitor output, thread/process state and stack-oriented runtime diagnostics."

    elif base.startswith("arstartup_trace"):
        category = "Startup"
        tags += ["startup", "core", "configuration", "runtime"]
        severity = "debug"
        parser = "ar_timestamp"
        description = "AR Server startup trace for a specific startup date/time."

    elif base in {"arhealthmonitor.log", "arprobe.log", "arsrvgrp.log", "arsignald.log"}:
        category = "Health / Runtime"
        tags += ["health", "runtime", "monitor", "server-group"]
        parser = "ar_timestamp"
        description = "Health, probe, signal daemon or server group runtime status logging."

    elif base in {"arthread.log", "threaddump.log"} or "thread" in name:
        category = "Threads"
        tags += ["thread", "stack", "runtime", "performance"]
        severity = "debug"
        parser = "thread_dump"
        description = "Thread and stack dump related diagnostics."

    # Explicit AR trace files.
    elif base in {"arapi.log"}:
        category = "API Trace"
        tags += ["trace", "api", "transaction"]
        severity = "debug"
        parser = "ar_trace"
        description = "AR API trace. Useful for following AR API calls and client/RPC activity."

    elif base in {"arfilter.log"}:
        category = "Filter Trace"
        tags += ["trace", "filter", "workflow", "transaction"]
        severity = "debug"
        parser = "ar_trace"
        description = "AR Filter workflow trace."

    elif base in {"arsql.log"}:
        category = "SQL Trace"
        tags += ["trace", "sql", "database", "performance"]
        severity = "debug"
        parser = "ar_trace"
        description = "AR SQL trace. Useful for database statements and SQL performance analysis."

    elif base in {"aresc.log", "arescl.log"}:
        category = "Escalation Trace"
        tags += ["trace", "escalation", "workflow"]
        severity = "debug"
        parser = "ar_trace"
        description = "AR Escalation trace."

    elif base in {"aruser.log"}:
        category = "User Trace"
        tags += ["trace", "user", "session", "authentication"]
        severity = "debug"
        parser = "ar_trace"
        description = "AR User trace / user-session related diagnostics."

    elif base in {"arinternalapi.log"}:
        category = "Internal API Trace"
        tags += ["trace", "api", "internal", "transaction"]
        severity = "debug"
        parser = "ar_trace"
        description = "Internal AR API trace."

    # Plug-in and Java.
    elif "javaplugin" in name:
        if "authentication" in name:
            category = "Authentication Plug-in"
            tags += ["plugin", "java", "authentication", "security", "rsso"]
            severity = "debug"
        elif "stdout" in name or "stderr" in name:
            category = "Java Plug-in Streams"
            tags += ["plugin", "java", "stdout", "stderr", "stream"]
            severity = "debug"
        elif "reporting" in name:
            category = "Reporting Plug-in"
            tags += ["plugin", "java", "reporting"]
        else:
            category = "Java Plug-in Server"
            tags += ["plugin", "java", "exception", "arplugin"]
            severity = "critical"
        parser = "plugin"
        description = "Java plug-in server logging, plug-in loading, plug-in errors and Java stack traces."

    elif _has_any(name, ["plugin", "atriumpluginsvr", "ai_ardbc", "deputil"]):
        category = "Plug-ins"
        tags += ["plugin", "java", "arplugin"]
        if "atrium" in name or "cmdb" in name:
            tags.append("cmdb")
        if "d2p" in name or "deploy" in name:
            tags.append("deployment")
        severity = "debug"
        parser = "plugin"
        description = "AR/Helix plug-in server or plug-in-specific log."

    elif base in {"old_plugin_log.log"}:
        category = "Plug-ins"
        tags += ["plugin", "legacy"]
        parser = "plugin"
        description = "Legacy/old plug-in log."

    # Web, REST, HTTP, webhooks and RSSO.
    elif _has_any(name, ["restwebservice", "jetty", "rsso", "wbhk", "wkflowrestclient"]):
        if "jetty" in name:
            category = "Web / Jetty"
            tags += ["web", "jetty", "http", "access", "rest"]
            parser = "http_access"
            description = "Jetty HTTP access/application log, often including REST API request lines."
        elif "rsso" in name:
            category = "Authentication / RSSO"
            tags += ["auth", "security", "rsso", "web"]
            parser = "generic"
            description = "RSSO agent / authentication integration log."
        elif "wbhk" in name:
            category = "Webhooks"
            tags += ["webhook", "integration", "rest", "workflow"]
            parser = "generic"
            description = "AR webhook related logging."
        elif "wkflowrestclient" in name:
            category = "Workflow REST Client"
            tags += ["workflow", "rest", "integration", "http"]
            parser = "generic"
            description = "Workflow REST client integration logging."
        else:
            category = "REST API"
            tags += ["web", "rest", "api", "http", "exception"]
            severity = "critical"
            parser = "java"
            description = "AR REST API service log, including REST request handling and servlet/Jersey stack traces."

    # FTS / search / AI.
    elif _has_any(name, ["arfts", "arftindx", "fts", "ais", "syntheticmonitoring", "ai_"]):
        category = "Search / FTS / AI"
        tags += ["fts", "search", "index", "ai"]
        if "synthetic" in name:
            tags += ["monitor", "synthetic"]
        parser = "generic"
        description = "Full-text search, indexing, AI or synthetic monitoring related logging."

    # Cache, configuration, localization and platform components.
    elif _has_any(name, ["cache", "ccs_", "config", "compgroup", "localetran", "keyexternalization", "encryptionutility"]):
        category = "Configuration / Cache"
        tags += ["configuration", "cache", "platform"]
        if "encryption" in name or "keyexternalization" in name:
            tags += ["security", "encryption"]
        parser = "generic"
        description = "Configuration, cache synchronization/eviction, locale or encryption/key management logging."

    # Deployment / packaging / bundles / D2P.
    elif _has_any(name, ["bundle", "deployer", "d2p", "deployment", "hlx-export"]):
        category = "Deployment"
        tags += ["deployment", "bundle", "d2p", "package"]
        parser = "generic"
        description = "Deployment, bundle, D2P or file deployer activity."

    # CMDB / Atrium.
    elif _has_any(name, ["cmdb", "atrium"]):
        category = "CMDB / Atrium"
        tags += ["cmdb", "atrium", "plugin"]
        parser = "generic"
        description = "CMDB/Atrium component logging."

    # Email and notification.
    elif _has_any(name, ["email", "aralert", "notification"]):
        category = "Email / Notification"
        tags += ["email", "notification", "alert"]
        parser = "generic"
        description = "Email Engine, alert or notification related logging."

    # Monitoring, runtime services and process/event logs.
    elif _has_any(name, ["alwayson", "events", "process", "jmx", "servicecontext", "dsm", "noe"]):
        category = "Runtime Services"
        tags += ["runtime", "monitor", "service", "events"]
        if "jmx" in name:
            tags.append("jmx")
        parser = "generic"
        description = "Runtime service, event, process or service context logging."

    # Security, validation and cloud integrations.
    elif _has_any(name, ["license", "attachment_validation", "awssdk"]):
        if "license" in name:
            category = "License"
            tags += ["license", "security"]
        elif "attachment" in name:
            category = "Attachment / Security"
            tags += ["attachment", "validation", "security"]
        else:
            category = "Cloud / AWS"
            tags += ["aws", "cloud", "integration"]
        parser = "generic"
        description = "Security, validation, license or cloud integration logging."

    # Custom / customer / local logs.
    elif name.startswith("dice.") or _has_any(name, ["test.log", "additionallogs"]):
        category = "Custom"
        tags += ["custom", "dice"]
        parser = "generic"
        description = "Custom/customer-specific log."


    elif "carte" in name:
        category = "Carte / Integrations"
        tags += ["carte", "integration", "job", "kettle"]
        parser = "generic"
        description = "Carte/job-server style integration log."

    elif _has_any(name, ["ararchive", "ardbcheck", "arcustomdbfunction", "ardataconnector", "arextension"]):
        if "archive" in name:
            category = "Archive"
            tags += ["archive", "core"]
            description = "AR archive-related logging."
        elif "dbcheck" in name or "customdbfunction" in name:
            category = "Database Utilities"
            tags += ["database", "db", "utility"]
            description = "Database check or custom database function logging."
        elif "dataconnector" in name:
            category = "Data Connector"
            tags += ["data", "connector", "integration"]
            description = "Data connector logging."
        else:
            category = "Extensions"
            tags += ["extension", "platform"]
            description = "AR extension framework logging."
        parser = "generic"

    elif "arhgroup" in name or "sgrefreshcycle" in name:
        category = "Server Group"
        tags += ["server-group", "runtime", "high-availability"]
        parser = "generic"
        description = "Server group / high-availability related logging."

    elif "smartreporting" in name:
        category = "Reporting"
        tags += ["reporting", "smart-reporting"]
        parser = "generic"
        description = "Smart Reporting related logging."

    elif "telemetry" in name:
        category = "Telemetry"
        tags += ["telemetry", "sync", "platform"]
        parser = "generic"
        description = "Telemetry synchronization logging."

    elif name in {"service.log", "user.log"} or re.match(r"user\.log\.\d+$", name):
        category = "Application Runtime"
        tags += ["application", "runtime", "user"]
        parser = "generic"
        description = "Generic application service/user runtime log."

    # Fallbacks based on name fragments.
    else:
        if "error" in name or "exception" in name or "uncaught" in name:
            category = "Errors / Exceptions"
            tags += ["error", "exception"]
            severity = "critical"
        elif "debug" in name or "trace" in name:
            category = "Debug / Trace"
            tags += ["debug", "trace"]
            severity = "debug"
        elif "api" in name:
            category = "API / Integration"
            tags += ["api", "integration"]
        elif "sql" in name:
            category = "SQL / Database"
            tags += ["sql", "database"]
            severity = "debug"
        elif "filter" in name:
            category = "Filter / Workflow"
            tags += ["filter", "workflow"]
            severity = "debug"
        elif "app" in name:
            category = "Application"
            tags += ["application"]
        else:
            category = "Other"
            tags += ["other"]

    tags = _dedupe(tags)
    return LogClassification(
        category=category,
        tags=tags,
        severity=severity,
        parser=parser,
        description=description,
    )
