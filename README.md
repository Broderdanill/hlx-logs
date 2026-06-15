# hlx-logs

`hlx-logs` is a Python/FastAPI application for collecting BMC Helix / AR System log files through the `HLX:Logs` AR REST form and making the downloaded log packages available for individual or combined download.

The result interface provides a log-analysis view and a visual flow view while still keeping the original downloaded files available individually and as a combined zip.

## Current version

**0.0.63**

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

## AR log control

The collect page includes **Enable all logs** and **Disable all logs** buttons. These submit to the BMC display/service form `AR System Server Group Log Management`, following the same service-form workflow pattern used by BMC's bundled `AR System Server Group Log:SetLogs` active link.

The same action is available as REST from this app:

```bash
curl -X POST http://localhost:8095/api/log-control/all \
  -H 'Content-Type: application/json' \
  -d '{"action":"enable"}'

curl -X POST http://localhost:8095/api/log-control/all \
  -H 'Content-Type: application/json' \
  -d '{"action":"disable"}'
```

The call uses the signed-in user's AR-JWT and requires the normal app session/authentication.

## Log categorization

Discovered log files are automatically categorized from their filenames and displayed with category/tags in the collect and discovery screens. The classifier supports combined logs such as `ardebug.log`, which can contain API, Filter, SQL, Escalation and User trace output when those AR Server log options are enabled.

See `docs/log-categories.md` for the current category rules.

## Version history

### 0.0.58
- Polished the Jira-inspired dark theme with calmer hover/active states for buttons, inputs and top navigation.
- Fixed top-left brand alignment and spacing around the app icon/version.
- Added a little more breathing room to compact panels, rows and Log settings without returning to the old bulky layout.
- Improved disabled/read-only visual treatment for inactive Log settings filename and user-restriction fields.

### 0.0.57
- Compact one-row Collect toolbar: search, Hide 0 KB, template and Fetch stay together.
- Inactive Log settings filename fields are read-only/disabled until the log type is enabled.
- Restrict-Log-Users text field is disabled when the restriction toggle is off.
- Jira-inspired dark theme trial: neutral dark surfaces, blue accent, lower cyan intensity and tighter spacing.

### 0.0.56
- Replaced Log settings checkboxes with compact boolean-style switches.
- Removed the Back to collect button from Log settings.
- Added subtle icons to the top navigation links.
- Added a Hide 0 KB filter beside the log search field on Collect.
- Improved Log settings spacing and switch alignment.
- Improved Discovery scrolling and compact table styling.

### 0.0.55
- Removed the top navigation Upload link; uploads are now available from the Collect page as a second create-collection tab.
- Collect page now offers two creation modes: Fetch from AR (default) and Upload / paste logs.
- Cleaned up the Log settings layout into clearer grouped controls for base pod, target pods, template/save, restrict users, and log rows.
- Improved Discovery page scrolling and compact table styling so long discovered log lists remain accessible and more consistent with the Collect page.

### 0.0.54
- Fixed filename setting updates by using the secondary row id from the join entry id before falling back to the configuration GUID.
- Physical configuration row lookup no longer requests guessed value-field aliases that can make AR reject the query.
- Added `Restrict-Log-Users` support in Log settings with checkbox plus semicolon-separated user field.
- When Restrict-Log-Users is unchecked, the app attempts to delete that setting row for selected target pods.

### 0.0.53
- Filename settings are now saved only for enabled log types.
- Filename settings are only posted when the value changed from the value loaded in the UI.
- Prevents Save from re-writing every known `*-Log-File` setting and avoids errors for inactive/missing settings such as `DSO-Log-File`.

### 0.0.52

- Log settings can now save filename settings for AR server logs.
- Added filename setting mappings such as API-Log-File, Filter-Log-File, SQL-Log-File, Thread-Log-File, Alert-Log-File and related supported log types.
- Save log settings now writes Debug-mode plus non-empty filename fields for every selected target pod.
- Log settings loads current filename values from the selected/base pod when available.

### 0.0.51
- Improved Log settings target pod checkbox layout.
- Save log settings now shows the global loading overlay while REST updates run.
- Discovery page layout adjusted to avoid horizontal scrolling and keep columns readable.

