# hlx-logs

`hlx-logs` is a Python/FastAPI application for collecting BMC Helix / AR System log files through the `HLX:Logs` AR REST form, storing the collected zip files, parsing the log content, and viewing merged timelines in a dark UI.

## Current version

**0.0.8**

## Run with Podman

```bash
podman build -f Containerfile -t hlx-logs:latest .
podman play kube deploy/podman-play-kube.yaml
```

Open:

```text
http://localhost:8095
```

## Configuration

Configuration is read from `config.yaml`, normally mounted through a ConfigMap.

```yaml
ar:
  base_url: "http://ars-arserver:8008"
  form_name: "HLX:Logs"
  attachment_field: "1EX"

storage:
  data_dir: "/data"
  retention_days: 5
```

Environment overrides:

```text
AR_BASE_URL
AR_FORM_NAME
AR_ATTACHMENT_FIELD
DATA_DIR
RETENTION_DAYS
LOG_LEVEL
SESSION_SECRET
```

## Stored collections

Collections are stored under:

```text
/data/collections/<TransactionId>/
```

Each collection contains:

```text
meta.json
rows.jsonl
downloads/*.zip
```

Default retention is **5 days**. Old collections are cleaned when the app starts and when a new collection begins.

The supplied Podman/Kubernetes manifests use a temporary `emptyDir` volume for `/data`. This means collections survive container restarts inside the same pod, but are removed when the pod is deleted or recreated. No PVC or hostPath volume is created by default.

## Log parsing

The parser currently recognizes common AR Server log patterns from the provided examples:

- ISO timestamp lines used by `arerror.log`, `ardebug.log`, and `arexception.log`
- AR monitor angle-bracket format from `armonitor.log`
- `ARERR`, `ARWARN`, `ARNOTE` style codes
- `TrID`, `TID`, `RPC ID`, `Queue`, `USER`, and monitor thread names
- API duration patterns such as `API[5.199 seconds]`
- Java exception / stack trace continuation lines

## Version history

### 0.0.8

- Changed Podman/Kubernetes manifests to use temporary `emptyDir` storage for `/data`.
- Removed the default hostPath-based data volume from the Podman manifest.
- Clarified that stored collections are temporary unless the deployment is explicitly changed to persistent storage.

### 0.0.7

- Reworked the UI to better match the HLX Migrator visual style: dark teal, flatter solid buttons, less rounded controls, denser tables.
- Removed the Availability Matrix from the collect page.
- Added persistent stored collections so previously fetched log transactions can be opened again.
- Added `/collections` view.
- Added progress screen with app icon animation, progress bar and live status text while logs are fetched.
- Added configurable storage retention, defaulting to 5 days.
- Added `/data` volume mount to the Podman/Kubernetes manifests. In 0.0.8 this was changed to temporary `emptyDir`.
- Improved AR log parsing based on uploaded samples: transaction id, TID, RPC ID, user, queue, duration, AR codes, monitor format and Java stack traces.
- Added result modes: combined timeline, by transaction, and by file.

### 0.0.6

- Added dark mode UI.
- Added app icon and favicon.
- Added common AR Server log type configuration for `arserver.sandbox`.
- Added richer parser metadata and result filters.

### 0.0.5

- Changed default AR REST base URL to `http://ars-arserver:8008`.

### 0.0.4

- Changed default attachment field to `1EX`.
- Attachment download uses `/api/arsys/v1/entry/{formName}/{entryId}/attach/{fieldName}`.
- Added retries while waiting for attachment generation.

### 0.0.3

- Removed invalid `Request ID` field from HLX:Logs transaction queries.
- Entry id is derived from REST links instead.
- Display signed-in user next to logout.

### 0.0.2

- Renamed `Dockerfile` to `Containerfile`.
- Changed application port to 8095.
- Added version history.

### 0.0.1

- Initial FastAPI MVP.
- Login through AR REST JWT.
- POST log requests to `HLX:Logs`.
- Download attachments and parse lines into a searchable timeline.
