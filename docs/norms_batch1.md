# Physiological Signal Norms — Batch 1: Blood Pressures and Cardiac

**Context:** Intraoperative / general anesthesia monitoring. Sourced from published guidelines, review articles, and device literature (Oct 2026). This document is part of the labeling ground truth for the supervised ML filtration system. Companion to `signal_norms_labeling_ground_truth.md` (batch 0: HR, SpO2, NIBP/ART overview, ECG, Resp, EtCO2, BIS, NOL, Temp).

---

## 1. CVP — Central Venous Pressure

### Normal range
- **2–8 mmHg** supine (Wikipedia; ICU practice). Some sources cite 1–6 mmHg (PMC pulmonary artery catheterisation review) or 2–6 mmHg (open-exam-prep CCRN guide).
- Awake spontaneously breathing: **1–7 mmHg** (5–10 cm H2O). Mechanical ventilation adds **3–5 cm H2O** (~2–4 mmHg) due to raised intrathoracic pressure.
- **Trend matters more than single values** — CVP is a weak stand-alone predictor of fluid responsiveness.
- Measure at **end-expiration**; zero transducer at midaxillary line (phlebostatic axis).

**Sources:**
- https://en.wikipedia.org/wiki/Central_venous_pressure
- https://pmc.ncbi.nlm.nih.gov/articles/PMC11589280/
- https://open-exam-prep.com/study-guides/ccrn/cardiovascular-hemodynamics/hemodynamic-monitoring-values
- https://www.slideshare.net/slideshow/central-venous-pressure-and-intraarterial-blood-pressure-monitoring-invasive-intraoperative-monitoring/229030788

### Waveform morphology (labeling)
- **a wave**: atrial contraction (absent in atrial fibrillation)
- **c wave**: tricuspid valve elevation, early ventricular contraction
- **x descent**: downward displacement of tricuspid during systole
- **v wave**: venous return against closed tricuspid valve
- **y descent**: tricuspid opening in diastole
- **Cannon a waves**: atrium contracting against closed tricuspid valve (AV dissociation)

### Common artifact patterns
- **Catheter whip**: sharp pressure spike at onset of systole (just after ECG R-wave) from catheter striking vessel/heart wall; visible in RV and PA tracings too. Repositioning 1–2 cm may help.
- **Damping**: rounded waveform losing sharp definition — air bubbles, blood/clots, loose connections, kinked tubing, fibrin on catheter tip, catheter wedged against vessel wall.
- **Catheter tip malposition**: tip abutting SVC wall produces artifactual ECG-like spikes (QRS-like complex from tip yielding/abutting wall with transmural pressure changes); risk of SVC perforation. Aspiration of venous blood + respiratory variation confirms correct placement.
- **Tricuspid valve abutment**: abnormal large a–c wave complex; CVP reads falsely high (10–12 mmHg vs true 4–6). Pull catheter back to correct.
- **Respiratory/PEEP effects**: PEEP raises measured CVP; read at end-expiration.
- **Zero/level errors**: transducer not at midaxillary line or not zeroed.

**Sources:**
- https://www.rch.org.au/picu/invasive_haemodynamic_monitoring_part_2/
- https://www.ovid.com/jnls/aoca/fulltext/10.4103/aca.aca_241_20~artifact-in-central-venous-pressure-waveform-due-to-central
- https://pmc.ncbi.nlm.nih.gov/articles/PMC3858709/
- https://www.scribd.com/document/748177203/The-contemporary-pulmonary-artery-catheter-Part-1-placement-and-waveform-analysis

### Recommended filtration (literature)
- Waveform quality check: require recognizable a–c–v complexes; reject damped/flat segments.
- Respiratory gating: use end-expiratory values.
- Rate-of-change gate: CVP changes slowly; sudden jumps >5 mmHg between samples are suspect.
- Cross-check: CVP ≈ right atrial pressure; large CVP–PA diastolic gradient is abnormal.

### Sampling rate norms
- Continuous waveform (transducer); trend values 1 Hz or slower.