### 0.0.50
- Improved AR log settings Debug-mode save for environments where the physical `AR System Configuration Setting` form rejects numeric field-id updates.
- Tries physical value field aliases such as `Value` before falling back to numeric field id `3205`.
- Reads the physical row to discover REST-exposed value field names where available.
- Keeps verification after save; success is only reported when the join view reads back the requested Debug-mode value.

### 0.0.49
- Fixed AR log settings Debug-mode update for physical AR System Configuration Setting form.
- Uses numeric field id 3205 for Setting Value when writing the underlying configuration row.
- Queries the physical configuration row by numeric field id 179 instead of join-form field labels.
- Keeps verification after save; success is only reported when the join view reads back the requested Debug-mode value.

### 0.0.48
- Changed Log settings Debug-mode save to update the underlying `AR System Configuration Setting` row first.
- Reads through `AR System Configuration Component Setting`, but writes the secondary configuration setting row because that form is a join form.
- Verifies the value by re-reading after every save before reporting success.
- Reports a warning if AR accepts the request but the visible Debug-mode value did not actually change.

### 0.0.47
- Fixed Debug-mode save failures where direct PUT on `AR System Configuration Component Setting` can return database-column errors such as `column t25.e3 does not exist`.
- Log settings now first tries normal AR REST PUT, then falls back to AR REST `mergeEntry` with the Debug-mode qualification.
- PUT payload now includes `Setting Name`, `Component Type`, and `Component Name` together with `Setting Value` so AR workflow has the row context.

### 0.0.46

- Fixed Log settings crash caused by reading `PodConfig.name`; discovered/static pods use `PodConfig.id`.
- Log settings now supports selecting multiple target pods.
- Save log settings writes the selected Debug-mode bitmask once per selected pod/server.
- Added a target-pod panel showing each pod's current Debug-mode value or read error.
- Templates still update the log-type checkboxes; filename fields remain visible but are not written yet.

### 0.0.45

- Added functional Log settings save for AR Server Debug-mode bitmask.
- Log settings now reads and writes `Setting Value` on `AR System Configuration Component Setting` where `Setting Name = Debug-mode`, `Component Type = com.bmc.arsys.server`, and `Component Name = selected pod/server`.
- Added server/pod selector and current Debug-mode value display.
- Added templates such as Filter, Workflow trace, SQL + Filter, API, Performance / SQL, Server diagnostics, and All supported debug logs.
- Kept filename fields visible for the next implementation step, but they are not written yet.


### 0.0.44

- Fixed the Log settings page so the common Save log settings button is visible.
- Kept Log settings separated from Collect, which only lists available log files to fetch.
- Added a sticky settings help/action bar so the save action stays easy to find.


### 0.0.43

- Restored separation between Collect logs and AR log settings.
- Collect logs now only lists already-discovered/fetchable log files with filename and size columns.
- Added a separate Log settings page for toggling AR log types and editing their future log filenames.
- Save log settings now posts changed log-setting rows from that separate page instead of mixing settings into the fetch list.

### 0.0.42

- Replaced per-row AR log-control Save buttons with one Save log settings button.
- The Collect page now posts the complete current toggle/filename state for all known AR log types.
- Rows with changed log-control fields are marked subtly until saved.
- Kept backward-compatible handling for the 0.0.41 single-row submit format.

### 0.0.41

- Replaced broad all-logs controls on the collect page with per-log controls.
- Each known AR log type can now be toggled on/off individually and saved with a filename.
- Added `POST /logs/control/save` for the UI, using `AR System Server Group Log Management` fields for the selected log type.
- Collect page keeps file size as a dedicated column and adds filename/toggle/save controls per log row.
- The old all-logs REST endpoint is still present for API compatibility, but the UI now uses individual log saves.

### 0.0.40

- Added AR REST log-control actions from the collect page: Enable all logs and Disable all logs.
- Added REST endpoint `POST /api/log-control/all` with JSON body `{"action":"enable"}` or `{"action":"disable"}`.
- The log-control call mirrors the BMC `AR System Server Group Log Management` service workflow from the imported definition.
- Log file size is now shown as its own column on the collect page instead of being embedded in the description text.

### 0.0.39

