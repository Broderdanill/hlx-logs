# hlx-logs

`hlx-logs` is a Python/FastAPI application for collecting BMC Helix / AR System log files through the `HLX:Logs` AR REST form and making the downloaded log packages available for individual or combined download.

The result interface provides a log-analysis view and a visual flow view while still keeping the original downloaded files available individually and as a combined zip.

## Current version

**0.0.28**

## Run with Podman

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
podman play kube deploy/podman-play-kube.yaml
```

## Configuration

Configuration is read from `config.yaml`, normally mounted through a ConfigMap.

```yaml
ar:
  base_url: "http://ars-arserver:8008"
  form_name: "HLX:Logs"
  attachment_field: "1EX"

discovery:
  enabled: true
  refresh_interval_seconds: 300
  pod_form_name: "AR System Configuration Component Setting"
  pod_query: "'Setting Name' = \"Configuration-Name\""
  pod_value_field: "Setting Value"
  log_form_name: "AR System Server Group Logs"
  log_server_field: "Server Name"
  log_filename_field: "fileName"
  log_size_field: "File Size"
  default_directory: "/opt/bmc/ARSystem/db"
  include_zero_byte_logs: true

storage:
  data_dir: "/data"
  retention_days: 5
```

## Automatic discovery

Manual pod/log configuration has been removed from the UI. After login, and then whenever the discovery cache is stale, the app uses the signed-in user's AR-JWT to discover:

1. available pod/server names from `AR System Configuration Component Setting`, using the configured pod query;
2. log files for each pod from `AR System Server Group Logs`, using `Server Name = "<pod>"`.

The discovered list is kept in memory for `discovery.refresh_interval_seconds`. A manual **Refresh now** button is available on the Discovery page.

Because discovery uses the logged-in user's JWT, no separate service credential is stored in the app.

## Environment overrides

```text
AR_BASE_URL
AR_FORM_NAME
AR_ATTACHMENT_FIELD
DATA_DIR
RETENTION_DAYS
LOG_LEVEL
SESSION_SECRET

DISCOVERY_ENABLED
DISCOVERY_REFRESH_INTERVAL_SECONDS
DISCOVERY_POD_FORM_NAME
DISCOVERY_POD_QUERY
DISCOVERY_POD_VALUE_FIELD
DISCOVERY_LOG_FORM_NAME
DISCOVERY_LOG_SERVER_FIELD
DISCOVERY_LOG_FILENAME_FIELD
DISCOVERY_LOG_SIZE_FIELD
DISCOVERY_DEFAULT_DIRECTORY
DISCOVERY_INCLUDE_ZERO_BYTE_LOGS

REQUIRE_ADMIN_GROUP
AR_ADMIN_GROUP_ID
AR_USER_FORM
AR_LOGIN_FIELD
AR_GROUP_LIST_FIELD
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

Collections include the original downloaded files plus an indexed log-analysis view. The default table is intentionally clean and only shows `Time` and `Message`; additional columns such as level, user, form, event, workflow and field can be enabled from the column picker. Visual Flow renders workflow-oriented logs as a Mermaid sequence diagram focused on Active Link, Filter and Escalation-style events.

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

## Log categorization

Discovered log files are automatically categorized from their filenames and displayed with category/tags in the collect and discovery screens. The classifier supports combined logs such as `ardebug.log`, which can contain API, Filter, SQL, Escalation and User trace output when those AR Server log options are enabled.

See `docs/log-categories.md` for the current category rules.

## Version history


### 0.0.28

- Workflow legend chips in Visual Flow are now clickable filters.
- Visual Flow can include/exclude Client/Form, Active Link, Guide, Filter, Escalation, Service/BackChannel and Error/Warning objects.
- Mermaid actor boxes are post-colored after rendering so exported SVGs also show workflow-type colors.
- Improved visual distinction for object types in workflow diagrams.

### 0.0.27

