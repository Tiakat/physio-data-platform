# Phase 4 — Restructure the existing Azure data into the canonical layout

The ~6,600 blobs uploaded by the old sync sit in a **legacy** layout that copies
whatever the Dropbox folders looked like:

```text
PROMISES/ExtractedData/The infinity/Included Patients/Patient 29 20241204 XX/IMG_9666.HEIC
DEXREM/17/DEXREM 17/1.2.826.0.1.3680043….csv
IPAMS/35/ExctractedData/NOL/2026-06-10 0801 PM07180261/Data_15.pmd.enc
```

Phase 4 moves them to the canonical architecture:

```text
PROMISES/Database/RawData/29/Photos/IMG_9666.HEIC
DEXREM/Database/RawData/17/BetterCare/1.2.826.0.1.3680043….csv
IPAMS/Database/RawData/35/NOL/2026-06-10 0801 PM07180261/Data_15.pmd.enc
SILVR/Database/ExtractedData/2/NOL/…/2024-10-15 0808_PlotPDF.pdf
```

Target = `PROJECT/Database/<Layer>/<subject>/<source>/<session sub-folders>/<file>`, from
the phase-1 classifier. Folders that only repeat the subject, source or a wrapper
(`DEXREM 17`, `Medasense_Data`, `ExtractedData`, `The infinity`) are dropped;
session/device folders (`2026-06-10 0801 PM07180261`) are kept.

## Safety model

1. **plan** — read-only, `reports/phase4/restructure_plan.csv` + summary.
2. **copy** — Azure *server-side* copy (nothing downloaded); metadata carried over plus
   `legacy_path`; target never overwritten.
3. **verify** — size + Content-MD5 + `dropbox_content_hash` of source vs copy.
4. **delete-legacy --confirm** — deletes only blobs whose copy passed verify.
   Turn on *blob soft delete* on the storage account first (Data protection → 
   "Enable soft delete for blobs", e.g. 30 days) so a mistake can be undone.

All steps append to `data/_ingestion/manifests/restructure_log.jsonl` and resume where they stopped.

## Plan on the 2026-09-23 inventory (6,740 blobs)

| project | copy | duplicate | review |
|---|---:|---:|---:|
| DEXREM | 521 | 0 | 0 |
| ESMONOL | 374 | 1 | 0 |
| IPAMS | 1,463 | 0 | 0 |
| MONREPI | 288 | 0 | 0 |
| POSBRAIN | 480 | 0 | 0 |
| PROMISES | 2,565 | 229 | 11 |
| SILVR | 241 | 0 | 0 |
| V-RAPS | 469 | 1 | 0 |

* **duplicate** — the same bytes stored 2–3 times in the legacy tree (PROMISES kept the
  same BetterCare export under `The infinity/`, `bettercare/<n>/` and `bettercare/BetterCare1/`).
  One copy is made; the extra legacy blobs are deleted with the rest after verify.
* **88 name collisions** — same file name, *different* content (e.g. a 84 MB and a 105 MB
  version of one BetterCare export). Both are kept under `<subject>/<source>/_from_legacy/<original path>`
  so a researcher can decide which is correct.
* **review (11)** — loose PROMISES photos/recap files with no subject, and one `info.mns`.
  They stay where they are.

## Run (lab PC, UdeM network)

```powershell
.\scripts\run_restructure.ps1                     # plan
.\scripts\run_restructure.ps1 -Copy -Project DEXREM   # try one project first
.\scripts\run_restructure.ps1 -Copy               # everything
.\scripts\run_restructure.ps1 -DeleteLegacy       # after checking verify = 0 MISMATCH
```
