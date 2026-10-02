# Physiological Signal Norms — Batch 2: Anesthetic Gases & Ventilation

**Context:** Intraoperative / general anesthesia monitoring. Labeling ground truth for ML filtration.
Companion to `signal_norms_labeling_ground_truth.md` (batch 1: HR, SpO2, NIBP/ART, ECG, Resp, EtCO2, BIS, NOL, Temp).

---

## 1. MAC — Minimum Alveolar Concentration

### Definition
Alveolar (end-tidal) anesthetic concentration that prevents purposeful movement in 50% of subjects at surgical stimulus. The ED50 of inhaled anesthetics; ED95 ≈ 1.3 MAC. Unitless multiple.

### MAC values (MAC40, 40-year-old, 1 atm, in O2)
| Agent | MAC (vol%) |
|---|---|
| Nitrous oxide | 104–105 |
| Desflurane | 6.0–6.6 |
| Sevoflurane | 1.8–2.0 |
| Isoflurane | 1.15–1.20 |
| Enflurane | 1.63 |
| Halothane | 0.75 |

**Sources:** Medscape "Characteristics of Anesthetic Agents" (https://www.medscape.com/viewarticle/492432_3); CoreNotes Anesthesia Review MAC table; Barash textbook table (via pg-elite-nceprep.com).

### Modifiers
- **Age:** −6 to −7% per decade after 40. Neonatal sevoflurane MAC 3.3%; >65 yr sevoflurane 1.45%.
- **Additive:** 0.6 MAC N2O + 0.5 MAC sevoflurane = 1.1 MAC total.
- **Decreased by:** hypothermia, hypotension (MAP <40 mmHg), pregnancy, opioids, dexmedetomidine, anemia, low PaO2.
- **Increased by:** hyperthermia, hypernatremia, chronic alcohol use, young age.
- **MAC-awake** (no response to command): ~0.3–0.5 MAC. **MAC-BAR** (blunts autonomic response): ~1.6–2.0 MAC.
- **Monitor age correction matters:** same gas mix reads 1.3 MAC in a 40-yr-old but 0.9 MAC in a 6-yr-old (Datex-Ohmeda). Wrong age → false depth sense.
- **Source:** PMC4155317 (https://pmc.ncbi.nlm.nih.gov/articles/PMC4155317/).

### Normal range (intraoperative)
- **Maintenance target: 0.7–1.3 MAC** (Draeger low-flow scheme targets 0.8–1.0).
- Clinical practice (Smith et al.): sevoflurane 0.86±0.35 MAC, isoflurane 0.69±0.17 MAC.
- **inxMAC** = monitor-calculated age- and N2O-corrected MAC multiple.

### Artifact patterns
- MAC = 0 with vaporizer on and end-tidal agent present → age/demographics not entered, or N2O not included in calc.
- MAC jumps when patient age edited mid-case (expected, not artifact — flag as metadata change).
- Negative or >5 MAC → calculation error.

### Clean vs artifact
- **Clean:** 0.5–1.5 MAC during maintenance, changes <0.2 MAC/min, consistent with vaporizer setting and age.
- **Artifact:** MAC = 0 while ET agent >0.5 MAC; step change >1 MAC without vaporizer change.

---

## 2. ET_DES — End-Tidal Desflurane

### Normal range
- **1 MAC = 6.0–6.6 vol%** (in 100% O2).
- **Maintenance: 6–7.2 vol% (1–1.2 MAC).** Mean end-tidal at end of anesthesia: 6.34 ± 1.15%.
- **Unit:** vol%.
- Blood:gas partition coefficient 0.42–0.45 (very low → fast wash-in/out).

**Sources:** Dove Medical Press MDER desflurane controller paper (https://www.dovepress.com/intelligent-generating-controller-a-desflurane-concentration-value-whi-peer-reviewed-fulltext-article-MDER); DrugBank pharmacokinetics review (https://go.drugbank.com/articles/A226525); PMC5416721 (https://pmc.ncbi.nlm.nih.gov/articles/PMC5416721/).

### Typical values by phase
| Phase | Vaporizer dial | Expected ET |
|---|---|---|
| Wash-in (rapid) | 12% (2 MAC) | rising to target in ~1–4 min |
| Wash-in (standard) | 6% | rising |
| Maintenance (1 L/min flow) | 3% | 4–6% |
| Draeger low-flow maintenance | 4–6% dial | 0.8–1.0 MAC |

### Artifact patterns
- **Airway irritation:** desflurane is the most pungent volatile; >5.4% (≈1 MAC in 100% O2) can trigger coughing/laryngospasm — high ET with airway events is physiological, not monitor artifact.
- Sampling-line issues (kink, water, disconnect) → falsely low/zero ET.
- ET > inspired during uptake phase → impossible; suggests mislabeled channels or calibration error.

### Filtration
- Rate-of-change gate: ET cannot change >2 vol%/min at constant vaporizer (wash-in excepted).
- Cross-check: ET ≈ vaporizer × FA/FI ratio (0.4–0.9 depending on duration).

### Clean vs artifact
- **Clean:** 3–8 vol% during maintenance, stable ±0.5 vol%, inspired > end-tidal, FA/FI 0.5–0.9.
- **Artifact:** sudden drop to 0 (disconnect); frozen >2 min at constant surgery; ET > inspired.

---

## 3. ET_SEVO — End-Tidal Sevoflurane

### Normal range
- **MAC: 2.05% adults, 3.3% neonates, 1.8% (MAC40).**
- **Maintenance: 0.5–1.0 MAC ≈ 1–2 vol%.** Minimal-flow target in studies: 1.2 vol%.
- **Unit:** vol%.
- Blood:gas partition 0.65.

**Sources:** PMC4923957 "SEVOFLURANE – A NEW ERA" (https://pmc.ncbi.nlm.nih.gov/articles/PMC4923957/); BJA MACEX study (https://www.bjanaesthesia.org.uk/article/S0007-0912(17)38492-1/pdf); BMC Anesthesiology 1-1-8 wash-in scheme (https://link.springer.com/article/10.1186/s12871-020-0940-2).

### Typical values by phase
| Phase | Vaporizer dial | Expected ET |
|---|---|---|
| Wash-in (rapid, 1-1-8) | 8% (4 MAC) | 1→3.5% over 1–5 min |
| Wash-in (standard) | 6% (first 60 s) | rising |
| Maintenance | 1.8–2.5% | 0.8–1.5% |
| Draeger low-flow maintenance | 2–2.5% dial | 0.8–1.0 MAC |
| Extubation (MACEX) | — | 0.95 ± 0.19% |

### FA/FI dynamics (wash-in)
FA/FI ratio rises 0.42–0.46 → 0.69–0.72 within ~260–286 s. Inspired and end-tidal run parallel.

### Artifact patterns
- Same sampling-line artifacts as desflurane.
- Sevoflurane is non-pungent — no airway-irritation confound.

### Clean vs artifact
- **Clean:** 0.5–2.5 vol% maintenance, stable, inspired > end-tidal, FA/FI 0.4–0.9.
- **Artifact:** drop to 0 (disconnect); ET > inspired; step >1 vol% without dial change.

---

## 4. Inspired Agents — inDes, inSev, inHal, inPrimA

### Normal ranges (vol%)
| Channel | Wash-in dial | Maintenance dial | Notes |
|---|---|---|---|
| inDes | 6–12% | 3–6% | 12% = 2 MAC rapid wash-in |
| inSev | 6–8% | 1–2.5% | 8% = 4 MAC rapid wash-in |
| inHal | 2–4% (induction) | 0.5–1% | Halothane MAC 0.75% |
| inPrimA | = primary agent dial | = primary agent dial | Generic channel |

**Sources:** BMC Anesthesiology 1-1-8 scheme; Draeger low-flow graphic PDF-8265 (https://www-qa.draeger.net/Content/Documents/Content/low-flow-graphic-pdf-8265-2017-de-master-1708-1.pdf); springermedizin.de FGF study.

### Key relationship
**Inspired ≥ end-tidal always** during uptake (FA/FI < 1). Gradient narrows as tissues saturate. At steady state FA/FI ≈ 0.7–0.9.

### Artifact patterns
- Inspired = 0 with vaporizer on → sampling before vaporizer, or agent misidentified.
- Inspired < end-tidal → channel swap or calibration error.
- Inspired spikes when vaporizer dial untouched → back-pressure / pump effect (usually small).

### Clean vs artifact
- **Clean:** inspired ≥ end-tidal, follows vaporizer dial within 30 s, FA/FI 0.4–1.0.
- **Artifact:** inspired < end-tidal; inspired = 0 with dial >0; frozen.

---

## 5. etPrimA / etSecA — End-Tidal Primary/Secondary Agent

Generic monitor channels: primary = main volatile in use; secondary = second agent (often N2O or a second volatile during crossover).
- Norms = norms of whichever agent is selected (see ET_DES / ET_SEVO / etN2O).
- **"Selected agent unit"** = monitor display unit for the active agent (vol%).
- During agent crossover both channels may show values briefly — expected, not artifact.

---

## 6. etN2O — End-Tidal Nitrous Oxide

### Normal range
- **MAC 104–105%** — cannot reach 1 MAC at 1 atm; always an adjunct.
- **Typical: 50–70% inspired/ET** (e.g., FiN2O 0.6–0.7 with FiO2 0.3–0.4).
- Provides ~0.5–0.65 MAC; reduces volatile requirement 20–30%.
- **Unit:** vol%.

**Sources:** numberanalytics.com N2O pharmacology (https://www.numberanalytics.com/blog/pharmacology-of-nitrous-oxide-in-anesthesia); PMC9284342 N2O vs N2O-free anesthesia (https://pmc.ncbi.nlm.nih.gov/articles/PMC9284342/).

### Physiology notes
- **Second gas effect:** accelerates uptake of co-administered volatile (higher FA/FI).
- **Diffusion hypoxia:** on discontinuation, rapid N2O washout dilutes alveolar O2 → give 100% O2 for 5–10 min after stopping.
- **Contraindicated:** pneumothorax, bowel obstruction, middle-ear/eye surgery, air embolism (expands closed gas spaces 2–3×).

### Artifact patterns
- ET N2O > inspired N2O → impossible; channel error.
- N2O present when flowmeter at 0 → leak or misidentification (N2O/IR overlap with other gases on some analyzers).

### Clean vs artifact
- **Clean:** 40–70%, stable, ≤ inspired, falls to 0 within ~5 min of discontinuation (with 100% O2).
- **Artifact:** frozen; > inspired; present with N2O off >10 min.

---

## 7. etO2 — End-Tidal Oxygen

### Normal range
- **Preoxygenation target: EtO2 >90%** (optimal denitrogenation).
- Intraop: tracks FiO2; FiO2 − EtO2 gradient normally small, widens with hypoventilation.
- Typical FiO2: 0.8–1.0 induction/emergence, <0.6 maintenance (ESAIC survey).
- **Unit:** vol%.

**Sources:** GE Healthcare oxygen transport white paper (https://clinicalview.gehealthcare.com/sites/default/files/Oxygen%20Transport%20White%20paper_JB82296XX_Jun30.pdf); jscimedcentral.com apneic oxygenation review; ESAIC oxygen survey (https://link.springer.com/article/10.1186/s12871-022-01884-2).

### Artifact patterns
- EtO2 > FiO2 → impossible; calibration or pipeline cross-connection (documented: O2-rich air from air outlets up to 100%).
- Sudden EtO2 drop with unchanged FiO2 → hypoventilation, disconnect, or increased uptake.

### Clean vs artifact
- **Clean:** 25–95%, ≤ FiO2 + 2%, stable or tracking FiO2 changes.
- **Artifact:** EtO2 > FiO2; step change without FiO2 change or ventilatory cause.

---

## 8. Carrier Gas / Anesthesia Gas / 2nd Anesthesia Gas

- **Carrier gas** = O2/air or O2/N2O mixture carrying the volatile to the patient.
- Typical low-flow: O2 0.25–0.35 L/min + air 0.2–0.5 L/min, or O2/N2O 50:50.
- **2nd anesthesia gas** = secondary agent channel (usually N2O).
- These are **settings/exposures, not physiological signals** — log as metadata, don't filter.

---

## 9. PEEP — Positive End-Expiratory Pressure

### Normal range (intraoperative)
- **Default: 5–10 cmH2O** (major abdominal surgery).
- **4–5 cmH2O** mitigates end-expiratory alveolar collapse in most ventilated patients.
- Low ≤5; moderate 5–10; high >15 cmH2O.
- EIT-guided optimal: 4.6–13.8 (supine), 7.0–15.0 (Trendelenburg 10°), 8.6–17.0 (Trendelenburg 20°).
- **Unit:** cmH2O. **This is a ventilator setting**, not a measured physiological signal.

**Sources:** Eikermann et al. differential PEEP effects (https://d.docksci.com/differential-effects-of-intraoperative-positive-end-expiratory-pressure-peep-on-_5a2a3045d64ab2ffa6235999.html); WikiLectures PEEP (https://www.wikilectures.eu/w/PEEP); Wikipedia PEEP (https://en.wikipedia.org/wiki/Positive_end-expiratory_pressure); DOAJ EIT titration study.

### Effects of excessive PEEP
>15 cmH2O: decreased venous return → hypotension, reduced cardiac output, increased RV afterload, risk of barotrauma.

### Artifact patterns
- PEEP = 0 (ZEEP) during controlled ventilation → either intentional or setting not recorded.
- PEEP >20 without ARDS diagnosis → verify; possible data-entry error.
- Measured PEEP ≠ set PEEP → circuit leak or auto-PEEP (intrinsic PEEP from incomplete exhalation).

### Clean vs artifact
- **Clean:** 0–15 cmH2O, stable, matches ventilator setting, plausible for procedure.
- **Artifact:** negative values; >30 (unless recruitment maneuver documented); rapid oscillation.

---

## 10. TV / VT — Tidal Volume

### Normal range
- **Lung-protective: 6–8 mL/kg ideal/predicted body weight** (double-lung ventilation).
- One-lung ventilation: 4–6 mL/kg IBW.
- Traditional (discouraged): 10–15 mL/kg.
- For 70 kg IBW: **420–560 mL**.
- Plateau pressure should stay <28–30 cmH2O.
- **Unit:** mL.

**Sources:** ATM intraoperative ventilation review (https://atm.amegroups.org/article/view/8594/html); PMC10068651 perioperative ventilation (https://pmc.ncbi.nlm.nih.gov/articles/PMC10068651/); BMC Anesthesiology LPV survey (https://link.springer.com/article/10.1186/s12871-018-0495-7).

### Artifact patterns
- VT = 0 with normal EtCO2 → spontaneous breathing or data dropout.
- VT exactly constant to the mL for hours → set (not measured) value logged; treat as setting.
- Sudden halving → circuit disconnect or one-lung switch.

### Clean vs artifact
- **Clean:** 300–700 mL (adult), breath-to-breath variation <15%, consistent with IBW.
- **Artifact:** 0 with mechanical ventilation documented; >1500 mL adult; negative.

---

## 11. DO2 — Oxygen Delivery

### Normal range
- **Indexed: ~600 mL O2/min/m².** Absolute (75-kg adult): ~1000 mL/min.
- Formula: DO2 = CO × CaO2 × 10; CaO2 = Hb×1.34×SaO2 + 0.003×PaO2.
- **Critical threshold:** DO2:VO2 ratio 2:1 — below this, tissue dysoxia, lactate rises.
- **Unit:** mL/min (or mL/min/m² indexed).

**Sources:** WS Hemo 2011 slides (https://www.slideshare.net/slideshow/oxygen-delivery-and-consumption-ws-hemo-2011-pptx/283945922); Frontiers in Medicine tissue oxygenation review (https://www.frontiersin.org/journals/medicine/articles/10.3389/fmed.2017.00247); IntechOpen anemia chapter.

### Artifact patterns
- DO2 is **derived**, not measured — errors propagate from CO, Hb, SaO2 inputs.
- DO2 = 0 with normal vitals → input dropout (usually CO or Hb missing).

### Clean vs artifact
- **Clean:** 400–800 mL/min/m², tracks CO changes, DO2:VO2 ≥2.
- **Artifact:** 0 with CO >0; step change without CO/Hb/SaO2 change (recalculation glitch).

---

## 12. V'O2 — Oxygen Consumption

### Normal range
- **Indexed: 110–160 mL/min/m².** Absolute: ~250 mL/min (resting adult).
- Formula (Fick): VO2 = CO × (CaO2 − CvO2).
- Normal extraction ratio (O2ER = VO2/DO2): **22–32% (~25%)**.
- VO2 <100 mL/min/m² = low; >160 with lactate >4 = supply-demand mismatch.
- **Unit:** mL/min (or mL/min/m² indexed).

**Sources:** WS Hemo 2011; scribd hemodynamic monitoring; anestesianet.com Marino oxygenation chapter.

### Artifact patterns
- Same derivation caveats as DO2.
- VO2 > DO2 (O2ER >100%) → impossible; input error.

### Clean vs artifact
- **Clean:** 80–200 mL/min/m², O2ER 20–35%.
- **Artifact:** negative; O2ER >100% or <5% sustained.

---

## Cross-signal consistency rules (gases & ventilation)
1. Inspired agent ≥ end-tidal agent (during uptake); FA/FI 0.4–1.0.
2. Total MAC = Σ (ET_agent / MAC_agent) + ET_N2O/104; target 0.7–1.3.
3. EtO2 ≤ FiO2 + 2%.
4. EtN2O ≤ FiN2O.
5. PEEP 5–10 with VT 6–8 mL/kg IBW = standard protective combo.
6. DO2:VO2 ≥ 2:1; O2ER 22–32%.
7. End-tidal agent = 0 with vaporizer on → sampling/disconnect artifact (also flagged by capnography).
8. Any inspired/end-tidal gas = 0 while its flowmeter/vaporizer is on → line or analyzer fault.
