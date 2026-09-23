# Phase 3 — Resumable, verified Dropbox → Azure sync

Replaces `tools/sync_dropbox_cloud.py` (kept for history, do not use).

## What was wrong with the old sync

| problem | effect | fix in `physio.sync` |
|---|---|---|
| only listed `<project>/Database/RawData` | 15 of 23 Dropbox folders never synced | driven by `config/projects.yaml` data roots + reconciliation plan |
| project errors printed then skipped | failures invisible | every attempt logged; `UNCONFIGURED` folders reported |
| state/report written once at the end, overwritten each run | crash = no record; no resume | append-only `data/_ingestion/manifests/ingestion_manifest.jsonl`; VERIFIED files skipped on re-run |
| non-ASCII metadata (`Données`, accents) | Azure rejects the header → upload fails | metadata values URL-quoted |
| download not checked | silent corruption possible | Dropbox `content_hash` recomputed on the stream before upload |
| one file at a time | slow | `--workers 4` (threads) |
| existing blob with different bytes → `.v2-…` side copies | confusing duplicates | logged as `CONFLICT`, nothing written; a human decides |

## Run

```powershell
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
.\scripts\run_migration.ps1                 # inventory + reconcile + dry run
.\scripts\run_migration.ps1 -Upload         # upload MISSING files, retry failures, re-reconcile
python -m physio.sync --verify-legacy       # hash the ~7,100 legacy blobs, stamp metadata
python -m physio.sync --include-review      # only after checking the review list
```

The Windows machine on the UdeM network must run it: the storage firewall only
admits that network (Cloud Shell and other IPs are refused).

## Manifest entry

```json
{"project": "PVB-ABDO", "dropbox_path": "/Liam/Projets actifs/PVB abdo/…/Data_1.pmd.enc",
 "content_hash": "…", "size_bytes": "123", "subject": "11", "source": "NOL", "stage": "RAW",
 "azure_path": "PVB-ABDO/Database/RawData/11/NOL/Data_1.pmd.enc",
 "status": "VERIFIED", "sha256": "…", "timestamp": "2026-09-23T…"}
```

Statuses: `VERIFIED`, `FAILED` (retry with `--retry-failed`), `CONFLICT`.

## Azure Function

`azure/dropbox_sync/function_app.py` in the repo is an incomplete fragment (no imports,
no trigger) and cannot be what runs in Azure. Automatic sync should call the same
`physio` code on a schedule — either a Windows Task Scheduler job running
`scripts/run_migration.ps1 -Upload` nightly, or a timer Function that imports `physio`
(the Function's outbound IP must then be allowed by the storage firewall).
