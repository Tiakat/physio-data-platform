# Signal processing configs — format

One YAML per signal family under `configs/signals/`. These are the Phase 11
rules from the processing architecture: **signal-specific, never generic**.
No config here is applied to any column until the schema dictionary
(Phase 3) confirms the column exists and Phase 8 classifies it.

`status: draft` means designed from physiology literature + the shared
ontology, awaiting dictionary evidence and K's review.

## Fields

```yaml
signal: ECG                 # signal family name
status: draft               # draft | reviewed | locked
category: B                 # A time, B raw waveform, C parameter,
                            # D derived index, E medication/exposure,
                            # F event, G metadata, H demographic, I unknown
channels:                   # canonical variables / raw channels this covers
  - ECG
validity:                   # Phase 9: out-of-range -> FLAG, never delete
  range: [min, max]         # from profiles/_variables.yaml where available
  unit: mV
  zero_is_valid: false
artifact_detection:         # Phase 10: each rule -> QC flag, not deletion
  - name: flatline
    method: ...
    params: {...}
filter:                     # Phase 11: the signal-specific method
  method: bandpass
  params: {low_hz: 0.5, high_hz: 40}
  note: ...
normalization:              # Phase 13: none | zscore | baseline | minmax
  policy: none
  rationale: ...
features:                   # Phase 14: only scientifically justified
  - r_peaks
missing_data:               # Phase 7 notes specific to this signal
  structural: ...
graphs:                     # Phase 16: primary | derived | technical | none
  priority: primary
cross_validation:           # Phase: cross-source checks where applicable
  ...
```

QC flag vocabulary (Phase 12, stored in `<sig>_quality`):
VALID, INVALID_RANGE, SPIKE, FLATLINE, MISSING, GAP, SATURATION,
DEVICE_ARTIFACT, LOW_QUALITY.
