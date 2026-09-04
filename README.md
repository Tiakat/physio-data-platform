# Lab Research Data Platform

Ingestion, validation, standardisation, preprocessing, quality control and visualisation
of clinical recordings from six research projects.

## The five design decisions, and why

### 1. Not everything is a CSV. Three tiers, never a failure.

Every file that arrives is registered, checksummed and stored. What happens next depends
on its tier, declared in the project profile.

| Tier | Files | Behaviour |
|---|---|---|
| A, parsed | Infinity CSV, BetterCare CSV, NOL ExcelData CSV, pump CSV | Full pipeline |
| B, kept | .med, .enc, .ara, .o_a, .m_a, .pdf, .jpeg | Registered, stored, checksummed, indexed, NOT parsed |
| C, ignored | .DS_Store, Thumbs.db, ~$ temp files | Logged and skipped |

A tier B file is **not** an ingestion failure. It appears in the database with
`parse_status = 'not_supported'` so it is findable, and a parser can be added later
without re-ingesting. This is the rule that stops one PDF from breaking a patient.

### 2. Preprocess only what has a declared schema.

Preprocessing runs when, and only when, the project profile declares a schema for that
file type. NOL has a schema, so NOL is preprocessed. Photographs do not, so they are
stored and indexed but never parsed. Adding BIS later means writing a parser and adding
six lines to a profile, not changing the pipeline.

### 3. Naming chaos is absorbed by profiles, not by scripts.

One pipeline. One YAML per project declaring folder patterns, file patterns, column
aliases, delimiter, date format, missing value codes and quality thresholds.
`discover.py` scans the archive and tells you what to put in the YAML.

### 4. Missing modalities: never silently compare. Record coverage explicitly.

The database holds a **coverage matrix**: for every patient, which modalities exist.
Cohort queries filter on required modalities *first*, and every result carries its
denominator.

```
Cohort: IPAMS, age > 60
  38 patients in project
  31 have Infinity
  27 have BetterCare
  24 have both          <- analysis runs on these 24, and says so
```

A patient missing BetterCare is not an error and is not dropped from the project. It is
dropped from analyses that require BetterCare, and the platform states how many were
dropped and why. Never impute a missing device.

### 5. Patients with only NOL or only BIS are NOT excluded at validation.

Validation answers "is this file intact and readable". It does not answer "is this
patient scientifically useful". Those are different questions and conflating them loses
data permanently.

- Validation: per file. Passes or fails on integrity.
- Coverage: per patient. Recorded as fact.
- Inclusion: per analysis. Decided by the cohort filter at query time.

A patient with only BIS stays in the database with `coverage = {bis}`. If a study needs
Infinity, the cohort filter excludes that patient and reports it. If a later study needs
only BIS, that patient is available. Excluding at ingestion would bake one study's
assumptions into the platform forever.

## Quick start

```bash
cp .env.example .env          # edit passwords
docker compose up -d          # postgres, minio, prefect, streamlit
python -m backbone.discover --root /data/incoming --out reports/
```

Open:
- Streamlit app: http://localhost:8501
- Prefect:       http://localhost:4200
- MinIO console: http://localhost:9001

## Order of work

1. `discover.py` on the whole Dropbox archive. Read the report.
2. Write or correct one profile per project.
3. Run the pipeline on one project. Check the QC queue.
4. Repeat for the other five.
5. Compare automated output against your manual results before trusting it.
