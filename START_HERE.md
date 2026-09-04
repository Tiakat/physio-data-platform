# Start here

Everything below runs on a Windows laptop with no cloud, no Docker and no
database. Azure comes later, in step 5, once the pipeline is proved on real
data.

## Step 1. Install, about 20 minutes

```powershell
winget install Python.Python.3.11
winget install Git.Git
winget install Microsoft.VisualStudioCode
```

Then, in the project folder:

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Step 2. Run the pipeline on one real project, about 10 minutes

Point it at a Dropbox folder that is already synced to the laptop.

```powershell
python -m tools.run_local --project V-RAPS --root "C:\Users\katia\Dropbox\Liam\Projects actifs\V-RAPS\Database\RawData"
```

Start with `--limit 50` if the project is large and the first run should be quick.

What appears on screen:

```
Files by status
  parsed           41
  not_supported    18      <- .med, .enc, .ara, .pdf, kept and indexed, not a failure
  duplicate         6      <- caught by checksum
  failed            0

Patients: 37
  has_infinity         31 of 37
  has_bettercare       27 of 37
  has_nol              34 of 37
  ready for primary    24 of 37
```

That last block is the answer to "some patients do not have everything". Nobody
is deleted. Coverage is recorded, and each analysis states its own denominator.

Written to `local_store/V-RAPS/`:

| File | Contents |
|---|---|
| `catalog.json` | one entry per file: checksum, device, patient, QC flags, provenance |
| `summary.csv` | one row per patient: coverage, duration, review count |
| `parquet/` | standardised signals, ten times smaller than CSV |

## Step 3. Open the researcher interface, immediately

```powershell
streamlit run app/Home.py
```

Four pages, none of which show a column name or a file path to a researcher:

- **Home**: every project, and a data availability grid showing which recordings
  each patient has.
- **Patient**: one patient, summary statistics table, signals plotted.
- **Compare**: filter a cohort, compare a measurement across patients and
  projects, box plot, per patient bar chart, a non parametric group test, and a
  CSV download.
- **Quality review**: only the recordings the automatic checks flagged, with
  approve, reject and reprocess buttons, each recorded with a name and a
  timestamp.

## Step 4. Repeat for the other five projects

```powershell
python -m tools.run_local --project IPAMS   --root "...\IPAMS\Database\RawData"
python -m tools.run_local --project SILVR   --root "...\SILVR\Database\RawData"
python -m tools.run_local --project DEXREM  --root "...\DEXREM\Database\RawData"
python -m tools.run_local --project ESMONOL --root "...\ESMONOL Patients recrutés\Database\RawData"
python -m tools.run_local --project PROMISES --root "...\PROMISES\Database\RawData"
```

When something does not parse, the fix is almost always a line in
`profiles/<project>.yaml`, not a change to any Python file. That is the whole
point of the design. If a genuinely new column name appears, add it to the
`aliases` list in `profiles/_variables.yaml` and every project gains it at once.

## Step 5. Only now, Azure

```powershell
az login
.\azure\setup.ps1                 # creates the six resources
python -m tools.migrate_to_azure  # uploads local_store and raw files
docker compose up -d              # postgres, minio, prefect, streamlit
```

## Step 6. Before anyone trusts the output

Take fifty to a hundred recordings already analysed by hand. Run them through
the pipeline. Compare row counts, durations, medians and any derived measure
against the manual result. Differences must fall inside a tolerance decided in
advance, and every exception must be explained.

This is the step that demonstrates automation has not silently changed the
science, and it is what the research director should ask for.

## Where to change what

| To change | Edit |
|---|---|
| A column name a device uses | `profiles/_variables.yaml`, the `aliases` list |
| Plausible range, or whether zero is a real reading | `profiles/_variables.yaml` |
| Folder names, delimiter, date format for one project | `profiles/<project>.yaml` |
| Quality thresholds | `profiles/<project>.yaml`, the `quality` block |
| Which devices an analysis needs | `profiles/<project>.yaml`, `coverage` |
| A new device format | new file in `backbone/parsers/`, then register it |
| What a researcher sees | `app/` |

Nothing in `backbone/` contains the name of a project. That is deliberate and
should stay true.

## Known state

Tested against the real Medasense NOL export from V-RAPS patient 37: 1231 rows,
5 second sampling, 51.7 percent of NOL values missing, ten events extracted,
demographics block read separately from the time series block. The two stacked
headers, the comma delimiter and the third date format are all handled.

Tested against a synthetic Infinity file: sentinel codes 9999 removed, aliases
resolved to eight standard variables, recording location read from the filename,
and a five minute flat stretch correctly flagged.

Duplicate detection confirmed on the real duplicated NOL file, which exists in
both DEXREM patient 27 and V-RAPS patient 37.

BetterCare wall clock recovery is implemented from the filename and was verified
earlier against Infinity in all 23 dual recorded patients, median residual lag
four seconds. It has not yet been run end to end here because the BetterCare
files are 100 MB each and were not copied to this machine. That is the first
thing to test in step 2.
