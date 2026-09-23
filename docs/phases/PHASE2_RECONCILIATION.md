# Phase 2 — Reconcile Dropbox ↔ Azure

**Goal:** know exactly which Dropbox files are already safely in Azure, which are
missing, and which disagree — by **content**, not by file name. Nothing is
uploaded, moved or deleted in this phase.

```powershell
python -m physio.inventory_dropbox        # phase 1a
python -m physio.inventory_azure          # phase 1b
python -m physio.reconcile                # -> reports/phase2/
```

## Identity

| where | identity used |
|---|---|
| Dropbox | `content_hash` returned by the API (SHA-256 over 4 MB block digests) — free, no download |
| Azure (new uploads) | blob metadata `dropbox_content_hash` + `sha256` written by `physio.sync` |
| Azure (legacy uploads, no metadata) | path + size → `MATCHED_SIZE`, upgraded by `physio.sync --verify-legacy`, which re-hashes the blob |

## Statuses (one per in-scope Dropbox file)

| status | meaning | action |
|---|---|---|
| `MATCHED` | an Azure blob carries the same content hash | none |
| `MATCHED_SIZE` | same path (canonical or legacy `PROJECT/<subject>/…`) and size, no hash on blob | verify later |
| `MISSING` | not in Azure | `upload`, or `review` if subject/source unknown or project in `review` |
| `CONFLICT` | same path, different size or hash | human decides — never overwritten |
| `DUPLICATE` | same bytes as another Dropbox file | `link` (uploaded once, catalogued twice) |
| `AZURE_ONLY` (separate file) | blob that no Dropbox file explains | investigate before any cleanup |

## Outputs (`reports/phase2/`, git-ignored)

* `reconciliation.csv` — the upload plan (one row per Dropbox file, with `target_path`)
* `reconciliation_summary.md` — counts per project, GB to upload, review reasons
* `azure_only.csv`

## Target path

Missing files go to the canonical layout proposed in phase 1:
`PROJECT/Database/<RawData|ExtractedData|ProcessedData|Metadata>/<subject>/<source>/…`.
If two *different* files would land on the same name, the original Dropbox
sub-path is kept under `<subject>/<source>/` so nothing collides.

Existing blobs in the legacy layout (`PROJECT/<subject>/…`) are **recognised, not moved**;
moving them is a later phase, after `--verify-legacy` has hashed them all.
