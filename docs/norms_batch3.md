# Physiological Signal Norms — Batch 3: ST Segments, Temperature Probes, PLS, NOL

**Context:** Intraoperative / general anesthesia monitoring. Labeling ground truth for ML filtration.
**Date:** 2026-10-02

---

## 1. ECG ST Segments — STI, STII, STIII, STV, STV+, STaVF, STaVL, STaVR

### What these columns are
Per-lead ST segment deviation values measured by the Infinity monitor, in **millimeters** (10 mm = 1 mV). One column per ECG lead:
- **STI** — lead I (lateral)
- **STII** — lead II (inferior)
- **STIII** — lead III (inferior)
- **STV** — precordial lead V (position depends on lead set; typically V5)
- **STV+** — second precordial lead (available with 6-wire lead set)
- **STaVF** — lead aVF (inferior)
- **STaVL** — lead aVL (lateral)
- **STaVR** — lead aVR

Lead territories: inferior ischemia → II, III, aVF (RCA); lateral → I, aVL, V5–V6 (LCx); anterior → V1–V4 (LAD). Intraoperative monitoring classically uses leads II + V5 (80% sensitivity); II + V4 + V5 reaches 96%.

### Monitor measurement specs (Infinity M540 VG8.0, Dräger)
- ST measuring range: **−15.0 to +15.0 mm** (−1.5 to +1.5 mV)
- Resolution: 0.1 mm (0.01 mV)
- Accuracy: ±0.5 mm (0.05 mV) or 15% of measured value, for STI, STII, STIII, aVR, aVL, aVF, V, V+, V1–V6
- ST complex analyzed: 828 ms (−260 to +568 ms from fiducial point); measurement at J point +80 ms (default)
- Update interval: every 15 s (needs ≥1 normal beat)
- Accuracy degrades with ECG double-counting or very low HR (<45 bpm)

### Normal
- **0 mm deviation** (isoelectric ST, possibly slight upsloping) in every lead
- Minor baseline wander (±0.5 mm) acceptable; within monitor accuracy spec

### Ischemia criteria (literature)
- **≥1 mm (0.1 mV) horizontal or down-sloping depression, or ≥1 mm elevation, in ≥2 contiguous leads, lasting ≥60 s** — AHA/ESC criteria (accepted intraoperative standard)
- STEMI thresholds (diagnostic, not monitoring alarms): men ≥40: ≥2 mm in V2–V3, ≥1 mm elsewhere; women: ≥1.5 mm in V2–V3, ≥1 mm elsewhere
- Some perioperative studies use a stricter >0.2 mV cutoff for significance
- Intraoperative practice (continuous ST monitoring protocols): set alarm limits **1–2 mm above and below the patient's own ST baseline**, because absolute thresholds generate false alarms

### Artifact patterns
- **Electrode motion / poor contact**: erratic ST values swinging between leads uncorrelated with clinical picture
- **Baseline wander** (respiration, patient movement): slow oscillation of ST values, symmetric across leads
- **Electrosurgical (cautery) interference**: high-frequency bursts obliterate the trace; ST values freeze or spike during cautery
- **Double-counting / irregular HR**: degraded ST accuracy per manufacturer note
- **Isolated single-lead change without anatomic plausibility** (e.g., lone STaVR deviation without inferior lead changes) → suspect artifact, but aVR elevation can reflect left main ischemia

### Clean vs artifact
- **Clean**: |ST| ≤1 mm in all leads, stable within ±0.5 mm over minutes, plausible anatomic pattern if deviated
- **Suspect**: deviation ≥1 mm sustained ≥60 s in ≥2 anatomically contiguous leads → clinical ischemia until proven otherwise
- **Artifact**: values jumping >5 mm sample-to-sample without hemodynamic correlate; frozen values during cautery; symmetric wandering across all leads; values at measurement extremes (±15 mm) with normal patient

### Filtration rules
- Do **not** smooth across leads — each lead is an independent measurement
- Use per-lead median filter over ~60 s to suppress transient noise; require persistence ≥60 s before clinical significance
- Cross-check anatomic plausibility: depression in II + III + aVF = inferior; I + aVL = lateral
- Reject segments during documented cautery periods

