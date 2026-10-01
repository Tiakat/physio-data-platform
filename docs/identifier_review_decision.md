# Identifier-column review — decision (2026-10-01)

## Question

The raw-header dictionary review (wider token set, run 36919976131,
`reports/recon/review_20261001_2018.md`) flagged **10 identifier candidates**:
1,830 → 1,855 unmapped columns, 557 single-patient columns.

## Finding

All 10 candidates are **the same column** in the Infinity source of each of
the 10 projects:

```
'Patient Category (^^ISO+)'   (token matched: "patient")
```

| Project | Patients carrying it |
|---|---|
| COLECTOMIE | 45 |
| DEXREM | 1 |
| ESMONOL | 15 |
| IPAMS | 36 |
| MONREPI | 20 |
| POSBRAIN | 14 |
| PROMISES | 54 |
| PVB-ABDO | 8 |
| SILVR | 11 |
| V-RAPS | 24 |

## Analysis

`Patient Category` is the Draeger Infinity monitor's **patient-type setting**
(Adult / Pediatric / Neonatal). It describes the monitor configuration for
the case — it does **not** identify an individual patient. The token heuristic
matched the word "patient"; the semantics are demographic, not identifying.

This is the same field as Stage 2's standing finding of "1 identifier-class
column inside the parquets of every project". That finding is now resolved:
it is `Patient Category`, not a patient ID.

**No patient names, IDs, or direct identifiers were found in any raw CSV
header across all 10 projects.**

## Decision

1. **Reclassify** `Patient Category (^^ISO+)` → domain `demographic`
   (quasi-identifier), canonical variable `PATIENT_CATEGORY`. Keep the
   column in the dictionary and in processing: alarm limits and normal
   ranges differ by category (Adult vs Pediatric vs Neonatal), so it is
   scientifically required for validity ranges.
2. **Never emit per-patient demographic values in the website feed.**
   The archive feed may carry only k≥5-suppressed project-level aggregates
   (e.g. patient-category mix). This is already the feed's design; this
   decision locks it.
3. **Keep the raw column** in Azure parquets (encrypted) and in processing.
   Nothing is deleted or renamed at source.
4. **Remaining watch item:** the 557 single-patient columns were scanned by
   header name only. During Phase 9 QC, add a *value-pattern* scan
   (free-text / name-like / ID-like values) before any de-identified sample
   reaches the feed. Header names alone cannot prove absence of identifiers
   in values.

## Consequences for the feed gate

- The identifier-column blocker for `PHYSIO_FEED_URL` is resolved **for
  headers**. The value-pattern scan (item 4) must still pass before the
  feed URL is configured.
- K's approval remains required for the feed connection itself.
