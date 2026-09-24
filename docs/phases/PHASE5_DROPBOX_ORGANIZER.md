# Phase 5 — Dropbox organised like the lab template (Azure dropped)

Dropbox is the single source of truth. `physio.organize` moves every file whose
place is certain into the lab template and keeps it that way automatically.

## Template (`config/template.yaml`)

```text
/Liam
├── Admin · Template de labo · Workflow
├── Archives/<étude>/Anciens documents
└── Projets actifs/<étude>
    ├── Database
    │   ├── RawData/<n>/Photos
    │   ├── RawData/<n>/ExtractedData/{BetterCare, Infinity, NOL, BIS, Pump, TOF, Oximetry, Audio, EEG …}
    │   └── AnalyzedData/<n>
    └── Documents
        ├── Version finale approuvée/{FIC, CRF, Short, Protocol}
        ├── Soumission ethique/            ← lettre d'approbation
        │   ├── Soumission initiale
        │   └── Amendement 1, 2, …        (numbered chronologically)
        └── Contrats legaux
```

## How a file is placed

| kind | rule | destination |
|---|---|---|
| device file | file-name signature (phase 1) + participant number in path/name | `RawData/<n>/ExtractedData/<device>/<session folder>/file` |
| photo | image in a participant folder | `RawData/<n>/Photos/` |
| lab-made (combined, filtered, analysis) | derived rules | `AnalyzedData/<n>/<analysis name>/` |
| consent / CRF / short / protocol in a "version finale" folder | name | `Version finale approuvée/{FIC,CRF,Short,Protocol}` |
| approval letter | name (approbation, acceptation …) | `Soumission ethique/` |
| CER responses, corrections, "à soumettre", "soumis …" | folder | `Soumission ethique/Soumission initiale/<round>/` |
| amendment folders | folder | `Soumission ethique/Amendement N/` |
| contracts / entente / agreement | folder or name | `Contrats legaux/` |
| old versions | folder | `/Liam/Archives/<étude>/Anciens documents/` |
| wrong case (`Bettercare`, `NOl`, `infinity`) | template spelling | folder renamed |

**Never moved:** anything the rules are unsure about (biblio, stats, grants, R projects,
no participant number …), folders named *SOURCE DO NOT MODIFY* / *ne pas modifier*,
`.Rproj.user`, identical duplicates (listed), same-name/different-content conflicts (listed),
and the **data** of `data_research`, `POEGEA`, `Intubation…` (their layout needs a decision;
their documents are organised).

Destinations are never overwritten. Every move is logged in `data/_organize/moves.jsonl`
with a run id and can be reversed with `undo`.

## Commands (lab PC, repo venv)

```powershell
.\scripts\organize_dropbox.ps1                           # plan everything (nothing moves)
.\scripts\organize_dropbox.ps1 -Study "PVB abdo" -Apply  # one study first
.\scripts\organize_dropbox.ps1 -Apply                    # all studies
.\scripts\organize_dropbox.ps1 -Undo <run id>            # put a run back
.\scripts\organize_dropbox.ps1 -Skeleton                 # create empty template folders
python -m physio.organize cleanup                        # list empty old folders
python -m physio.organize cleanup --confirm              # delete them (recoverable in Dropbox)
.\scripts\install_organizer_task.ps1                     # automatic mode (see below)
```

Reports: `reports/organize/summary.md`, `plan.csv` (moves), `unsure.csv` (stays, with reason),
`duplicates.csv`.

## Automatic mode

`install_organizer_task.ps1` registers a Windows scheduled task that starts at logon and runs
`python -m physio.organize watch`: it long-polls Dropbox and, about a minute after any
change, re-plans the touched study and applies it. New files dropped in the wrong place or
folders typed with the wrong case are fixed while the PC is on.

## Plan on the 2026-09-23 inventory (72,279 files)

| | files |
|---|---:|
| already in the right place | 1,674 |
| **will be moved** | 6,794 |
| folder case fix | 97 (10 folder renames) |
| identical duplicates (stay, listed) | 196 |
| same name, different content (stay, listed) | 152 |
| stay (unsure / not data) | 63,366 — 42,434 `data_research`, 12,067 PROMISES `Documents/stats` (R code) … |
