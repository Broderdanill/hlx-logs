# hlx-logs

`hlx-logs` is a FastAPI web application for collecting, downloading and analysing BMC Helix / AR System log files through AR REST.

The application is designed for containerized BMC Helix environments where log files are exposed through the AR System Server Group log workflow. It lets an administrator discover AR pods, fetch selected logs, download the raw log packages quickly, and run parsing/indexing only when analysis is needed.

## Version

**1.0.7**

Patch notes:
- Added support for `Restrict-Logging`, the on/off switch for restricted AR logging. The UI now reads this setting to decide whether restricted logging is enabled.
- Saving restricted logging writes `Restrict-Logging=1` when enabled and `Restrict-Logging=0` when disabled.
- `Restrict-Log-Users` remains the semicolon-separated user list and is preserved when restricted logging is disabled.
- Missing `Restrict-Logging` and `Restrict-Log-Users` component-setting rows are created through the existing Component/Setting GUID mapping flow.

## What it does

- Authenticates users against AR REST with `/api/jwt/login`.
- Optionally verifies that the user belongs to the AR Administrator group (`Group List` id `1` by default).
- Discovers AR pods from `AR System Configuration Component Setting`.
- Discovers available log files and sizes from `AR System Server Group Logs`.
- Requests selected logs through the custom `HLX:Logs` form.
- Downloads the attachment from field `1EX`.
- Stores collections temporarily under `/data`.
- Lets users download the raw logs immediately without parsing.
- Runs parsing/indexing only when the user clicks **Analyze logs**.
- Supports uploaded/pasted logs and zip/nested zip uploads.
- Provides searchable log analysis and focused filter transaction flow visualization.
- Manages AR Debug-mode log settings and selected log filename settings through AR REST.

## Required AR Developer Studio import

Before using the application, import the Developer Studio definition file included at the root of this repository:

```text
HLX_LOGS_APP.def
```

This definition creates the custom AR form:

```text
HLX:Logs
```

It also creates the workflow that triggers the BMC AR server group log service/plugin so the requested file is added as an attachment. `hlx-logs` depends on this form and workflow to fetch log files through AR REST.

Expected request fields on `HLX:Logs`:

```text
Pod
Directory
TransactionId
Filename
```

Expected attachment field:

```text
1EX
```

## Runtime model

1. A user logs in to `hlx-logs` with AR credentials.
2. The app stores the AR-JWT in the web session.
3. Discovery reads pods and available log files using that AR-JWT.
4. Collect creates one `HLX:Logs` entry per selected pod/log file.
5. AR workflow attaches a zip/log package to field `1EX`.
6. `hlx-logs` downloads and stores the raw package under `/data`.
7. The result opens on the **Downloads** tab.
8. The user can download raw logs immediately or click **Analyze logs** to parse/index the collection.

## Build and run with Podman

```bash
podman build -f Containerfile -t localhost/hlx-logs:latest .
podman play kube deploy/podman-play-kube.yaml
```

Open:

```text
http://localhost:8095
```

## Build with Buildah

```bash
chmod +x buildah-script.sh
./buildah-script.sh
```

Optional build variables:

```bash
IMAGE_NAME=localhost/hlx-logs IMAGE_TAG=1.0.7 ./buildah-script.sh
NO_CACHE=true ./buildah-script.sh
PULL=false ./buildah-script.sh
```

## Configuration

The application reads `config.yaml` and environment variables. Environment variables override `config.yaml`.

Important defaults:

```yaml
ar:
  base_url: "http://ars-arserver:8008"
  form_name: "HLX:Logs"
  attachment_field: "1EX"
  collect_concurrency: 4

storage:
  data_dir: "/data"
  retention_days: 5

security:
  require_admin_group: true
  admin_group_id: "1"
```

Useful environment variables:

```text
AR_BASE_URL=http://ars-arserver:8008
SESSION_SECRET=change-me-to-a-long-random-string
LOG_LEVEL=INFO
CONFIG_PATH=/app/config.yaml
DATA_DIR=/data
RETENTION_DAYS=5
AR_COLLECT_CONCURRENCY=4
REQUIRE_ADMIN_GROUP=true
AR_ADMIN_GROUP_ID=1
```

## Discovery forms

Pods are discovered from:

```text
Form:  AR System Configuration Component Setting
Query: 'Setting Name' = "Configuration-Name"
Field: Setting Value
```

Available log files are discovered per pod from:

```text
Form:  AR System Server Group Logs
Query: 'Server Name' = "<pod>"
File:  fileName
Size:  File Size
```

The default log directory sent to `HLX:Logs` is:

```text
/opt/bmc/ARSystem/db
```

## Log settings

