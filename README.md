# physio-data-platform

Automated processing for the lab's hospital physiological data (~1 TB, ~22 projects):
raw signals in Dropbox → cleaned Parquet + features + statistics → dashboard feed
for the lab website. Runs without depending on any one computer.

## Architecture

```
Dropbox (raw files) 
    │  ingest.py — download new/changed files, manifest with content hashes
    ▼
staging/<project>/raw/
    │  runner.py — per-patient adapter call, failures isolated per patient
    ▼
processed/<project>/<config_fingerprint>/<patient_id>/
    signals.parquet      cleaned signals (one row per sample)
    features.parquet     per-window / per-phase metrics
    figures/*.png        per-patient graphs
    patient_log.json     provenance for this patient
processed/<project>/<config_fingerprint>/
    cohort_stats.parquet cross-patient statistics
    run_manifest.json    code version + config fingerprint + per-patient status
    │  website.py — copy figures + JSON index for the site
    ▼
website/<project>/feed.json (+ figures/)
```

**One shared pipeline, 22 configs.** Each project is a YAML file in `projects/`;
the code never forks per project. Changing a config changes its fingerprint, so
results from different configs land in different directories and never mix.

**Deterministic.** Fixed seeds, no adaptive per-run decisions. Every output can
be traced to the exact code version + config that produced it (`run_manifest.json`).

## Quickstart

```bash
pip install -r requirements.txt
export DROPBOX_TOKEN=...   # only needed for --ingest

# full run for one project
python run.py --project intraop-eeg --ingest --process --stats --feed

# re-run only processing (uses already-staged files)
python run.py --project intraop-eeg --process --stats --feed
```

## Adding a project

1. Copy `projects/_template.yaml` to `projects/<name>.yaml` and fill in the
   Dropbox folder, sampling rate, channels, and filter/normalization notes.
2. Write an adapter in `adapters/` with this signature:

   ```python
   def process_patient(raw_path: str, cfg, out_dir: str, log) -> dict:
       ...
       return {"n_samples": ..., "duration_s": ...}
   ```

   Use `physio_platform.store.write_signals / write_features` for the Parquet outputs
   and save per-patient PNGs under `<out_dir>/figures/`.
3. Point `processing.adapter` at `"adapters.<name>:process_patient"`.
4. Test on one de-identified patient before running the cohort.

## Project status

| Project | Config | Adapter | Status |
|---|---|---|---|
| intraop-eeg | `projects/intraop-eeg.yaml` | `adapters/intraop_eeg.py` (wraps `eegpipe`) | scaffolded, needs real Dropbox path + trial run |

## Roadmap

- [ ] Fill in real Dropbox paths for all projects; trial-run one de-identified patient end to end
- [ ] Adapters for the remaining signal types (ECG, blood pressure, respiration, medications...)
- [ ] Cross-project statistics and comparisons
- [ ] Move compute to Azure (container job on a schedule), storage to encrypted Azure Blob
- [ ] Website dashboard reading `website/*/feed.json`

## Privacy

This repository contains **no patient data**. Only aggregated/de-identified
outputs (figures, statistics) are published to the website feed. Raw and
processed patient-level data stay in access-controlled storage. Work with
de-identified samples unless the REB/privacy authority has approved otherwise.