### Clean vs artifact
- **Clean**: 0–12 mmHg, recognizable a/c/v waves, respiratory variation present, stable trend.
- **Artifact**: flatline; ECG-like sharp spikes (wall abutment); sudden step >5 mmHg; values <−5 or >25 mmHg; frozen value.

---

## 2. PA_SYS / PA_DIA / PA_MEAN — Pulmonary Artery Pressures

### Normal range
- **Systolic: 15–30 mmHg**; **Diastolic: 4–12 mmHg** (some sources 8–15); **Mean: 10–20 mmHg** (some sources 9–18).
- **mPAP = (PASP + 2 × PADP) / 3**.
- **mPAP ≥ 20 mmHg** = pulmonary hypertension (updated definition); PCWP 6–12 mmHg normal.
- Higher values expected with mechanical ventilation/PEEP.

**Sources:**
- https://pmc.ncbi.nlm.nih.gov/articles/PMC11589280/
- https://www.droracle.ai/articles/645538/what-parameters-can-be-monitored-with-a-pulmonary-artery
- https://www.bjaed.org/action/showPdf?pii=S2058-5349%2824%2900096-9
- https://aneskey.com/hemodynamic-monitoring-arterial-and-pulmonary-artery-catheters/

### Waveform morphology (labeling)
- PA waveform: systolic peak similar to RV systolic, but **diastolic "step-up"** vs RV (dicrotic notch from pulmonic valve closure distinguishes PA from RV).
- Peak PA systolic occurs during ECG T wave.

### Common artifact patterns
- **Catheter whip**: very sharp pressure wave at beginning of systole (just after ECG R-wave), from catheter motion/fluid acceleration or striking heart/PA wall. Produces artefactual systolic peaks. Most common PA catheter artefact (J Clin Monit Comput 2022 review).
- **Damping**: underestimates systolic, overestimates diastolic (same physics as arterial line).
- **Wedge confusion**: balloon inflation gives PCWP waveform (a/v waves like CVP); prolonged wedging risks PA rupture/infarction — inflate ≤1.5 mL for <8–10 s.
- **Hypovolemia/tamponade/RV failure**: PA–RV transition hard to discern (no diastolic step-up).

### Recommended filtration (literature)
- Whip rejection: discard systolic peaks coincident with sharp upstroke artefact; use **trend of mean PAP** (less affected by whip than systolic/diastolic extremes).
- Respiratory gating: end-expiratory readings.
- Plausibility: PA diastolic should exceed PCWP; PA systolic < systemic systolic.

### Sampling rate norms
- Continuous waveform; trend numerics 1 Hz.

### Clean vs artifact
- **Clean**: sys 10–40, dia 0–20, mean 5–25 mmHg; visible dicrotic notch; sys > dia by ≥5 mmHg.
- **Artifact**: isolated sharp systolic spikes (whip); flatline; sys ≤ dia; negative values; frozen.

---

## 3. ART_SYS / ART_DIA / ART_MEAN — Invasive Arterial Pressure (expanded)

### Normal range
- Same systemic targets as batch 0: systolic 90–140, diastolic 50–90, **MAP 65–100 mmHg**; intraop maintain **MAP ≥ 65**.
- **MAP is the most reliable number** — preserved regardless of damping state.

### Damping physics (critical for labeling)
- **Optimal damping coefficient ζ ≈ 0.64–0.7** → 1–2 oscillations after flush test, accurate to 2/3 of natural frequency.
- **Underdamped (ζ < 0.64)**: amplified waveform — **falsely HIGH systolic, falsely LOW diastolic**, widened pulse pressure. Radial underdamping overestimates SAP by mean **21 ± 9 mmHg (+15%)** in ICU patients (Springer, Anesthesiology and Perioperative Science). MAP unaffected.
- **Overdamped (ζ > 1)**: slurred, broad waveform, loss of dicrotic notch — **underestimated systolic, overestimated diastolic**, narrowed pulse pressure. MAP unaffected.
- Causes of overdamping: air bubbles, clots, kinked/compliant tubing, 3-way taps, vasospasm, low flush-bag pressure.
- Causes of underdamping: long stiff tubing, high vascular resistance, partially closed stopcock.