- Reworked focused filter transaction visualization after deeper AR filter-log analysis.
- The diagram is now grouped by AR filter-processing frames instead of drawing one large node network.
- Top-to-bottom order follows the selected AR `TrID` from the filter log.
- Each frame shows input operation, executed IF/ELSE filters, key actions/outputs, and hidden skipped checks.
- Plain failed qualifications are hidden when requested, while `Failed qualification -- perform else actions` is treated as an executed ELSE branch.
- Repeated filter-guide/service executions are shown as repeated frames in the same AR transaction trace.

### 0.0.38

- Fixed Mermaid zoom buttons in the focused filter transaction flow.
- Removed the general log filter bar from the focused visual flow view.
- Visual flow now shows a transaction-focused heading with selected AR TrID and detected user when available.
- Flow generation ignores unrelated table filters so the selected transaction does not become incomplete after navigating back and forth.

### 0.0.37

- Reworked Filter transaction flow to build a compact Mermaid flowchart from rows sharing the selected AR filter TrID.
- The flow now connects filter checks/actions by the AR log transaction id instead of drawing a huge participant-heavy sequence diagram.
- Reduced Mermaid parser failures by sanitizing node labels and limiting low-level repeated GET/SET/API rows to summary nodes.

### 0.0.34

- Fixed delayed loading overlay so it does not stay visible when returning to already-rendered views.
- Reworked focused filter transaction Mermaid generation to summarize workflow actions instead of rendering huge raw-row diagrams.
- Added Mermaid `maxTextSize` configuration for larger local diagrams.

### 0.0.33

- Compact filter transaction flow generation.
- Discovery layout fix.


### 0.0.32

- Fixed an Internal Server Error in the results view caused by an undefined `tx` variable in SQLite row filtering.
- Transaction-focused filtering now works consistently for both Log view and focused Visual Flow.

### 0.0.31

- Visual Flow is no longer a general result tab; it is opened from a clickable Transaction ID in Log view.
- Focused flow currently targets `arfilter` rows with a matching `TrID`, so the diagram represents one server-side filter transaction instead of a broad mixed workflow overview.
- Log view now shows the Transaction column by default and turns filter-log transaction ids into flow links.
- Removed the redundant `Collect more` action from the collection/result header.
- Pressing Enter in the Collect page `Filter logs...` field is ignored so it no longer accidentally submits the fetch form.

### 0.0.30

- Collect page now sorts pods and log files alphabetically by name.
- Log file size from `AR System Server Group Logs` is shown in the Collect list.
- Collection fetches are processed with a configurable concurrency limit (`AR_COLLECT_CONCURRENCY`, default `4`) to reduce blocking when multiple users work at the same time.
- Fixed parsed row metadata when the same filename is fetched from multiple pods; pod and filename are now matched from the exact download source.
- Mermaid is now served locally from `/static/mermaid.min.js` instead of loading from the public CDN.

### 0.0.29

- Simplified the pod list on the Collect page to show only the pod name.
- Changed the Collect search placeholder to `Filter logs...`.
- The Collect search now filters only log files; pods always remain visible and selectable.


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

### 0.0.63
- Changed collection flow to download/save log packages first without parsing or indexing.
- Added explicit Analyze logs action that parses files and builds the SQLite search index on demand.
- Newly uploaded extra logs mark the collection as pending analysis to avoid showing stale indexed data.

### 0.0.62
- Converted the Collect log selector into a cleaner table-like grid with separated headers and aligned columns.
- Added a Jira-dark compatible loading overlay with binary stream animation and the app genie icon.
- Made Restrict-Log-Users disable/delete idempotent when the setting row is already missing.
- Continued visual polish for Discovery settings, checkboxes/toggles and Mermaid colors.

### 0.0.61
- Refined Jira-like UI details: badge alignment, pod/toggle spacing, button icon consistency, Discovery settings table colors, and Mermaid/visual flow theming.
- Polished Collections and Upload action buttons to use monochrome UI icons instead of emoji glyphs.

### 0.0.60
- Reworked Log settings switches and rows for a finished, Jira-like layout.
- Applied a broad pixel-polish pass to tables, upload forms, collections, result views, discovery and Mermaid visuals.
- Fixed floating/sticky table headers so rows do not visibly bleed above headers during scroll.
- Kept the new genie icon and aligned it consistently across app, favicon and loading/progress UI.
