# Physiological Signal Norms — ML Labeling Ground Truth

**Context:** Intraoperative / general anesthesia monitoring. Sourced from published guidelines, review articles, and device literature (Oct 2026). This document is the labeling ground truth for the supervised ML filtration system.

---

## 1. HR — Heart Rate

### Normal range
- **60–100 bpm** resting adult (ASA 1/2 pre-sedation baseline target)
- Intraoperative under general anesthesia: commonly **50–100 bpm**; bradycardia <50 and tachycardia >100–120 are alarm thresholds on most monitors
- Monitor measurement range: 15–300 bpm (adults), accuracy ±1 bpm (WHO patient monitor technical specification)

**Sources:**
- DOCS Education, "Monitoring Vital Signs During Sedation For Optimal Patient Safety": preferred HR >60 and <100 — https://www.docseducation.com/blog/monitoring-vital-signs-during-sedation-optimal-patient-safety?_wrapper_format=html&page=208
- WHO technical specification (patient monitor): HR range 15–300 bpm adults, accuracy ±1 bpm — https://www.who.int/docs/default-source/wpro---documents/countries/mongolia/call-for-proposals/20210703-detailed-technical-speficitions.pdf?Status=Master&

### Common artifact patterns
- **Motion artifact**: erratic spikes/drops uncorrelated with ECG QRS; sudden jumps >20 bpm between consecutive samples
- **Electrode motion**: QRS-like morphology noise that fools beat detectors (ECGSQI undersensitive to it — Clifford et al.)
- **Electrosurgical (cautery) interference**: high-frequency bursts obliterating the trace during surgical cautery use
- **Poor contact / lead-off**: flatline at 0 or frozen last value
- **Double-counting / half-counting**: HR suddenly doubles or halves (T-wave oversensing)

**Sources:**
- Clifford et al., "Robust heart rate estimation from multiple asynchronous noisy sources using signal quality indices and a Kalman filter" (PMC) — https://pmc.ncbi.nlm.nih.gov/articles/PMC2259026/
- "An Analysis of the Effects of Noisy ECG Signal on Heartbeat Detection Performance" (MDPI Sensors) — EM artefacts have the highest influence on beat detection, followed by motion artifact and baseline wander — https://www.mdpi.com/2306-5354/7/2/53/htm

### Recommended filtration (literature)
- **Signal Quality Indices (SQIs)**: tSQI, sSQI, kSQI, pSQI computed per beat; reject beats with SQI < 0.7; fuse multiple sources (ECG + ABP + PPG) with Kalman filter weighted by SQI (Clifford et al.)
- **QRS detection**: Pan–Tompkins algorithm best under noise, followed by Hamilton (MDPI Sensors study)
- **Physiological plausibility gate**: reject beat-to-beat changes >20% without corroboration from a second source