- Added `Ignore failed qualifications` to filter out workflow branches that did not run.
- Added Mermaid zoom controls for Visual Flow.
- Added `Download SVG` for workflow diagrams.
- Added a workflow legend and grouped Mermaid lanes by workflow type.
- Improved Mermaid dark background handling.

### 0.0.26

- Fixed Custom selection so choosing it clears any previous template-selected logs.
- Added editable collection names from the result view.
- Removed workflow anchors from Visual Flow to give the Mermaid diagram the full width.
- Improved Mermaid dark-mode styling and increased rendered diagram size for readability.
- Cleaned the log table: default Time/Message view is one line per row, without line numbers or tag badges embedded in the message.
- Added stronger single-line table sizing to prevent uneven row heights and make the message column wider.

### 0.0.25

- Fixed paste-only uploads so an empty file input no longer blocks pasted log text.
- Simplified Collect top actions; Collections, Upload and Discovery now live only in the top navigation.
- Changed category quick-picks into a Template dropdown with Custom selection as default.
- Moved Fetch selected logs next to search and template selection.
- Subdued button icons so they match the monochrome/dark UI style.
- Improved log table layout and sticky headers.
- Enlarged Mermaid sequence diagrams, added stronger scroll support, and forced a darker Mermaid theme.

### 0.0.24

- Collections can now be browsed across users from the Collections page using a user menu.
- Removed the recent-collections side panel from the Collect start page.
- Result log view now defaults to only `Time` and `Message`, with a visible-column picker for adding/removing columns.
- Added direct paste support when creating a collection or adding logs to an existing collection.
- Visual Flow uses a darker scrollable Mermaid canvas and exposes workflow value details in notes when detected.
- Added icons to several primary actions for easier scanning.

### 0.0.23

- Förbättrad parser för Mid Tier Active Link-loggar och Progressive Active Link-loggar.
- Visual Flow känner nu igen `EVENT Start/End`, `ActiveLink Start/End`, `True/False actions`, `action N`, `SetFields`, `Change Field`, `Refresh Field`, `Guide Called`, `Exiting Guide`, `ServiceAction`, `BackChannel Request/Response` och klientfel.
- Förbättrad parser för `arfilter.log`: filterfas, filtercheckar, passed/failed qualification och server-side användare/transaktioner.
- Mermaid sequence-diagrammet använder nu mer verkliga AR workflow-lanes, till exempel formulär, active links, guider, filter, service/backchannel och errors.
- Mindre brus i Visual Flow genom att transaktionsmarkörer filtreras bort.

### 0.0.22

- Reworked Visual Flow Mermaid output to a detailed AR workflow-style sequence diagram.
- Participants are now specific workflow objects/forms/guides instead of only broad technical layers.
- Added self-messages for set-field style operations and clearer call/return-style event labels.
- Visual Flow remains limited to workflow-oriented logs such as AR Filter, Active Link, Progressive Active Link and Escalation logs.

### 0.0.21

- Aligned the HLX Logs color palette and control styling more closely with the provided HLX Migrator CSS reference.
- Fixed the result log table header so it no longer overlaps the first log rows in the result view.
- Limited Visual Flow to workflow-oriented logs only: AR Filter, Active Link, Progressive Active Link and Escalation-style logs.
- Updated the Mermaid sequence/swimlane lanes to focus on Client/API, Active Link, Filter/Guide, Escalation, SQL, Errors and System stages.
- Added explanatory text in Visual Flow so it is clear why general AR error/debug/plugin logs are excluded from that diagram.

### 0.0.20

- Removed the large summary metric cards from the result page to reduce visual noise.
- Moved Upload additional logs into a top-level collection action button beside Download/Delete/Collect more.
- Tightened result page layout to avoid overlapping filters, buttons and the log table.
- Reworked the Visual Flow Mermaid output from a wide horizontal flowchart into a sequence/swimlane-style stage diagram.
- The new visual flow follows movement between Client/API, Workflow, SQL, Plug-ins, Errors and System stages, including back-and-forth transitions.

