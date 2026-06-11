# Log categorization

Starting in 0.0.17, hlx-logs classifies discovered log files automatically from filenames returned by `AR System Server Group Logs`.

The classifier intentionally supports multiple tags per log file. For example, `ardebug.log` is categorized as **Combined Trace** and tagged with `api`, `filter`, `sql`, `escalation`, `user`, `workflow` and `debug`, because the file can contain multiple trace streams depending on which AR Server logging options are enabled.

## Primary categories

| Category | Examples | Typical purpose |
|---|---|---|
| Core AR | `arerror.log` | Startup, AR Server events, warnings, errors and ARNOTE/ARERR-style diagnostics |
| Performance / Exceptions | `arexception.log` | Slow API threshold events, exception/performance traces, TrID/TID/RPC context |
| Combined Trace | `ardebug.log` | Combined AR debug output; may include API, Filter, SQL, Escalation and User traces |
| API Trace | `arapi.log`, `arinternalapi.log` | AR API call tracing |
| Filter Trace | `arfilter.log` | Filter workflow tracing |
| SQL Trace | `arsql.log` | SQL/database tracing and database performance analysis |
| Escalation Trace | `aresc.log`, `arescl.log` | Escalation workflow tracing |
| User Trace | `aruser.log` | User/session related tracing |
| Runtime Monitor | `armonitor.log` | Runtime monitor, process/thread and stack diagnostics |
| Startup | `arstartup_trace-*.log` | Startup traces for specific startup timestamps |
| Java Plug-in Server | `arjavaplugin.log`, `arjavaplugin.log.1` | Java plug-in server, plug-in loading and Java stack traces |
| Java Plug-in Streams | `arjavaplugin-stdout-*.log`, `arjavaplugin-stderr-*.log` | Java plug-in process stdout/stderr streams |
| Plug-ins | `AtriumPluginSvr.log`, `ard2pplugin.log`, `depUtilPlugin.log` | Plug-in-specific logs |
| REST API / Web / Jetty | `arrestwebservice.log`, `jetty.log`, `rsso-agent.0.log` | HTTP, REST API, Jetty and authentication-web integration logs |
| Search / FTS / AI | `arfts.log`, `arftindx.log`, `ais.log`, `ai_ardbc_plugins.log` | Full-text search, indexing and AI-related components |
| Deployment | `bundledeploy.log`, `ard2pdeploymentactivity.log`, `arfiledeployer.log` | Bundle, D2P and deployment activities |
| CMDB / Atrium | `cmdb_*.log`, `atrium-ar-kit.log`, `AtriumPluginSvr.log` | CMDB/Atrium logging |
| Configuration / Cache | `arcache*.log`, `ccs_*.log`, `keyexternalization.log` | Cache, configuration and key/encryption utilities |
| Email / Notification | `email.log.0`, `aralert.log` | Email engine and alert/notification logging |
| Runtime Services | `events.log`, `process.log`, `jmx.log`, `ServiceContext.log` | Runtime services and event/process logs |
| Security / License / Attachment | `License_consumption_limit_debug.log`, `attachment_validation.log` | Security, validation and license-related logging |
| Custom | `dice.*`, `additionalLogs.log` | Customer/custom application logs |
| Carte / Integrations | `Carte-signal.log` | Carte/job-server and integration runtime logging |
| Server Group | `arhgroup.log`, `sgrefreshcycle.log` | Server group / high-availability behavior |
| Database Utilities | `ardbcheck.log`, `arcustomdbfunction.log` | Database checks and custom DB function diagnostics |
| Data Connector | `ardataconnector.log` | Data connector diagnostics |
| Reporting | `smartreporting.log`, `arjavaplugin-reporting.log` | Reporting and Smart Reporting logs |
| Telemetry | `telemetry-sync.log` | Telemetry synchronization |
| Application Runtime | `service.log`, `user.log` | Generic application service/user runtime logs |

## Notes

* Classification is based on filename metadata during discovery. Content-based analysis can be added later when the log viewer is redesigned.
* Rotated and date-suffixed files inherit the category of the base log, for example `armonitor.log.1` and `arjavaplugin-stderr-2026-06-01.log`.
* Zero-byte files are retained when `discovery.include_zero_byte_logs` is true and get an additional `empty` tag.