### Sampling rate norms
- HR trend values: typically **1 Hz** (one value/sec) or every 5 s on anesthesia monitors (NTUH study: HR recorded at 5 s intervals — MDPI Sensors 2026, https://www.mdpi.com/1424-8220/26/11/3498)
- Derived from ECG sampled at 250–500 Hz

### Clean vs artifact
- **Clean**: HR 30–180 bpm, beat-to-beat variation <15%, corroborated by PPG pulse rate within ±5 bpm, stable SQI ≥0.7
- **Artifact**: sudden step >20 bpm between samples; HR = 0 with normal SpO2 pleth; HR exactly doubled/halved vs previous stable value; flatline (zero variance) >10 s

---

## 2. SpO2 — Peripheral Oxygen Saturation

### Normal range
- **95–100%** healthy adults; **97–99%** typical
- Intraoperative target: **>95%** (provides safety margin on oxyhemoglobin dissociation curve; PaO2 ~80 mmHg at 95% vs ~60 mmHg at 90%)
- **<90%** = hypoxemia (critical threshold — steep part of dissociation curve); **<80%** = severe/emergency
- COPD patients: target 88–92% may be acceptable

**Sources:**
- "Pulse Oximetry—A Perioperative Perspective" (PMC) — https://pmc.ncbi.nlm.nih.gov/articles/PMC13298035/
- MDPI J. Clin. Med. "Pulse Oximetry—A Perioperative Perspective" — https://www.mdpi.com/2075-4418/16/12/1812
- BMC Nursing (Springer): normal SpO2 in healthy individuals 97–99% — https://link.springer.com/article/10.1186/s12912-018-0283-1

### Common artifact patterns
- **Motion artifact** (shivering, repositioning): unpredictable erroneous values, spurious desaturation
- **Low perfusion** (hypothermia, vasopressors, shock): under-reading by 4–6% or signal loss
- **Ambient light interference**: falsely low or erratic readings
- **Venous pulsation** (forehead probe, Trendelenburg): false low up to ~5%
- **IV dyes** (methylene blue): transient false low
- **Carboxyhemoglobin**: falsely high/normal (CO poisoning missed)
- **Methemoglobinemia**: SpO2 "fixed" near 85%
- **Probe off finger**: sudden drop to 0 or frozen value

**Sources:**
- PMC "Pulse Oximetry—A Perioperative Perspective", Table 2 (limitations/sources of error) — https://pmc.ncbi.nlm.nih.gov/articles/PMC13298035/

### Recommended filtration (literature)
- **Plethysmograph waveform quality check**: reject SpO2 values when pleth amplitude is poor or perfusion index is low
- **Rate-of-change gate**: SpO2 cannot physiologically change >3–4%/sec; faster changes = artifact
- **Cross-check with HR**: pulse rate from oximeter should match ECG HR within ±5 bpm
- Masimo SET signal-extraction technology is the clinical reference for motion-resistant pulse oximetry

### Sampling rate norms
- **1 Hz** typical for trend; pleth waveform 50–100 Hz

### Clean vs artifact
- **Clean**: 90–100%, changes <2%/min, pleth waveform present with good amplitude, pulse rate matches ECG HR
- **Artifact**: drop >4% within 5 s without clinical cause; SpO2 = 0 or frozen; value oscillating wildly ±5% beat-to-beat; SpO2 fixed at exactly 85% (methemoglobinemia flag)

---

## 3. NIBP / ART — Blood Pressure (Systolic, Diastolic, Mean)

### Normal range
- **Systolic 90–140 mmHg**, **diastolic 50–90 mmHg**, **MAP 65–100 mmHg** (normal baseline category)
- Intraoperative: maintain **MAP ≥ 65 mmHg** (narrative review: "maintain MAP above 65 mmHg during surgery")
- Hypotension thresholds (literature): SAP <80–100, MAP <50–70, DAP <40–60 mmHg
- Hypertension: systolic ≥140 or diastolic ≥90; severe ≥180/110 (postpone elective surgery)

**Sources:**
- "Continuous Blood Pressure Monitoring in Patients Having Surgery: A Narrative Review" (PMC) — maintain MAP above 65 mmHg — https://pmc.ncbi.nlm.nih.gov/articles/PMC10385393/
- Anesthesia & Analgesia (Ovid): hypotension thresholds SAP <80–100, MAP <50–70, DAP <40–60 — https://www.ovid.com/jnls/anesthesia-analgesia/fulltext/10.1213/ane.0000000000007379~diastolic-versus-systolic-or-mean-intraoperative-hypotension
- Wiley "Provisional Decision-Making for Perioperative Blood Pressure Management" — baseline categories and MAP ≥65 targets — https://onlinelibrary.wiley.com/doi/10.1155/2022/5916040

### Common artifact patterns
- **NIBP cuff artifact**: motion during inflation → error or over-reading; wrong cuff size → systematic bias (too small = over-reads)
- **ART damping** (underdamped/overdamped): underdamped → systolic overshoot; overdamped/kinked line → falsely low systolic, narrowed pulse pressure
- **Transducer height error**: ±7.5 mmHg per 10 cm height mismatch
- **Flush artifact**: sudden square-wave spike to ~300 mmHg during line flush
- **NIBP during cautery**: complete measurement failure

### Recommended filtration (literature)
- **Pulse pressure plausibility**: systolic > diastolic always; pulse pressure 20–100 mmHg; MAP ≈ diastolic + ⅓(systolic − diastolic) — reject triples violating this
- **Flush detection**: reject spikes >250 mmHg lasting <5 s
- **Damping check** (ART): square-wave test; natural frequency and damping coefficient assessment

### Sampling rate norms
- **NIBP**: intermittent — every 1–5 min typical intraoperatively
- **ART**: continuous waveform 100–250 Hz; numeric values 1 Hz

### Clean vs artifact
- **Clean**: 70 < systolic < 250, 30 < diastolic < 150, 40 < MAP < 180; systolic > diastolic + 10; consistent with previous reading within ±30 mmHg (NIBP)
- **Artifact**: systolic ≤ diastolic; any value = 0; NIBP reading identical to 3+ decimals repeated (frozen); ART flatline at transducer zero; spike >300 mmHg

---

## 4. ECG (waveform)

### Normal range (morphology)
- P wave <0.12 s, PR interval 0.12–0.20 s, QRS <0.12 s, QTc <450 ms (men) / <470 ms (women)
- Resting sinus rhythm 60–100 bpm

### Common artifact patterns
- **Baseline wander**: low-frequency drift (respiration, electrode movement) — biggest confounder after EM/motion
- **Muscle (EMG) artifact**: high-frequency noise, worst impact on QRS detection (MDPI Sensors study: EM artefacts contributed to poorest detection performance)
- **Motion artifact**: large erratic deflections, main frequency range wider than 0.5 Hz
- **Powerline interference**: 50/60 Hz sinusoidal contamination
- **Electrode motion**: QRS-like deflections causing false beat detections
- **Electrosurgical interference**: broadband high-frequency obliteration of trace

**Sources:**
- "Main artifacts in electrocardiography" (Wiley J. Arrhythmia) — https://onlinelibrary.wiley.com/doi/10.1111/anec.12494
- MDPI Sensors heartbeat detection study — EM > MA > BW in degrading detection — https://www.mdpi.com/2306-5354/7/2/53/htm
- "A method to extract realistic artifacts from ECG recordings" (PMC) — 5th-order Butterworth bandpass 0.5–40 Hz as standard preprocessing — https://pmc.ncbi.nlm.nih.gov/articles/PMC7771512/

### Recommended filtration (literature)
- **Bandpass 0.5–40 Hz** (5th-order Butterworth) — standard preprocessing removing baseline wander and high-frequency noise while preserving QRS (PMC 7771512)
- **Notch 50/60 Hz** for powerline
- **High-pass 0.5 Hz** is the widely accepted cut-off for baseline wander, but motion artifact extends beyond it — adaptive filtering preferred when reference noise channel available (MDPI Sensors 20/5/1468)
- **Signal Quality Indices** per 10 s window: kSQI, sSQI, pSQI; reject windows with SQI <0.7

### Sampling rate norms
- **250–500 Hz** diagnostic/monitor ECG (NTUH intraoperative study: 500 Hz — MDPI Sensors 2026)
- Minimum 100–120 Hz acceptable for QRS detection algorithms (arxiv preprocessing review)

### Clean vs artifact
- **Clean**: recognizable P-QRS-T morphology, stable baseline (±0.5 mV), SQI ≥0.7, QRS amplitude 0.5–3 mV
- **Artifact**: saturated/flatline segments; broadband noise obscuring all morphology; repeated identical QRS-like spikes at non-physiological intervals; 50/60 Hz dominant on spectrum

---

## 5. Respiratory Rate (RR)

### Normal range
- **12–20 breaths/min** adults (NEWS2, accepted clinical range)
- Bradypnea <12, tachypnea >20, apnea = 0
- Under general anesthesia with mechanical ventilation: set rate typically 10–16/min

**Sources:**
- Wheatley (2018) Nursing Times / NEWS2: normal RR 12–20 bpm — https://cdn.ps.emap.com/wp-content/uploads/sites/3/2018/06/180627-Respiratory-rate-3-how-to-take-an-accurate-measurement-1.pdf
- NumberAnalytics "Respiratory Rate Essentials for Anesthesiologists" — https://www.numberanalytics.com/blog/respiratory-rate-guide-for-anesthesiologists

### Common artifact patterns
- **Impedance pneumography**: cardiac artifact (heartbeats counted as breaths → doubled rate); motion artifact
- **Capnography-derived**: dislodged sampling line → rate drops to 0; diluted sample → erratic rate
- **Doubling/halving**: algorithm locks onto cardiac signal instead of respiratory

### Recommended filtration
- **Rate-of-change gate**: RR cannot change >5 breaths/min between consecutive samples physiologically
- **Cross-check**: capnography-derived RR vs impedance RR agreement within ±2/min
- **Plausibility**: 4–40 breaths/min absolute bounds

### Sampling rate norms
- **1 Hz** trend values; impedance waveform 25–50 Hz

### Clean vs artifact
- **Clean**: 8–30/min, stable ±3/min over 1 min, agrees with capnography
- **Artifact**: sudden doubling/halving; RR = 0 with normal EtCO2 waveform; erratic ±10/min swings

---

## 6. EtCO2 — End-Tidal CO2

### Normal range
- **35–45 mmHg** normocapnia
- **<35 mmHg** = hypocapnia/hyperventilation; **>45 mmHg** = hypercapnia/hypoventilation
- General anesthesia cohort mean: 37.2 ± 4.3 mmHg (PLOS ONE procedural sedation study)
- Capnography reading typically 3–4 mmHg below arterial PaCO2 (32–42 mmHg on monitor ≈ PaCO2 35–45)

**Sources:**
- PLOS ONE "The relationship between minute ventilation and end tidal CO2" — normal range 35–45 mmHg shaded; GA cohort 37.2 ± 4.3 — https://journals.plos.org/plosone/article/figures?id=10.1371/journal.pone.0180187
- Anesthesia General capnography guide — https://anesthesiageneral.com/capnography/
- JEMS capnography review — https://www.jems.com/patient-care/capnography-provides-bigger-physiological-picture-to-maximize-patient-care/

### Common artifact patterns
- **Sampling line disconnection/kink**: EtCO2 drops to 0, waveform flatlines
- **Water in sampling line**: erratic low readings, "shark-fin" distortion
- **Bronchospasm**: "shark-fin" waveform (prolonged upslope), normal-to-low EtCO2 number
- **Rebreathing** (exhausted soda lime): elevated baseline (inspired CO2 >0)
- **Cardiogenic oscillations**: small oscillations on plateau

### Recommended filtration (literature)
- **Waveform morphology check**: normal capnogram = square/rectangular with sharp upstroke, alveolar plateau, return to zero baseline
- **Zero-baseline check**: inspired CO2 must be ~0; elevated baseline = rebreathing or calibration error
- **Rate-of-change gate**: EtCO2 changes <10 mmHg/min physiologically (except induction/emergence events)

### Sampling rate norms
- **1 Hz** numeric; capnogram waveform 20–100 Hz

### Clean vs artifact
- **Clean**: 30–50 mmHg, square waveform, zero inspiratory baseline, consistent breath-to-breath
- **Artifact**: EtCO2 = 0 with normal SpO2 (line off); sudden drop >15 mmHg in one breath; no waveform but numeric value present (frozen)

---

## 7. BIS — Bispectral Index

### Normal range
- **0–100** dimensionless; **40–60** = appropriate level for general anesthesia (manufacturer recommendation)
- 100 = fully awake; 80–100 awake/light sedation; 60–80 moderate sedation; 40–60 general anesthesia; <40 deep anesthesia/burst suppression; 0 = EEG silence
- BIS <40 for >5 min associated with increased risk of stroke (HR 3.23), MI (1.94), death (1.41)

**Sources:**
- Wikipedia "Bispectral index" — https://en.wikipedia.org/wiki/Bispectral_index
- NICE Diagnostics Assessment (Depth of anaesthesia monitors) — target 40–60 — https://www.nice.org.uk/guidance/htg292/documents/depth-of-anaesthesia-monitors-eentropy-bis-and-narcotrend-overview2
- Scientific Reports (Nature): BIS 40–60 during maintenance — https://www.nature.com/articles/s41598-023-37150-9?error=cookies_not_supported&code=f8bb6c79-28b8-47d4-931a-2f983a0e23f4

### Common artifact patterns
- **EMG contamination**: falsely elevated BIS (muscle activity reads as "awake"); EMG spikes signal incipient arousal
- **Electrocautery**: BIS drops to 0 or freezes during cautery use
- **Poor electrode contact**: high impedance → erratic values or "check electrode" state
- **Pacemaker / shivering**: rhythmic artifact elevating BIS

### Recommended filtration (literature)
- **Signal Quality Index (SQI)**: BIS monitor provides SQI; reject BIS values when SQI <50%
- **EMG check**: BIS devices report EMG power (dB); BIS >60 with high EMG = suspect, treat EMG first
- **Suppression ratio (SR)**: cross-check — high SR with BIS 40–60 is inconsistent
- **Smoothing**: BIS is already a 15–30 s smoothed index; do not over-smooth further

### Sampling rate norms
- **BIS .r2a**: raw EEG 2 channels, **128 Hz**, int16
- **BIS .spa**: 1 Hz pipe-delimited trends (BIS, SQI, EMG, SR)
- Monitor display updates every 1 s

### Clean vs artifact
- **Clean**: SQI >80%, EMG <35 dB, BIS changes <10/min, consistent with anesthetic dosing
- **Artifact**: BIS = 0 during cautery with SQI drop; sudden jump >20 within seconds; frozen value (zero variance) >30 s; BIS >80 in a paralyzed anesthetized patient with high EMG

---

## 8. NOL — Nociception Level Index

### Normal range
- **0–100** dimensionless; **10–25** = adequate analgesia under general anesthesia (Medasense recommendation)
- <10 = excessive analgesia (absence of noxious stimulation); >25–27 = inadequate analgesia / severe noxious stimulation
- Computed every **5 seconds** from photoplethysmogram amplitude, skin conductance, HR, HRV and derivatives via Random Forest regression

**Sources:**
- MDPI Medicina NOL-guided analgesia RCT: <10 excessive, 10–27 adequate, >27 inadequate; computed every 5 s — http://www.mdpi.com/1648-9144/60/12/1921
- PMC "Surgical pleth index monitoring in perioperative pain management" — NOL 10–25 appropriate under GA — https://pmc.ncbi.nlm.nih.gov/articles/PMC10834712/
- JTD review: Medasense target 10–25; Random Forest regression from HR, HRV high-frequency power, PPG amplitude, skin conductance — https://jtd.amegroups.org/article/view/31818/html

### Common artifact patterns
- **Finger probe displacement**: signal loss → frozen NOL or drop to 0
- **Motion** (accelerometry channel): movement falsely elevates NOL
- **Hypothermia/vasoconstriction**: poor PPG amplitude → unreliable NOL
- **Skin conductance electrode drying**: drift in baseline

### Recommended filtration (literature)
- **Probe-off detection**: NOL frozen or 0 with no PPG signal = exclude
- **Rate-of-change gate**: NOL is a 5-s index; changes >30 within 10 s without surgical stimulus are suspect
- Protocols require deviation >2 min before acting (filters transient spikes)

### Sampling rate norms
- **0.2 Hz** (one value per 5 s)

### Clean vs artifact
- **Clean**: 0–100, changes <15 per 30 s, PPG signal present
- **Artifact**: frozen value >1 min; NOL = 0 for extended periods during known surgical stimulation; erratic ±40 swings

---

## 9. Body Temperature

### Normal range
- **36.5–37.5 °C** core ("comfortably warm" per NICE)
- **<36.0 °C** = inadvertent perioperative hypothermia (NICE definition)
- 34–36 °C = mild hypothermia; intraoperative target: maintain **>36.0 °C**
- Core temperature normally 37 ± 0.2 °C; peripheral 2–4 °C lower than core

**Sources:**
- NICE CG65 "Hypothermia: prevention and management in adults having surgery" — hypothermia <36.0 °C; normal 36.5–37.5 °C — https://Www.Nice.Org.uk/guidance/cg65/chapter/Context
- PMC "The effect of humidified heated breathing circuit on core body temperature" — core 36.5–37.5 °C; mild hypothermia 34–36 °C — https://pmc.ncbi.nlm.nih.gov/articles/PMC5562134/

### Common artifact patterns
- **Probe dislodgement**: sudden drop toward ambient temperature (~20–22 °C)
- **Skin vs core confusion**: skin probes read 2–4 °C lower — not artifact but must be labeled by site
- **Warming device artifact**: forced-air warmer blowing on skin probe → falsely high spikes
- **Esophageal probe in trachea**: reads low with respiratory variation

### Recommended filtration (literature)
- **Rate-of-change gate**: core temperature changes <0.5 °C per 10 min physiologically (except active cooling/warming); faster = probe artifact
- **Site labeling**: always record measurement site (esophageal, bladder, nasopharyngeal, skin)
- **Ambient floor**: reject values <25 °C as probe-off (unless therapeutic hypothermia documented)

### Sampling rate norms
- **0.1–1 Hz** typical; slow-changing signal

### Clean vs artifact
- **Clean**: 35–38 °C, drift <0.3 °C per 10 min, consistent with warming measures
- **Artifact**: sudden drop >2 °C within minutes; value <30 °C without documented cause; sawtooth pattern (intermittent contact)

---

## Cross-Signal Consistency Rules (for labeling)

These relationships must hold in clean data; violations indicate artifact in at least one signal:

1. **HR (ECG) ≈ pulse rate (SpO2 pleth)** within ±5 bpm
2. **MAP ≈ diastolic + ⅓(systolic − diastolic)** within ±5 mmHg
3. **SpO2 >90%** while HR = 0 → HR is artifact (not cardiac arrest)
4. **EtCO2 = 0** with normal SpO2 and normal airway pressures → sampling line artifact
5. **BIS 40–60** expected during maintenance phase of general anesthesia; BIS >80 with adequate anesthetic dosing + high EMG → EMG artifact
6. **NOL >25** should correlate with HR/BP increases; isolated NOL spike without hemodynamic change → suspect motion artifact
7. **RR (impedance)** should agree with **capnography RR** within ±2/min

---

## Sampling Rate Summary Table

| Signal | Waveform rate | Numeric/trend rate |
|--------|--------------|-------------------|
| ECG | 250–500 Hz | HR 1 Hz (or 5 s) |
| SpO2 | pleth 50–100 Hz | 1 Hz |
| ART (invasive BP) | 100–250 Hz | 1 Hz |
| NIBP | — | every 1–5 min |
| RR | impedance 25–50 Hz | 1 Hz |
| EtCO2 | capnogram 20–100 Hz | 1 Hz |
| BIS raw EEG | 128 Hz (2 ch) | BIS/SQI/EMG 1 Hz |
| NOL | — | 0.2 Hz (every 5 s) |
| Temperature | — | 0.1–1 Hz |

---

*Compiled 2026-10-02 for the physio-data-platform ML labeling system. All ranges refer to adult patients under general anesthesia unless noted.*
