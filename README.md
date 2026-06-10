# hlx-logs

FastAPI-based MVP for collecting BMC Helix log zip files through AR REST API and rendering them as searchable, merged timelines.

## What it does

1. Requires login against AR REST `/api/jwt/login`.
2. Lets the user select configured pods and log types.
3. Sends one POST per selected pod/log combination to `HLX:Logs`:

```json
{
  "values": {
    "Pod": "arserver.sandbox",
    "Directory": "/opt/bmc/ARSystem/db",
    "TransactionId": "generated-uuid",
    "Filename": "arjavaplugin.log"
  }
}
```

4. Polls `HLX:Logs` for entries with the same `TransactionId`.
5. Downloads the configured attachment field from each result entry.
6. Extracts zip contents, parses timestamps and levels, merges rows by time, and shows a searchable UI.

## Configuration

Defaults are in `config.yaml` and are designed to be mounted as a ConfigMap at `/app/config.yaml`.
Runtime changes in the UI are in-memory only and are not persisted after restart.

Important environment variables:

| Name | Default | Purpose |
|---|---:|---|
| `AR_BASE_URL` | from config | AR REST base URL, for example `http://platform-user-ext:8008` |
| `AR_FORM_NAME` | `HLX:Logs` | AR form to POST to/query |
| `AR_ATTACHMENT_FIELD` | `ZipFile` | Attachment field that contains generated zip |
| `AR_RESULT_QUERY_TEMPLATE` | `'TransactionId' = "{transaction_id}"` | Query used to find generated files |
| `SESSION_SECRET` | dev placeholder | Secret for signed browser session cookie |
| `LOG_LEVEL` | `INFO` | Python logging level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `CONFIG_PATH` | `/app/config.yaml` | Mounted YAML config path |

## Build locally

```bash
podman build -t hlx-logs:latest .
podman play kube deploy/podman-play-kube.yaml
```

Then open `http://localhost:8080` if you mapped the port locally, or expose it according to your environment.

## Notes

The app assumes your custom Developer Studio logic generates or updates result entries in `HLX:Logs` with the same `TransactionId`, and that the generated zip is available as an attachment field configured by `AR_ATTACHMENT_FIELD`.