**Sources:** Dräger Infinity M540 Instructions for Use VG8.0 (technical data: ST segment analysis); Dräger M300+ telemetry data sheet (ST measurement and alarm limit ranges); GE Healthcare "12 lead ST monitoring" quick guide; Cleveland Clinic Quarterly on intraoperative ECG ischemia monitoring; Annals of Cardiac Anaesthesia ST-segment study (AHA/ESC criteria); McGraw Hill AccessEmergencyMedicine STEMI criteria; Sandau & Smith continuous ST monitoring protocol (1–2 mm baseline-relative alarms).

---

## 2. Temperature — Ta, Tb (dual temperature probes)

### What these columns are
Two temperature channels from the patient monitor (probe **a** and probe **b**). Standard anesthesia practice places one **core** probe (nasopharyngeal, esophageal, bladder) and one **peripheral/skin** probe. The pair lets clinicians track the **core-to-periphery temperature gradient**, which widens as anesthesia redistributes heat.

- **Ta** — temperature probe channel a (°C)
- **Tb** — temperature probe channel b (°C)

### Normal ranges
- **Core: 36.5–37.5 °C** ("comfortably warm" per NICE CG65)
- **<36.0 °C** = perioperative hypothermia (treat)
- Intraoperative target: maintain **>36.0 °C**
- Skin/peripheral: typically **2–4 °C below core**; forehead skin ~2 °C below core even in steady state
- The core–peripheral gradient normally widens under anesthesia in the first 30–60 min (redistribution), then stabilizes

### Artifact patterns
- **Probe dislodgement**: sudden drop toward ambient (~20–22 °C in OR)
- **Skin probe under forced-air warmer**: false high spikes
- **Esophageal probe in trachea**: falsely low / erratic readings
- **Equilibration**: up to 3 min needed after probe placement — ignore early values
- **Site confusion** (skin read as core): persistent 2–4 °C offset misinterpreted as hypothermia

### Clean vs artifact
- **Clean**: 35–38 °C, drift <0.3 °C/10 min; Ta and Tb track each other with stable gradient
- **Artifact**: drop >2 °C in minutes (dislodgement); any reading <30 °C without documented cause (probe off); sawtooth pattern (intermittent contact); Tb > Ta by large margin without warmer (site swap)
- **Physiologically impossible**: <25 °C → probe off, label MISSING not hypothermia

### Filtration rules
- Rate-of-change gate: <0.5 °C/10 min is physiologic; faster = artifact
- Always preserve probe identity (which is core vs peripheral) — the gradient is the signal
- Never impute missing temperature with zero; preserve gaps
- Cross-check Ta vs Tb: sudden divergence = probe problem on one channel, not patient change

**Sources:** NICE CG65 (perioperative hypothermia, <36.0 °C definition); Sessler et al., "Temperature Monitoring and Perioperative Thermoregulation" (PMC2614355) — core-to-periphery gradients, skin 2 °C below core, site artifacts; Physio-Control LIFEPAK 15 manual (probe equilibration ~3 min, activation range 24.8–45.2 °C).

---

## 3. PLS — Pulse Rate (SpO2-derived)

### What this column is
**Heart rate derived from the pulse-oximetry (plethysmograph) signal**, not from ECG. On Infinity monitors, when HR is sourced from SpO2 the parameter label changes to **PLS** (Dräger Infinity Gamma XL / M540 documentation).

### Normal
- Same as HR: **60–100 bpm** resting; intraoperative 50–100
- Monitor display range: 30–250 bpm; alarm range 30–240 bpm
- Must agree with ECG-derived HR within **±5 bpm**

### Artifact patterns
- **Motion artifact**: erratic PLS spikes/drops uncorrelated with ECG HR
- **Low perfusion** (hypothermia, vasoconstriction): PLS drops out or under-reads; pleth amplitude collapses
- **Ambient light / venous pulsation**: false low readings
- **Probe-off**: PLS → 0 or frozen while ECG HR normal

### Clean vs artifact
- **Clean**: PLS within ±5 bpm of ECG HR, stable beat-to-beat, good pleth waveform amplitude
- **Artifact**: PLS diverging >5 bpm from ECG HR; PLS = 0 with normal ECG trace; wild swings with motion
- **Cross-signal rule**: SpO2 >90% with PLS = 0 → PLS artifact, not asystole