**Sources:**
- https://derangedphysiology.com/main/cicm-primary-exam/cardiovascular-system/Chapter-734/resonance-damping-and-frequency-response
- https://anaesthetics.ukzn.ac.za/wp-content/uploads/2024/09/Transducers-and-damping-Ref-2018-1.pdf
- http://link.springer.com/article/10.1007/s44254-023-00033-3
- https://www.slideshare.net/samirelansary/arterial-line-analysis-46584416

### Square wave (flush) test — labeling reference
- Rapid flush → square wave then oscillations. **Optimal: 1–2 oscillations** before returning to baseline. >2 oscillations = underdamped; 0 with slow return = overdamped.
- Flush spikes (~300 mmHg) must be excluded from trend data.

### Common artifact patterns
- **Flush artefact**: spike to ~300 mmHg for <5 s during line flush.
- **Blood withdrawal**: static pressure during sampling.
- **Damping drift**: gradual loss of dicrotic notch over hours (clot forming).
- **Transducer height error**: ±7.5 mmHg per 10 cm above/below phlebostatic axis.
- **Cautery**: high-frequency obliteration during electrosurgery.

### Recommended filtration (literature)
- Damping detection via flush-test oscillation count or waveform feature analysis; flag under/overdamped segments.
- Flush rejection: exclude values >250 mmHg lasting <5 s.
- Pulse-pressure plausibility: sys > dia + 10; PP 20–100 mmHg.
- MAP consistency: MAP ≈ dia + ⅓(sys − dia) ± 5 mmHg.
- Prefer MAP for clinical decisions when damping is abnormal.

### Clean vs artifact
- **Clean**: clear anacrotic limb, dicrotic notch visible, PP 20–100, MAP 40–180, consistent beat-to-beat.
- **Artifact**: >250 mmHg spikes <5 s (flush); flatline at 0; sys ≤ dia; loss of dicrotic notch with narrowed PP (overdamped); exaggerated spiky systolic with widened PP (underdamped).

---

## 4. NBP_SYS / NBP_DIA / NBP_MEAN — Non-Invasive Blood Pressure (expanded)

### Normal range
- Systolic 90–160, **MAP 70–100 mmHg** (periop concepts). Intraop MAP ≥ 65.
- **MAP is the most accurate oscillometric number** (directly measured at maximal oscillation amplitude); systolic/diastolic are estimated by algorithm.

### Method (labeling)
- Oscillometric: cuff inflates above systolic, deflates gradually; **MAP = cuff pressure at maximal oscillation amplitude**; SBP/DBP derived via proprietary amplitude-ratio algorithms.
- Intermittent: typically every **1–5 min** intraop (up to every 2–5 min per device settings). Values between readings are stale — do not interpolate as continuous.

### Common artifact patterns
- **Wrong cuff size**: too small → falsely high; too large → falsely low. Width-to-arm-circumference ratio should be 0.44–0.55.
- **Motion artifact**: patient movement, shivering, transport — primary challenge of oscillometry; produces erratic/non-physiologic values or failed readings.
- **Arrhythmia** (e.g., AF): irregular pulse amplitude → inaccurate.
- **Very low BP / shock**: oscillometric accuracy degrades; may fail to detect.
- **Position**: beach-chair or lateral positioning changes hydrostatic gradient vs brain/heart level.
- **Cuff leak / malposition**: repeated failed measurements.

**Sources:**
- https://www.periopconcepts.com/blog/nibp
- https://www.medscape.com/viewarticle/514540_4
- http://derangedphysiology.com/main/cicm-primary-exam/cardiovascular-system/Chapter-752/invasive-and-non-invasive-measurement-blood-pressure
- https://link.springer.com/article/10.1007/s10877-020-00482-2

