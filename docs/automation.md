# Automation: Dropbox → Azure → website

This is the target architecture (decided 2026-09-30). Raw data stays in
Azure after all; the website only ever sees de-identified aggregates.

```
┌─ Existing data ──────────────────────────────────────────────┐
│ Dropbox (/Liam/Projets actifs)                                │
│   └─ tools/sync_dropbox_cloud.py  →  Azure rawdata/          │
│      (cloud-to-cloud, Dropbox refresh token + Azure creds)   │
└──────────────────────────────────────────────────────────────┘
┌─ Future ETT data ────────────────────────────────────────────┐
│ BioDASh → ETT → Dropbox parquet_zip/  (today, manual sync)   │
│ BioDASh → ETT → Azure Blob direct     (later, service         │
│   principal OAuth2; ETT customization, unattended sync)       │
└──────────────────────────────────────────────────────────────┘
                          │
                          ▼
              Azure Blob (labdataplatform)
              ├── rawdata/ett/<project>/<session>/<exam>/source.parquet.zip
              │     immutable, byte-identical delivery
              ├── processed/<project>/<exam_guid>/standardized/
              │     parquet, nullable superset schema, sorted, sentinels nulled
              ├── processed/<project>/<exam_guid>/features/stats.parquet
              ├── processed/<project>/comparatives/      (cross-patient)
              └── reports/feed/feed.json                (website JSON, public-read)
                          │
                          ▼
              liam-v2 /[locale]/physio-data   (PHYSIO_FEED_URL → feed.json)
              existing /dashboard untouched
```

## Stage 1 — Dropbox → Azure (existing data)

`python -m tools.sync_dropbox_cloud` — cloud-to-cloud copy, Dropbox
refresh-token OAuth + Azure credentials from `.env`. State in `sync_state/`,
reports in `sync_reports/`. Run on a schedule (Windows Task Scheduler today;
a cron/ACI job when Azure access is fixed).

## Stage 2 — ETT ingest (new data)

`python -m tools.ingest_ett --source <dir> --out <store> [--project CODE]`

- Source today: the Dropbox `parquet_zip/` folder (synced to Azure first,
  or read from a local Dropbox mirror). Later: Azure Blob prefix directly.
- Project mapping: `config/ett_sources.yaml` (ETT target folder → project).
- Per delivery: ZIP CRC32 → GUID cross-check (filename vs footer) →
  idempotency on `exam_guid` → parse → validate → features → manifest →
  catalog. Failures go to `quarantine/<reason>/` with a `.reason.txt`.
- Standardized parquet carries **no patient_id and no free text**; those
  live only in the internal `catalog.json` (access-controlled).
- Prefect version for Azure: `flows/ett_ingestion_flow.py`
  (discover → verify → ingest → feed).

## Stage 3 — website feed

`python -m tools.build_feed --store <store> --out reports/feed`

Aggregates only, k-anonymity k=5 suppression. Upload `reports/feed/` to the
Azure `reports` container with public read (or behind the site's proxy);
set `PHYSIO_FEED_URL` in liam-v2's `.env` to the `feed.json` URL.
The physio-data page fetches it server-side, revalidated hourly.

## Running it on a schedule, inside Azure (production)

`python -m tools.daily_pipeline` runs all three stages unattended:

1. mirrors new `*.parquet.zip` from Dropbox → `rawdata/ett/incoming/`
   (immutable, sha256 in blob metadata),
2. ingests them with `tools/ingest_ett` → uploads `processed/<project>/<exam>/...`,
3. rebuilds the feed with `tools/build_feed` → publishes `reports/feed/*.json`.

State lives in `processed/_pipeline/state.json`; re-runs only touch new
deliveries. Auth uses `tools/azure_auth.py`: storage key → SAS → 
`DefaultAzureCredential`. On a workstation run `az login` once and no
secret is needed at all.

Recommended deployment — **Azure Container Apps Job** with a cron trigger
(`0 12 * * 1-5`, i.e. 08:00 EDT Mon–Fri; note cron is UTC so winter runs at
07:00 EST unless adjusted) and a **system-assigned managed identity** with
"Storage Blob Data Contributor" on `labdataplatform`. The job then needs no
SAS and no key — managed identity is the permanent fix for the 403s. Startup
command clones this repo and runs `python -m tools.daily_pipeline`; Dropbox
credentials go in as job secrets.

## What still needs a human

1. **Azure access**: the stored SAS gets 403 on every request. Rotate the
   storage key (the 2026-09-30 SAS was pasted in chat) and mint a
   least-privilege credential, or finish setup in portal.azure.com.
   Until then, run stages 1–2 against a local `--out` dir.
   **Permanent fix**: deploy the Container Apps Job with a managed identity
   (above) — SAS tokens disappear from the picture entirely.
2. **ETT sample**: the vendor will provide a test/de-identified dataset.
   Re-run `tools/ingest_ett --dry-run` on it before trusting the mapping.
3. **REB/privacy sign-off** before any identifiable hospital data is
   processed outside the lab's controlled environment.
