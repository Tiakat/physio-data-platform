# Lab folder template (Template de labo)

Reference layout for every study. Source: lab template shared 2026-10-01.
The supervisor (`tools/supervisor.py`) audits each project's real Dropbox
layout against this template. The audit is **read-only** — it reports
deviations, it never moves/renames/deletes anything in Dropbox.

```
Template de labo
└── Workflow
    ├── Archives
    │   └── Nom de l'étude
    │       └── Anciens documents
    └── Projets actifs
        └── Nom de l'étude
            ├── Database
            │   ├── RawData
            │   │   └── #participant (1, 2, 3, ...)
            │   ├── Photos
            │   ├── ExtractedData
            │   │   ├── BetterCare
            │   │   ├── Infinity
            │   │   ├── NOL
            │   │   └── BIS
            │   └── AnalyzedData
            │       └── #participant
            └── Documents
                ├── Version finale approuvée
                ├── FIC-CRF-Short Protocol
                ├── Soumission ethique
                │   ├── (document Approbation)
                │   ├── Soumission initiale
                │   ├── Amendement 1
                │   └── Amendement 2
                └── Contrats legaux
```

Notes for the audit:

- Participant folders are numbered (`1`, `2`, `3`, …). Any purely numeric
  folder name under `RawData/` or `AnalyzedData/` is treated as a
  participant folder.
- `Photos/` holds patient photos. Photos are **never** ingested to Azure —
  not even encrypted. The ingest excludes any `Photos` folder at any level
  plus common image extensions anywhere.
- `ExtractedData/` device folders are the canonical device vocabulary:
  BetterCare, Infinity, NOL, BIS. A study may use only some of them —
  present device folders are checked against the vocabulary, absent ones
  are not flagged.
- `Documents/` holds regulatory paperwork. Documents are never ingested.
- Real projects deviate (e.g. DEXREM uses `Included patients/` instead of
  `Database/RawData/`). Deviations are reported by the audit; the ingest
  keeps working through `config/projects.yaml` (`data_roots`,
  `exclude_folders`), which maps the real layout to the logical structure.