### Recommended filtration (literature)
- Plausibility screen (used in ED shock study): reject if sys−dia < 7 mmHg, sys < 40, MAP < 30.
- Rate-of-change gate: NBP cannot physiologically swing >30 mmHg between 3-min readings without cause; flag larger jumps.
- Cross-check with ART when both present: persistent >20 mmHg MAP disagreement needs investigation (but note site/cuff differences).
- Failed-measurement codes must not be parsed as values.

### Clean vs artifact
- **Clean**: 70 < sys < 250, 30 < dia < 150, MAP 40–180; consistent ±30 mmHg between consecutive readings.
- **Artifact**: sys ≤ dia; any = 0; identical frozen value across readings; >50 mmHg jump between readings; error-code values.

---

## 5. BRA / RAD / P1 — Arterial pressure by site

### What these are
- **BRA_SYS/BRA_DIA/BRA_MEAN**: brachial artery pressure.
- **RAD_SYS/RAD_DIA/RAD_MEAN**: radial artery pressure (most common arterial line site — ease of access, fewer complications).
- **P1_SYS/P1_DIA/P1_MEAN**: generic "pressure channel 1" — on Dräger Infinity monitors, P1 is a configurable invasive pressure channel, most commonly the arterial line.

### Site-dependent differences (labeling)
- **Pulse-wave amplification**: systolic pressure INCREASES from aorta → periphery due to wave reflection; **MAP and diastolic remain ~constant** along the arterial tree.
- **Radial SBP > brachial SBP**: by **~8 mmHg in young adults**, **~14 mmHg in older adults** (ResearchGate/Artery Research studies). Brachio-radial amplification is ~52% of total central-to-peripheral amplification.
- Femoral vs radial MAP: generally interchangeable, but in refractory shock on high-dose norepinephrine, femoral MAP can exceed radial MAP by **4.3–15 mmHg** (62–75% of cases ≥5 mmHg gradient).
- **MAP is site-independent** for labeling purposes — use MAP when comparing BRA vs RAD vs P1.

**Sources:**
- https://v.vibdoc.com/download/normal-arterial-line-waveforms.html?reader=1
- https://www.nature.com/articles/s41598-022-12975-y.pdf
- https://link.springer.com/article/10.1016/j.artres.2010.10.013
- https://www.researchgate.net/publication/269870158_P510_MAJOR_BRACHIAL-TO-RADIAL-SYSTOLIC-BLOOD-PRESSURE-AMPLIFICATION_OCCURS_IN_HEALTHY_PEOPLE_AND_IS_SIGNIFICANTLY_INCREASED_WITH_AGE

### Labeling rules
- BRA/RAD/P1 follow the **same normal ranges, artifact patterns, and filtration** as ART (Section 3).
- **Do not flag** radial-vs-brachial systolic differences <20 mmHg as artifact — this is physiology (amplification), not error.
- **Do flag**: MAP disagreement >15 mmHg between simultaneously recorded sites (possible damping/zero error at one site).
- P1: treat as arterial line by default; confirm against ART channel if both present (should agree within damping tolerance).

---

## Cross-signal consistency rules (batch 1 additions)

Appended to the batch-0 list:

8. **MAP ≈ dia + ⅓(sys − dia) ± 5 mmHg** for every arterial pressure channel (ART, BRA, RAD, P1, NBP). Violation → check damping or parsing.
9. **MAP is site-independent**: ART_MEAN ≈ BRA_MEAN ≈ RAD_MEAN ≈ P1_MEAN ± 10 mmHg when recorded simultaneously. Larger gaps → investigate zero/damping at one site.
10. **Radial systolic ≥ brachial systolic** typically (amplification); reversed gradient is unusual — verify.
11. **CVP < PA diastolic < PA systolic < systemic systolic** — anatomical pressure ordering must hold; violations indicate mislabeled channels or severe artifact.
12. **PA diastolic ≈ PCWP + small gradient**; PA diastolic far above wedge suggests pulmonary vascular disease or artifact.
13. **NIBP MAP should track ART MAP** within ~15 mmHg when both present and patient stable; persistent divergence → cuff or damping issue.
