# Step 1. Dropbox to Azure, and validation

Preprocessing, statistics and the researcher interface are deliberately not in
this step. This step answers two questions only:

1. What is allowed to leave Dropbox and enter Azure.
2. Is what arrived valid, before anything touches it.

## What runs

```
Dropbox                                    researchers keep working as they do now
   |
   |  tools/sync_dropbox.py                selective, incremental, checksum based
   |    include  RawData, project Excel
   |    refuse   AnalyzedData, Documents, ...
   |    report   any folder it has never seen
   v
Azure Blob  rawdata/                       immutable, never modified
   |
   +-- tools/validate_project.py           file level and patient level
   |
   +-- tools/ingest_metadata.py            the Excel, into tidy tables
```

## Rehearse with no Azure account

Everything works against a local folder first, so the rules can be checked
before a single byte goes to the cloud.

```powershell
# nothing is written, nothing is uploaded
python -m tools.sync_dropbox --project IPAMS --dropbox "C:\Users\katia\Dropbox\Liam" --dry-run

# write into a local folder that stands in for the container
python -m tools.sync_dropbox --project IPAMS --dropbox "C:\Users\katia\Dropbox\Liam" --target .\azure_sim
```

When the rules look right, switch the target to Azure by setting two variables
and dropping `--target`:

```powershell
$env:AZURE_STORAGE_ACCOUNT="stlabdata1234"
$env:AZURE_STORAGE_KEY="<key from the portal>"
python -m tools.sync_dropbox --project IPAMS --dropbox "C:\Users\katia\Dropbox\Liam"
```

## What the rules say

All of it lives in `ingest/rules.yaml`. Edit that file, never the Python.

| Under `<project>/Database` | Action |
|---|---|
| `RawData` | transferred, byte for byte |
| `Données <project>.xlsx` and similar | transferred to `_metadata/`, and parsed |
| `AnalyzedData`, `Documents`, `Protocole`, `Scripts`, `Backup`, ... | refused by name |
| `.DS_Store`, `Thumbs.db`, `~$...` | silently ignored |
| anything else | **not transferred, reported, waits for a decision** |

That last row is the important one. A folder the platform has never seen is
never copied on a guess. Tested output:

```
  refused by rule      AnalyzedData, Documents

  ACTION NEEDED, unrecognised folders were NOT transferred:
      NewImportantFolder
      Add each to include_directories or exclude_directories in
      ingest/rules.yaml, then run the sync again.
```

## New patients appear on their own

The sync is incremental by checksum, so it can run on a schedule. Verified
behaviour:

```
run 1                       transferred 6 files      2 new patients
run 2, nothing changed      transferred 0 files      6 already in Azure
researcher adds patient 39  transferred 1 file       1 new patient
                            IPAMS_039  from folder '39'
```

Internal identifiers are assigned once and kept in `sync_state/<project>.json`,
so a folder can be renamed later without the patient changing identity. The
identifier never contains initials, which matters for PROMISES where folders are
named like `Patient 13 20240905 PLL`. The original folder name is kept only as
provenance.

## A corrected file never overwrites and is never skipped

If a file changes content under a name already in Azure, the original stays and
the new version is written beside it:

```
  corrections detected: 1 files changed content under an existing name
      IPAMS/RawData/39/.../20260901090000.infinity.data.OR^^BLOC07.csv
        stored as  ...OR^^BLOC07.v2-f4ad7f6f.csv
      the original is retained; nothing was overwritten
```

## If a sync is interrupted

The state file is an optimisation, not the truth. If a run dies between writing
state and finishing an upload, state claims a file is present when it is not.
Run with `--verify` occasionally, which re-checks the container and re-queues
anything missing:

```
  state repaired       1 entries claimed an upload that was missing, queued again
```

## Validation

```powershell
python -m tools.validate_project --project IPAMS --root .\azure_sim\rawdata\IPAMS --deep
```

Two levels, and they answer different questions.

**File level.** One validator per device type, shared by every project, because a
BetterCare file is a BetterCare file regardless of which study it belongs to.
Each checks: readable, delimiter, the columns that device always carries, a
usable time axis whatever the column is called, duration computed from the
timestamps, row count, and sampling interval against what the device actually
does. Contracts live in `DEVICE_CONTRACT` in `backbone/validate.py`.

**Patient level, and this is the part that adapts per project.** The expected set
of recordings is not hard coded. It is learned from the project by prevalence
and cross checked against the declared expectation in `ingest/rules.yaml`:

```
What this project normally records (3 patients, 60% threshold)
    infinity       100%   expected
    nol             67%   expected
    declared in rules.yaml: infinity, bettercare, nol

Patients   0 complete   3 incomplete
    37   has infinity, nol
    38   has infinity, nol
    39   has infinity     missing nol
```

So a patient missing something 90 percent of the project has is flagged, and a
patient missing something only 20 percent have is not. The same code behaves
differently for PROMISES than for DEXREM with no project specific branch.

Validation never deletes and never excludes. It labels.

## Three outcomes, not two

```
PASS      continues
WARNING   continues, appears in the review queue
FAIL      held, will not survive preprocessing
```

Vendor and binary formats are a fourth, separate category. They are preserved
and counted, and they are **not** failures:

```
Preserved, not validated  2 files, 0.3 MB
    .med         1
    .ara         1
    vendor or binary formats; stored and findable, contents not checked
```

## When a file type has no script

Reported in two places, so it cannot be missed. In the sync:

```
  ACTION NEEDED, files transferred but no parser exists:
      .med         1 files
      .ara         1 files
```

And in validation, if a whole device has no contract:

```
ACTION NEEDED, no validator exists for these devices:
    unknown      add a contract to DEVICE_CONTRACT and a parser
```

The file is still transferred and preserved. Only the interpretation waits.

## The project Excel

```powershell
python -m tools.ingest_metadata --project IPAMS --workbook ".\azure_sim\rawdata\IPAMS\_metadata\Données IPAMS.xlsx"
```

Handled separately from the recordings, on purpose. Verified against a workbook
built from the real IPAMS headings, including the title row above the headings:

```
  header row            1          <- found automatically
  patients extracted    38
  recognised columns    22
  unmapped columns      4          (kept in variables.csv under extra::)

        age   sex   bmi   asa  surgery_minutes
mean   60.8   0.5  26.3   1.8            287.3
```

Three outputs in `metadata/<project>/`:

| File | Purpose |
|---|---|
| `demographics.csv` | one row per patient, the common variables, ready for statistics |
| `variables.csv` | long format, every value, with the original column heading kept |
| `provenance.json` | workbook checksum, sheet, header row, which columns mapped where |

Nothing is dropped. A heading that is not recognised is still captured under
`extra::`, so no study variable is lost silently. Add a fragment to `CANONICAL`
in `tools/ingest_metadata.py` to promote one to a proper field.

## Order of work

1. `--dry-run` on one project. Read the refused and unrecognised folder lists.
2. Correct `ingest/rules.yaml` until nothing is unexpectedly refused or flagged.
3. Sync that project to `--target .\azure_sim`.
4. Validate with `--deep`. Fix what fails.
5. Ingest that project's Excel.
6. Repeat for the other five projects.
7. Only then point the target at Azure and schedule the sync.

## Not in this step

Preprocessing, quality scoring, statistics, the researcher interface, the
database. Those come next, and they consume what this step produces.
