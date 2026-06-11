# hlx-logs

`hlx-logs` is a Python/FastAPI application for collecting BMC Helix / AR System log files through the `HLX:Logs` AR REST form and making the downloaded log packages available for individual or combined download. Log-content viewing/parsing is intentionally disabled for now and will be redesigned later.

## Current version

**0.0.14**

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
rows.jsonl      # currently empty; reserved for future log-content views
downloads/*.zip
```

Default retention is **5 days**. Old collections are cleaned when the app starts and when a new collection begins.

The supplied Podman/Kubernetes manifests use a temporary `emptyDir` volume for `/data`. This means collections survive container restarts inside the same pod, but are removed when the pod is deleted or recreated. No PVC or hostPath volume is created by default.

## Log content display

Log content display, search, timelines and parser-driven views are intentionally disabled in this version. The app now focuses on collecting log artifacts and allowing users to download them individually or as one flattened zip. The parser files remain in the codebase as a foundation for a later redesign, but fetched collections no longer parse or render log rows.

## Administrator login validation

By default, hlx-logs verifies that the authenticated AR user is a member of Administrator group id `1` before allowing access. The check is performed after `/api/jwt/login` by querying the AR `User` form with the generated JWT and reading the configured group list field.

```yaml
security:
  require_admin_group: true
  user_form: "User"
  login_field: "Login Name"
  group_list_field: "Group List"
  admin_group_id: "1"
```

You can disable this for troubleshooting with `REQUIRE_ADMIN_GROUP=false`, but the recommended setting is to keep it enabled.

## Multi-user behavior

Each collected transaction is tagged with the signed-in username and is only shown to that user. Collections are still stored in the temporary `/data` volume and are removed when the pod is recreated or when retention cleanup deletes them. Users can also delete their own collections immediately from the start page, collections page or result page.

## Version history

### 0.0.14

- Temporarily removed the log-content view, search filters, row tables, timeline modes and parser-driven UI.
- Collection detail pages now focus only on downloadable artifacts.
- Fetched logs are stored as attachments only; no parsing is performed during collection.
- `Download all logs` and individual log downloads remain available.
- Upload still creates a collection, but uploaded files are stored for download rather than parsed for viewing.

### 0.0.13

- Reworked the log result filter bar into a compact default set plus collapsible advanced filters.
- Removed visible From/To time filters; focused time-window filtering now uses Around time only.
- Re-cropped the progress icon asset so the visible medallion fills the circular progress frame better.
- Added responsive/table refinements to reduce horizontal overflow in the result view.

### 0.0.12

- Added upload support for creating collections from local log files.
- Upload accepts multiple files in one request.
- Upload accepts zip archives containing log files, including nested zip attachments.
- Known AR log filenames are recognized from the configured log types and parsed with the same parser as fetched collections.
- Uploaded collections are user-scoped, temporary, searchable, downloadable and deletable like fetched collections.
- Added `/upload` page and navigation entry.

### 0.0.11

- Added time interval filters to the log result view: from/to timestamps and a focused "around time" mode with configurable minutes before/after.
- Added quick focus links per log row (`±2m` and `±10m`) so a user can inspect context around a specific event.
- Added tag and user filters based on parsed log structure.
- Improved parser heuristics for uploaded AR log samples, especially Java/plugin stack traces, monitor lines, API/transaction metadata and FTS/auth/performance tags.
- Reworked the progress icon rendering with a dedicated medallion crop and circular frame so the visible icon fills the progress circle better.

### 0.0.10

- Cropped the transparent padding from the application icon and used the cropped icon in the top bar and progress animation.
- Improved progress screen layout so long fetch messages do not overflow the card.
- Collection jobs now continue when an individual log request, read, attachment download, or parse step fails. Failed items are reported as warnings instead of aborting the whole collection when other logs succeed.
- Result and collection views now show collection warnings/failure counts.

### 0.0.9

- Added Administrator group validation at login. By default the user must be a member of AR group id `1`.
- Added configurable security settings: `security.require_admin_group`, `security.user_form`, `security.login_field`, `security.group_list_field`, and `security.admin_group_id`.
- Added multi-user collection ownership so users can collect, browse and delete their own temporary log transactions without colliding with other users.
- Added delete actions on the start page, collections page and result page.
- Added `Download all logs`, which returns one flattened zip containing the extracted log files rather than the raw nested zip attachments.
- Optimized result rendering with cached parsed rows and paged result tables. Default result page size is now 1000 rows, selectable up to 2000 rows.
- Refined the visual style again toward the HLX Migrator look: flatter buttons, tighter tables, less rounded controls and denser panes.

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
