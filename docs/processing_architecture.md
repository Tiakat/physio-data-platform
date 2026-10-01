# Processing architecture: the 23 phases on Azure

Source: K's reproducible physiological-data pipeline spec (2026-10-01).
Principle: **never modify the raw data, never apply the same processing to
every column, never discard information because it looks unusual.**

Dropbox is the immutable original-data source only. Every processing step
runs in GitHub Actions and lands, encrypted, in Azure. Dropbox is never
written to.

## The three data levels (never collapsed)

| Level | Content | Azure location | Rule |
|---|---|---|---|
| 1 — RAW | Standardized parquets, one per source file, Fernet-encrypted | `rawdata/<CODE>/parquet/*.parquet.enc` | Written once by Stage 1. Never overwritten. |
| 2 — PROCESSED | Per-patient signals: `<sig>_raw`, `<sig>_filtered`, `<sig>_quality` side by side | `processed/level2/<CODE>/<patient>/` | QC flags, artifact masks, filtered copies. Raw always retained. |
| 3 — DERIVED | Features, patient statistics, project statistics, graphs | `processed/level3/<CODE>/` | Built from Level 2 only. |

Plaintext is allowed only for de-identified counts/status/digests under
`reports/` and `processed/stage2/digest_*.json`. Everything else encrypted.

## Phase map

| # | Phase (K's spec) | Implementation | Status |
|---|---|---|---|
| 1 | Dataset discovery | Stage 1 walks all 10 projects; supervisor audits layout | live |
| 2 | File inventory + hashes | Ingest state per file (size, rev, SHA-256 before encryption) | live |
| 3 | Complete column inventory | `tools/recon_schemas.py` header-only census → 5 CSVs | running |
| 4 | Patient/source identification | `patient_dir_regex` per profile; device tiers | live |
| 5 | Column canonicalization | `resolve_columns()` + `profiles/_variables.yaml`; original vs canonical kept separate; duplicates never collapsed | live |
| 6 | Time / sampling-rate detection | Per-signal Δt from timestamps: nominal vs observed rate, median/min/max Δt, irregularity | planned |
| 7 | Missing-data analysis | 5-type taxonomy (isolated, short gap, long gap, entire variable, structural); not-recorded ≠ not-applicable ≠ missing ≠ invalid | planned |
| 8 | Signal classification | Categories A–I: time, raw waveform, parameter, derived index, medication/exposure, event, metadata, demographic, unknown. Unknown → review queue, never processed | planned |
| 9 | Physiological validity QC | Per-variable validity ranges from config; out-of-range → flagged, not deleted | planned |
| 10 | Signal-specific artifact detection | Range + rate-of-change + local context + morphology + device quality indicators + duration + neighboring channels | planned |
| 11 | Signal-specific filtering | `configs/signals/<signal>.yaml`: ECG, PPG, arterial pressure, respiration, HR, SpO₂, BIS, NOL, pump — each its own method, no generic Hampel-everything | planned |
| 12 | Filtered-data generation | Level 2: `<sig>_raw`, `<sig>_filtered`, `<sig>_quality` | planned |
| 13 | Normalization where justified | Per-variable policy in signal configs (z-score / baseline / none). HR/SpO₂/ART/BIS/dose keep native units unless justified | planned |
| 14 | Feature extraction | ECG→HRV, pressure→SBP/DBP/MAP, PPG→pulse features, respiration→RR/breath features, NOL→baseline/max/AUC, BIS→thresholds — only scientifically justified features | planned |
| 15 | Patient statistics | n, valid_n, missing %, mean, SD, median, IQR, min, max + signal duration/artifact % | planned |
| 16 | Patient graphs | Raw vs filtered overlays, full recording, zoomed windows, QC flags; only primary variables graphed by default (config-controlled) | planned |
| 17 | Within-project analysis | Patient-level feature tables → project summaries | planned |
| 18 | Project graphs | Distributions, boxplots, time-series comparisons, justified correlations only | planned |
| 19 | Missing-data report | Per project: patient file matrix, column missingness, gaps, structural missingness, file problems | planned |
| 20 | Methods/reference report | Per project: sources, definitions, rates, units, QC rules, filters, stats, corrections, software versions, literature | planned |
| 21 | Validation | Row counts, range checks, expected outputs exist, no silent failures (`STATUS=FAILED`, never continue silently) | planned |
| 22 | Optimized outputs | Parquet for all numerical Levels 2–3 | live (L1), planned (L2–L3) |
| 23 | Dashboard/API integration | Website consumes validated aggregates from `reports/feed/` only. No scientific preprocessing on the website | feed live; validation gate open |

## Gating rule

Phases 1–8 run across **all projects** before any Phase 9+ work.
The schema dictionary is the gate: no filtering configuration is defined
until the inventory shows what actually exists. This is what prevents the
generic-filter-everything failure mode.

## Cross-source validation (designed into Phase 10)

Where two sources observe the same physiology, derive from the waveform
and compare against the monitor parameter:

- BetterCare ECG → derived HR vs Infinity HR
- BetterCare arterial waveform → derived MAP vs Infinity ART M

Discrepancies become QC flags on the period, not automatic corrections.

## Processing log

Every run records: run_id, timestamp, project, patient, source file,
file hash, software version, configuration version, filter + parameters,
normalization, status. Stored encrypted with the outputs.

## Tests (planned, per spec)

`test_discovery`, `test_schema`, `test_time`, `test_missing_data`,
`test_ecg`, `test_ppg`, `test_pressure`, `test_normalization`,
`test_statistics`, `test_reports` — including: filtering preserves
timestamps, raw is never modified, NaN never becomes zero, duplicate
columns detected, 200 Hz vs 1 Hz distinguished.
