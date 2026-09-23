# Platform phases

| # | phase | status | doc |
|---|---|---|---|
| 1 | Inventory every Dropbox project + classify files by name | ✅ code | [PHASE1](PHASE1_INVENTORY_AND_CLASSIFICATION.md) |
| 2 | Reconcile Dropbox ↔ Azure by content hash | ✅ code | [PHASE2](PHASE2_RECONCILIATION.md) |
| 3 | Resumable verified sync of missing files (all projects) | ✅ code, to run on the lab PC | [PHASE3](PHASE3_SYNC.md) |
| 4 | Restructure legacy Azure layout → `Database/<Layer>/<subject>/<source>` (plan → copy → verify → delete) | ✅ code | [PHASE4](PHASE4_RESTRUCTURE_AZURE.md) |
| 5 | Register manifest in PostgreSQL catalog (`projects/subjects/sessions/sources/files`) | next | |
| 6 | Extraction with provenance (raw_file → transformation → extracted_file) | | |
| 7 | Standardisation + processing (Parquet, versioned pipelines) | | |
| 8 | QC layers (ingestion, structural, signal, human review) | | |
| 9 | Web / analysis interface | | |

Rules: RawData is immutable · identity = content hash · nothing is deleted before
reconciliation + hash validation · project specifics live in `config/`, not in code.
