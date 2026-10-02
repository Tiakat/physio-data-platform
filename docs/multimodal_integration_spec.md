# Patient-Level Multimodal Integration Architecture
# K's spec (2026-10-02) — Phase 2/3 design
# 
# Core principle: ONE integrated patient record, not separate datasets per device.
# Do NOT correlate every column with every other column.

## Objective

For each patient, build:
```
Patient 001
├── Demographics (age, sex, weight, height, BMI, ASA)
├── Cardiovascular (HR, SBP/DBP/MAP, arterial waveform, HRV)
├── Respiratory (RR, SpO2, ETCO2, VT, PEEP, PIP, MV)
├── Anesthesia (propofol, remifentanil, sevoflurane, desflurane, FiO2, MAC)
├── Nociception (NOL, HR response, BP response, event response)
├── Brain/depth (BIS, SQI, EMG)
├── Events (induction, intubation, incision, no-touch, drug admin, emergence)
└── Outcome/features (per-domain profiles)
```

## Time Synchronization

All sources → common timeline:
```
patient_id | timestamp | source | variable | value | unit | quality
```

Different resolutions for different questions:
- Cardiovascular response: 5-10 sec windows
- NOL response: 10-30 sec windows  
- BIS: 30-60 sec windows
- Drug exposure: event intervals
- Patient summary: entire recording

Do NOT upsample everything to 200 Hz. Use analysis windows.

## Three Datasets Per Patient

### A. patient_timeseries.parquet
One row per analysis window:
```
patient_id, timestamp, HR, MAP, SBP, DBP, SpO2, RR, ETCO2,
NOL, BIS, FiO2, PEEP, PIP, VT, propofol_rate, remifentanil_rate,
sevoflurane, event
```

### B. patient_events.parquet  
One row per event:
```
patient_id, event, event_time,
HR_baseline, HR_peak, delta_HR,
MAP_baseline, MAP_min, delta_MAP,
NOL_baseline, NOL_peak, delta_NOL,
BIS_baseline, BIS_min, delta_BIS
```

### C. patient_features.parquet
One row per patient:
```
patient_id, age, sex, BMI, ASA,
mean_HR, HR_SD, mean_MAP, MAP_SD, pct_MAP_lt_65,
mean_SpO2, mean_ETCO2, mean_RR, mean_VT_PBW,
NOL_mean, NOL_max, NOL_AUC, pct_NOL_gt_25, incision_delta_NOL,
BIS_mean, BIS_SD, pct_BIS_40_60,
propofol_total, remifentanil_total, volatile_exposure,
HR_response_intubation, NOL_response_intubation,
HR_response_incision, NOL_response_incision,
pct_missing, pct_artifact, recording_duration
```

## Analysis Levels

1. **Within-patient**: Does NOL↑ coincide with HR↑, MAP changes, BIS↓?
2. **Event response**: What happens around intubation/incision/drug admin?
3. **Between-patient**: Do high NOL responders differ by age/BMI/ASA/drug exposure?
4. **Cross-source/device**: Do BetterCare/Infinity/NOL/BIS agree? (Bland-Altman)

## Key Methods

- **Lagged correlation**: corr(drug_t, NOL_{t+τ}) for τ = 0, 10s, 30s, 60s, 120s
- **Mixed-effects models**: NOL_t ~ HR_t + MAP_t + BIS_t + remi_t + (1|patient)
- **Spearman** (not Pearson) for NOL correlations
- **Event windows**: -5min to +5min around incision/intubation
- **Multiple testing correction**: FDR for correlation matrices

## Patient Report Structure

Per patient HTML report:
1. Data quality
2. Demographics  
3. Cardiovascular response
4. Respiratory response
5. Anesthesia exposure
6. NOL/nociception
7. BIS
8. Event responses
9. Cross-device agreement
10. Patient-level correlations
11. Feature table
12. Missing data

## Pipeline Architecture

```
RAW FILES
    ↓
SOURCE-SPECIFIC PROCESSING (master_dictionary.yaml)
    ↓
TIME SYNCHRONIZATION → COMMON TIMELINE
    ↓
EVENT SYNCHRONIZATION
    ↓
SIGNAL-SPECIFIC QC
    ↓
FEATURE EXTRACTION
    ↓
┌──────────┬──────────┬──────────┐
▼          ▼          ▼          ▼
TIMESERIES EVENTS   PATIENT   → MULTIMODAL ANALYSIS
FEATURES   FEATURES FEATURES     │
                                ▼
                    ┌──────────┬──────────┬──────────┐
                    ▼          ▼          ▼          ▼
                Correlation  Event    Cross-device  Patient
                analysis   response   validation   report
```

## Implementation Order

1. Build patient integration layer: patient_id → timestamp → source → signal → value → QC → event
2. Generate three Parquet datasets (timeseries, events, features)
3. Correlation, lagged analysis, mixed models, graphs, reports all use the same validated data

## Important Rules

- NO single "patient score" (don't combine domains with arbitrary weights)
- Separate domain profiles: cardiovascular, respiratory, nociception, anesthesia, neurological, drug exposure, data quality
- Demographics correlate with patient-level features, NOT raw signals
- Cross-source discrepancies flagged, not auto-corrected
- Predefined hypotheses + effect sizes + CI + FDR correction
- Correlation ≠ causation