The **Log settings** page updates future AR server logging; it does not fetch existing log files.

`Debug-mode` is read and written through:

```text
Form: AR System Configuration Component Setting
Qual: ('Setting Name' = "Debug-mode") AND ('Component Type' = "com.bmc.arsys.server") AND ('Component Name' = "<pod>")
Field: Setting Value
```

Supported `Debug-mode` bit values:

| Value | Log type | Default file |
| ---: | --- | --- |
| 1 | SQL | `arsql.log` |
| 2 | Filter | `arfilter.log` |
| 4 | User | `aruser.log` |
| 8 | Escalation | `arescl.log` |
| 16 | API | `arapi.log` |
| 32 | Thread | `arthread.log` |
| 64 | Alert | `aralert.log` |
| 256 | Server Group | `arsrvgrp.log` |
| 512 | Full Text Index | `arftindx.log` |
| 1024 | Archive | `ararchive.log` |
| 32768 | DSO Server | `ardist.log` |
| 65536 | Approval Server | `arapprov.log` |
| 131072 | Plug-in | `arplugin.log` |

Filename settings are written only for enabled log types whose filename was changed in the UI. Known filename setting names include:

```text
API-Log-File
Escalation-Log-File
Filter-Log-File
SQL-Log-File
Thread-Log-File
Alert-Log-File
```

`Restrict-Logging` controls whether restricted logging is active (`1` = enabled, `0` = disabled). `Restrict-Log-Users` stores the semicolon-separated AR login names used when restricted logging is enabled; the list is preserved when `Restrict-Logging` is set to `0`.

Before saving any setting through `AR System Configuration Component Setting`, `hlx-logs` checks `AR System Block Configuration Component Setting` for rows where `Status` is `Blocked Setting` **and where `Setting Name` matches the setting being saved**. Only those scoped rows are changed to `Allowed Setting` before the save starts, and the same scoped settings are verified again after saving. This unlocks the relevant AR settings without changing unrelated blocked settings.

The Log settings page is read live from AR REST every time it is opened and sends `Cache-Control: no-store`, so the displayed `Debug-mode`, filename settings, `Restrict-Logging` and `Restrict-Log-Users` values reflect the current AR configuration rather than cached UI state.

## Collections and storage

Collections are stored under:

```text
/data/collections/<collection-id>
```

Each collection contains:

```text
meta.json
rows.jsonl          # written after analysis
rows.sqlite3        # written after analysis
downloads/          # raw downloaded packages
```

Collections are temporary. The cleanup job removes collections older than `retention_days` when the app starts and when new collections are created.

## Analysis and visual flow

Analysis is intentionally lazy. A fetched or uploaded collection is first saved with `analysis_status: pending`; parsing and indexing run only when the user clicks **Analyze logs**.

The parser supports:

- `arfilter.log`
- API/SQL/filter style AR traces
- active link logs
- progressive active link logs
- escalation/generic AR logs
- zip, nested zip and gzip payloads

Filter transaction flow uses the AR `TrID` found in `arfilter.log`, not the collection id generated by the app. The flow groups related filter-processing frames and focuses on operation, qualification, IF/ELSE actions, service/guide calls and relevant outputs.

## Logging

Application logging uses Python logging and is controlled with:

```text
LOG_LEVEL=INFO
```

Recommended values:

```text
DEBUG
INFO
WARNING
ERROR
```

The app avoids logging passwords and AR-JWT values. AR REST errors are logged with enough detail for troubleshooting while avoiding credential output.

## Troubleshooting

### Login fails

Check that `AR_BASE_URL` is reachable from inside the `hlx-logs` container:

```bash
curl -i http://ars-arserver:8008/api/jwt/login
```

### Discovery shows no pods

Verify that the logged-in AR user can query:

```text
AR System Configuration Component Setting
```

with:

```text
'Setting Name' = "Configuration-Name"
```

### Discovery shows pods but no log files

Verify access to:

```text
AR System Server Group Logs
```

and that the query below returns entries:

```text
'Server Name' = "<pod>"
```

### Collect fails with no attachment

Confirm that `HLX_LOGS_APP.def` has been imported and that the workflow on `HLX:Logs` is enabled. The app expects the retrieved log package on attachment field `1EX`.

### Analysis is empty

Open the collection and click **Analyze logs**. New collections are intentionally not parsed until analysis is explicitly requested.

## Repository layout

```text
app/                       FastAPI application
app/static/                CSS, JavaScript, Mermaid and icons
app/templates/             Jinja templates
config.yaml                default runtime configuration
HLX_LOGS_APP.def           required AR Developer Studio import
deploy/podman-play-kube.yaml
Containerfile
buildah-script.sh
```
