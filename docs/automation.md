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

## What still needs a human

1. **Azure access**: the stored SAS gets 403 on every request. Rotate the
   storage key (the 2026-09-30 SAS was pasted in chat) and mint a
   least-privilege credential, or finish setup in portal.azure.com.
   Until then, run stages 1–2 against a local `--out` dir.
2. **ETT sample**: the vendor will provide a test/de-identified dataset.
   Re-run `tools/ingest_ett --dry-run` on it before trusting the mapping.
3. **REB/privacy sign-off** before any identifiable hospital data is
   processed outside the lab's controlled environment.
4. **Scheduler**: pick one — Windows Task Scheduler (today), Azure
   Container Instance on a timer, or Prefect Cloud. The CLIs are
   scheduler-agnostic on purpose.