### Filtration rules
- PLS is a **corroborating signal**: use it to validate ECG HR, not as primary HR
- Rate-of-change gate (>20 bpm between samples = suspect)
- Reject when pleth perfusion index low
- Never treat isolated PLS = 0 as cardiac arrest without ECG confirmation

**Sources:** Dräger Infinity Gamma XL user manual (alarm validation table: "Pulse rate (PLS)"); Dräger M540 quick reference guide (HR source selection: "SpO2 – derives the heart rate from the pulse oximetry signal. The heart rate parameter field label changes to PLS"); Dräger M300+ telemetry data sheet (pulse rate 30–250 bpm, alarm range 30–240 bpm).

---

## 4. NOL — Nociception Level Index (expanded)

### What this column is
Multiparameter index of **nociception (pain response) under general anesthesia**, from the Medasense PMD-200 monitor. Finger probe measures photoplethysmography (PPG), galvanic skin response, peripheral temperature, and accelerometry; a **Random Forest** model fuses them into one index.

### Scale and targets
- **Range: 0–100** (0 = no nociception, 100 = maximum)
- **Target under GA: 10–25** (Medasense manufacturer recommendation)
- Clinical protocol cutoffs (NOL-guided RCT): **<10** = excessive analgesia; **10–27** = adequate nociception–antinociception balance; **>27** = inadequate analgesia / severe noxious stimulation
- Sampling: computed **every 5 s** (0.2 Hz)

### Input parameters (Random Forest features)
HR, high-frequency HRV power (0.15–0.4 Hz band), PPG wave amplitude, skin conductance level, number of skin conductance fluctuations, and their time derivatives.

### Clinical use
- Guides opioid (remifentanil/fentanyl) dosing: adjust only if NOL stays outside target for **>2 minutes** (filters transients)
- Superior to HR/BP alone for detecting nociceptive response under GA; leads to reduced analgesia administration and less hemodynamic instability
- Limitation: poor standalone predictor of *postoperative* pain — combine with clinical factors

### Artifact patterns
- **Probe displacement**: frozen value or drop to 0
- **Motion** (detected via accelerometry): false elevation
- **Hypothermia / vasoconstriction**: degraded PPG → unreliable
- **Dried conductance electrodes**: baseline drift

### Clean vs artifact
- **Clean**: 0–100, changes <15 per 30 s, PPG present, tracks surgical stimulation
- **Artifact**: frozen >1 min; 0 during known noxious stimulus; ±40 swings without clinical correlate; flatline with absent PPG

### Filtration rules
- Rate-of-change gate: >30 per 10 s without stimulus = suspect
- Require >2 min deviation before acting (per clinical protocols)
- Probe-off detection: NOL without valid PPG = artifact
- Cross-check: NOL >25 should track with HR/BP rises during stimulation; isolated NOL spike without hemodynamic response → motion artifact

**Sources:** Funcke et al., "Surgical pleth index monitoring in perioperative pain management" (PMC10834712) — NOL 0–100, target 10–25; JTD review "The quantification and monitoring of intraoperative nociception levels in thoracic surgery" — PMD-200, Random Forest derivation, target 10–25; NOL-guided breast surgery RCT (MDPI Medicina) — <10 / 10–27 / >27 cutoffs, 5 s update, >2 min deviation protocol; preprints.org epidural-NOL study — four input parameters, 0–100, target 10–25.

---

## Labeling summary (for ML ground truth)

| Signal | Normal | Artifact flag |
|---|---|---|
| STI…STaVR (each) | −1 to +1 mm, stable | ≥1 mm sustained ≥60 s in ≥2 contiguous leads; ±15 mm extremes; erratic jumps |
| Ta, Tb | 35–38 °C, gradient 2–4 °C | <30 °C (probe off); drop >2 °C/min; sawtooth |
| PLS | 60–100, within ±5 of ECG HR | Diverges >5 from ECG HR; 0 with normal ECG |
| NOL | 10–25 (adequate) | Frozen >1 min; 0 during stimulus; ±40 swings |
