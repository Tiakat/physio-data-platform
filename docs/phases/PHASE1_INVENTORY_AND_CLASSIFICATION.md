# Phase 1 — Inventory and file classification

**Goal:** know every file that exists (Dropbox and Azure), and recognise what each
file *is* from its **name alone**, so files sitting outside a source folder
("loose" files) are still classified.

## Why only 8 projects reached Azure

`tools/sync_dropbox_cloud.py` discovered all 23 Dropbox folders but then listed
only `<project>/Database/RawData`. Projects that are organised differently
(`Colectomie en ambulatoire/Database/<n>/ExtractedData`, `PVB abdo/Included patients`,
`POEGEA/Raw EEG files` …) raised *not_found*, were printed as `PROJECT FAILED`
and skipped. The 8 projects with a `Database/RawData` folder were the only ones synced.

Fix: `config/projects.yaml` now lists **every** Dropbox folder with a status and its
data roots; anything not listed is reported as `UNCONFIGURED` instead of being skipped.

| status | projects |
|---|---|
| `data` (sync) | DEXREM, ESMONOL, IPAMS, MONREPI, POSBRAIN, PROMISES, SILVR, V-RAPS, COLECTOMIE, PVB-ABDO |
| `review` (layout must be confirmed by a human) | POEGEA, INTUBATION-FORCES, DATA-RESEARCH |
| `docs_only` | AI-VL, CLAS, DEXBIB, GLIMPSE, ORDAS, PVB-VATS, SAFE, SONONURSE, VOICE |
| `ignore` | `**Sample Folder**` |

## Tools

| command | output |
|---|---|
| `python -m physio.inventory_dropbox` | `reports/phase1/dropbox_inventory.csv`, `dropbox_projects.csv` |
| `python -m physio.inventory_azure` | `reports/phase1/azure_inventory.csv` (size, MD5, sha256 / dropbox hash metadata) |
| `python -m physio.classify <inventory.csv>` | `classified_files.csv`, `classification_summary.md` |

`reports/phase*/` is git-ignored: those files contain patient folder names.

## How a file is classified

1. **File-name signature** (`config/sources.yaml` → `rules`, first match wins) gives
   source, stage (RAW / EXTRACTED / DERIVED / DOCUMENT / METADATA) and confidence.
2. **Folder evidence**: the deepest folder whose name is a source alias
   (`Better Care`, `medasense`, `infinity_brute` …) or matches a device folder
   pattern (BIS `M-xxxx-########`, `DH########`, `L########`).
3. A *high-confidence* name signature beats a contradicting folder (e.g. BIS
   L-files inside PROMISES' `The infinity` container).
4. **Subject** from the first folder matching the project's subject patterns;
   if none, from the file name (`PROMISES 51 …_combined.csv`, `Patient 16_….csv`).
5. **Session** date/time from the name (Infinity timestamp, BetterCare UID,
   NOL date) or from dated folders.
6. A **canonical path** is *proposed* (`PROJECT/Database/<Layer>/<subject>/<source>/…`) —
   nothing in Azure is moved by this phase.

## File-name signatures found in Azure (7,111 blobs, 2026-09-23)

| source | pattern (case-insensitive) | stage | example (synthetic) |
|---|---|---|---|
| Infinity | `YYYYMMDDhhmmss.infinity.data.<bed>.csv` | RAW | `20250509091611.infinity.data.OR^^BLOC07.csv` |
| Infinity | `…infinity.data…_blood_pressure.csv / _analysis.xlsx / _report.pdf` | DERIVED | |
| Infinity | `message N.xml / .json` (gateway HL7) | RAW | `message 12.xml` |
| BetterCare | `1.2.826.0.1.3680043.2.403.36.1YYMMDDhhmmss….13.….-N.csv` (DICOM UID) | RAW | |
| BetterCare | `bettercare.csv`, `*_combined.csv` | DERIVED | |
| NOL (PMD-200) | `Data_N.pmd(.enc)`, `*.mat(.enc)`, `pmd_log.csv`, `key.bin`, `metadata.json` | RAW | `Data_12.pmd.enc` |
| NOL | `YYYY-MM-DD_hhmm[-n]_PMxxxxxxxx.med` / `.zip` | RAW | |
| NOL | `YYYY-MM-DD hhmm[-n]_ExcelData.csv`, `…_PlotPDF.pdf` | EXTRACTED | |
| NOL | `YYYY-MM-DD hhmmss_Screenshot.png` | DOCUMENT | |
| BIS | `L|S########.{ara,h_a,m_a,r2a,o_a,spa,t_a,e_a,f_a}`, `DH########.zip` | RAW | `L05090917.ara` |
| BIS | `BIS_ / DSA_ / EEG_<id4>[_n]_YYYYMMDD_a-b.pdf` | EXTRACTED | |
| BIS | `ScrCap_<id4>_YYYYMMDDhhmmss.pdf` | DOCUMENT | |
| either | `[prefix-]SN <serial> Events Log YYYYMMDD hhmm.csv` | RAW (folder decides) | |
| Pump | `Perf_N_History(Device)_YYMMDD-hhmmss.csv` | RAW | |
| Oximetry | `YYYYMMDD_hhmm__TPADn.Hn`, POSBRAIN `posbrain N m-d-yy ….csv` | RAW | |
| NMT (MONREPI) | `Patient N[_YYMMDD_hhmmss].vital / .csv` (TOF_CNT, TOF_RATIO …) | RAW | |
| VitalRecorder | `*.vital` (other projects) | RAW | |
| Photos / Audio | `.jpg .jpeg .heic .png` / `.m4a .mp3 .wav` | DOCUMENT | |
| Clinical | `*analysé*`, `database/master/liste/données…xlsx` | DERIVED / METADATA | |

## Result on the Azure inventory

| | files |
|---|---:|
| classified with **high** confidence | 6,230 |
| medium | 879 |
| unclassified (`info.mns`, 1 × empty) | 2 |
| **loose** (not in any source folder) — all recognised by name | 308 |
| subject not resolvable from path/name (loose PROMISES photos, recap stats) | 10 → human review |

Files per project × source (Azure):

| project | BIS | BetterCare | Infinity | NOL | other |
|---|---:|---:|---:|---:|---|
| DEXREM | 231 | 39 | 23 | 214 | Pump 14 |
| ESMONOL | 144 | 20 | 17 | 188 | Photos 6 |
| IPAMS | 545 | 347 | 45 | 388 | Photos 137, Clinical 1 |
| MONREPI | 86 | – | 21 | 97 | NMT 51, Clinical 33 |
| POSBRAIN | 157 | 59 | 17 | 236 | Oximetry 11 |
| PROMISES | 629 | 679 | 722 | 551 | Photos 225, Clinical 1 |
| SILVR | – | 31 | 19 | 165 | Audio 26 |
| V-RAPS | 52 | 284 | 64 | 537 | |

## Adding a new file family

Add a rule to `config/sources.yaml` (use `projects: [CODE]` to scope it), add a
synthetic example to `tests/test_classify.py`, run `pytest tests/test_classify.py`.