### 0.0.19

- Added a SQLite index per collection for faster result filtering and rendering.
- Result view now queries indexed rows instead of loading all parsed rows into memory for every search/filter change.
- Added a global loading overlay for slower result/filter/upload/delete actions.
- Restored the application logo/progress icon to the original image rendering with a black background instead of cropped assets.
- Improved result page layout so header buttons, filters and tables do not overlap as easily.
- Visual flow now includes a Mermaid swimlane/stage-style diagram with lanes for Client/API, Workflow, SQL, Plug-ins, Errors and System events.
- Added Copy Mermaid action and a Mermaid source fallback when the browser cannot load the Mermaid renderer.

### 0.0.18

- Reintroduced log content analysis in a dedicated result interface.
- Added Log view with search, user, form, file, level and time-from/to filters.
- Added Visual flow view for ordered API, Filter, Escalation, SQL, Active Link-like, plugin and error events.
- Added upload of additional logs directly into an existing collection.
- Collections now store parsed analysis rows while retaining individual and Download all log downloads.

### 0.0.17

- Added automatic log file categorization for discovered AR/Helix log files.
- Added multi-tag classification so combined logs such as `ardebug.log` can be found by API, Filter, SQL, Escalation and User trace presets.
- Added category/tag columns in Discovery.
- Added additional preset buttons in Collect: Critical, Trace, SQL, Filter, Plugin, Web/REST and Deployment.
- Added `docs/log-categories.md`.

### 0.0.16

- Removed manual pod/log-file configuration from the user flow.
- Added automatic discovery of pod/server names from `AR System Configuration Component Setting`.
- Added automatic discovery of available log files per pod from `AR System Server Group Logs`.
- Added configurable discovery interval and a manual Discovery refresh page.
- The collect page now uses only discovered pods and discovered log files.
- Added discovery status, warning reporting and environment overrides for discovery settings.

### 0.0.15

- Added `buildah-script.sh` for Buildah-based image builds.

### 0.0.14

- Removed log content viewing, searching, filters, timeline modes and parser-driven result tables from the active user flow.
- Collection detail now focuses on metadata, warnings, individual downloads, `Download all logs`, and deletion.
- Upload remains available as a downloadable collection flow.

### 0.0.13

- Reworked progress icon asset crop.
- Simplified visible result filters before log content viewing was removed.

### 0.0.12

- Added upload flow for user-provided log files and zip files.

### 0.0.11

- Added time-focused log filters and improved parser heuristics before log content viewing was disabled.

### 0.0.10

- Collection jobs continue when an individual log fails, reporting warnings instead of aborting the whole job.

### 0.0.9

- Added Administrator group validation at login.
- Added multi-user collection ownership.
- Added delete actions.
- Added `Download all logs` as a flattened zip.

### 0.0.8

- Changed manifests to use temporary `emptyDir` storage for `/data`.

### 0.0.7

- Reworked UI toward the HLX Migrator style.
- Added stored collections, progress view and retention.

### 0.0.6

- Added dark mode UI, app icon and common AR Server log configuration.

### 0.0.5

- Changed default AR REST base URL to `http://ars-arserver:8008`.

### 0.0.4

- Changed default attachment field to `1EX`.
- Attachment download uses `/api/arsys/v1/entry/{formName}/{entryId}/attach/{fieldName}`.

### 0.0.3

- Removed invalid `Request ID` field from `HLX:Logs` transaction queries.
- Display signed-in user next to logout.

### 0.0.2

- Renamed `Dockerfile` to `Containerfile`.
- Changed application port to 8095.
- Added version history.

### 0.0.1

- Initial FastAPI MVP.
- Login through AR REST JWT.
- POST log requests to `HLX:Logs`.
- Download attachments.
