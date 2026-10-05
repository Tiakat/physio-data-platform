"""Knowledge-driven filtering engine for the physio-data-platform.

Replaces blind/generic filtering with a system that consults ALL available
knowledge before removing a single sample:

1. **Dictionary-driven hard bounds** — physiological ranges parsed from the
   literature-backed norm files (docs/signal_norms_labeling_ground_truth.md
   and docs/norms_batch{1,2,3}.md, Oct 2026). Values outside hard bounds are
   ALWAYS artifact. Values inside are kept.
2. **ML-guided artifact detection** — the trained supervised RandomForest
   models in Azure processed/ml_models/sup/ ({safe}_model.pkl, 8 window
   features, 100-sample windows / 50-step) classify windows as clean/artifact.
   Falls back to dictionary bounds when no model exists for a column.
3. **Signal-specific rules** — from K's requirements (SpO2 flat is normal,
   NBP gaps are normal, ART morphology preserved, ...).
4. **Safety rails (NON-NEGOTIABLE)**:
   - filtering would remove >50% of a signal -> keep ORIGINAL, flag REVIEW
   - filtering would remove ~100% (<1% remaining) -> keep ORIGINAL, CRITICAL
   - every removal is logged with reason (auditable)

Public entry point: smart_filter_frame(df, t_seconds, svc) -> (filt, qc, log)
  df         : raw dataframe (numeric signal columns + timestamp-like cols)
  t_seconds  : 1-D array of timestamps in seconds, same length as df
  svc        : Azure BlobServiceClient (for ML model download; may be None ->
               dictionary-only mode)
Returns (filt_df, qc_df, filter_log dict).
Never raises on a per-column problem: a failing column passes through with
a flag, so one bad column can never kill a patient's processing.
"""

import io
import os
import pickle

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# 1. KNOWLEDGE BASE — hard physiological bounds.
# Extracted verbatim from the literature-backed norm files (Oct 2026):
#   docs/signal_norms_labeling_ground_truth.md  (HR, SpO2, NIBP/ART, ECG,
#       RR, EtCO2, BIS, NOL, Temperature)
#   docs/norms_batch1.md  (CVP, PA pressures, ART expanded, NBP expanded)
#   docs/norms_batch3.md  (ST segments, dual temp, PLS, NOL expanded)
# hard_lo/hard_hi : values outside are ALWAYS artifact, no exceptions.
# clean_lo/clean_hi : literature "clean" range (informational, for the log).
# ---------------------------------------------------------------------------
KNOWLEDGE_BASE = {
    # family: dict(hard_lo, hard_hi, clean_lo, clean_hi, unit, notes)
    "HR":      dict(hard_lo=15,  hard_hi=300, clean_lo=30, clean_hi=180,
                    unit="bpm",
                    notes="Clean 30-180 bpm; artifact: step >20 bpm, =0, "
                          "flatline >10 s, doubled/halved vs stable value."),
    "SPO2":    dict(hard_lo=0,   hard_hi=100, clean_lo=90, clean_hi=100,
                    unit="%",
                    notes="Flat at 98-99% is NORMAL, never filter it. "
                          "Artifact: <70, sudden drop >4-5% in seconds, =0."),
    "ART_SYS": dict(hard_lo=20,  hard_hi=300, clean_lo=70, clean_hi=250,
                    unit="mmHg",
                    notes="PP 20-100, MAP 40-180; sys must exceed dia+10."),
    "ART_DIA": dict(hard_lo=10,  hard_hi=250, clean_lo=30, clean_hi=150,
                    unit="mmHg", notes=""),
    "ART_MEAN": dict(hard_lo=20, hard_hi=250, clean_lo=40, clean_hi=180,
                     unit="mmHg", notes=""),
    "NBP_SYS": dict(hard_lo=20,  hard_hi=300, clean_lo=70, clean_hi=250,
                    unit="mmHg",
                    notes="Intermittent cuff: gaps are NORMAL, never "
                          "interpolate across gaps."),
    "NBP_DIA": dict(hard_lo=10,  hard_hi=250, clean_lo=30, clean_hi=150,
                    unit="mmHg", notes=""),
    "NBP_MEAN": dict(hard_lo=20, hard_hi=250, clean_lo=40, clean_hi=180,
                     unit="mmHg", notes=""),
    "CVP":     dict(hard_lo=-5,  hard_hi=30,  clean_lo=0,  clean_hi=12,
                    unit="mmHg", notes="Clean 0-12 mmHg."),
    "PA_SYS":  dict(hard_lo=0,   hard_hi=80,  clean_lo=10, clean_hi=40,
                    unit="mmHg", notes=""),
    "PA_DIA":  dict(hard_lo=-5,  hard_hi=50,  clean_lo=0,  clean_hi=20,
                    unit="mmHg", notes=""),
    "PA_MEAN": dict(hard_lo=0,   hard_hi=60,  clean_lo=5,  clean_hi=25,
                    unit="mmHg", notes=""),
    "RR":      dict(hard_lo=4,   hard_hi=60,  clean_lo=8,  clean_hi=30,
                    unit="/min",
                    notes="Clean 8-30/min; artifact: sudden doubling/halving, "
                          "=0 with normal EtCO2."),
    "ETCO2":   dict(hard_lo=5,   hard_hi=80,  clean_lo=30, clean_hi=50,
                    unit="mmHg",
                    notes="Clean 30-50 mmHg square waveform; =0 with normal "
                          "SpO2 means line off."),
    "BIS":     dict(hard_lo=0,   hard_hi=100, clean_lo=20, clean_hi=80,
                    unit="index",
                    notes="0-100 index; artifact: frozen >30 s, jump >20 in "
                          "seconds, =0 with SQI drop."),
    "NOL":     dict(hard_lo=0,   hard_hi=100, clean_lo=0,  clean_hi=100,
                    unit="index",
                    notes="0-100 index; artifact: frozen >1 min, erratic "
                          "+-40 swings."),
    "TEMP":    dict(hard_lo=30,  hard_hi=43,  clean_lo=35, clean_hi=38,
                    unit="C",
                    notes="Clean 35-38 C; artifact: <30 without cause, "
                          "sudden drop >2 C in minutes."),
    "ST":      dict(hard_lo=-5,  hard_hi=5,   clean_lo=-1, clean_hi=1,
                    unit="mm", notes="|ST| <= 1 mm clean."),
    "PLS":     dict(hard_lo=15,  hard_hi=300, clean_lo=30, clean_hi=180,
                    unit="bpm",
                    notes="SpO2-derived pulse; should track ECG HR +-5 bpm."),
    # Waveform families: NEVER filtered by this engine (morphology belongs
    # to the EEG/ECG pipelines, not to trend filtering).
    "ECG":     dict(passthrough=True, unit="mV", notes="Waveform: passthrough."),
    "PLETH":   dict(passthrough=True, unit="au", notes="Waveform: passthrough."),
}

# Safety rails (fractions of valid samples).
RAIL_REVIEW_FRAC = 0.50   # >50% removal -> keep original, flag for review
RAIL_WIPE_FRAC = 0.99     # >99% removal -> keep original, CRITICAL

# ML window geometry — MUST match tools/ml_supervised_train.py
ML_WINDOW = 100
ML_STEP = 50


# ---------------------------------------------------------------------------
# 2. Column -> signal family mapping
# ---------------------------------------------------------------------------
def classify_family(col: str) -> str:
    """Map a raw column name to a KNOWLEDGE_BASE family (or 'OTHER')."""
    u = str(col).upper().replace(" ", "_")
    # Waveforms first (passthrough, never filtered here).
    if "ECG" in u:
        return "ECG"
    if "PLETH" in u or "PLETHYSMO" in u:
        return "PLETH"
    # HR (but not HRV).
    if "HRV" in u:
        return "OTHER"
    if u.startswith("HR") or "_HR" in u or u == "HR":
        return "HR"
    if "PULSE" in u or u.startswith("PLS"):
        return "PLS"
    # Pressures.
    if "CVP" in u:
        return "CVP"
    # Pulmonary artery (PA_SYS/PA_DIA/PA_MEAN, PAP, PAD, PAM).
    if u.startswith("PA_") or u.startswith("PAP") or u.startswith("PAD") \
            or u.startswith("PAM"):
        if "SYS" in u or u.endswith("_S") or "SYST" in u:
            return "PA_SYS"
        if "DIA" in u or u.endswith("_D") or "DIAST" in u:
            return "PA_DIA"
        return "PA_MEAN"
    # Non-invasive: NBP / NIBP. MUST come before the ART/IBP check because
    # "IBP" is a substring of "NIBP".
    if "NBP" in u or "NIBP" in u:
        if "SYS" in u or "SYST" in u:
            return "NBP_SYS"
        if "DIA" in u or "DIAST" in u:
            return "NBP_DIA"
        return "NBP_MEAN"
    # Arterial (invasive): ART / ABP / IBP.
    if "ART" in u or u.startswith("ABP") or "IBP" in u:
        if "SYS" in u or "SYST" in u:
            return "ART_SYS"
        if "DIA" in u or "DIAST" in u:
            return "ART_DIA"
        return "ART_MEAN"
    if "SPO2" in u or "SP_O2" in u:
        return "SPO2"
    if "BIS" in u:
        return "BIS"
    if "NOL" in u:
        return "NOL"
    if "TEMP" in u or u in ("TA", "TB", "T1", "T2"):
        return "TEMP"
    if "ETCO2" in u or "ET_CO2" in u or u in ("CO2",):
        return "ETCO2"
    if u.startswith("RR") or "RESP" in u or "AWRR" in u:
        return "RR"
    if u.startswith("ST") and any(
            ch.isdigit() or ch in ("I", "V", "A") for ch in u[2:3]):
        return "ST"
    return "OTHER"


def sanitize(name: str) -> str:
    """Same sanitizer as tools/ml_supervised_train.py (model filenames)."""
    return "".join(c if c.isalnum() or c in ("-", "_", ".") else "_"
                   for c in str(name))


# ---------------------------------------------------------------------------
# 3. ML model cache (Azure processed/ml_models/sup/)
# ---------------------------------------------------------------------------
_MODEL_CACHE = {}
_MODEL_BLOB_MAP = None  # filename -> blob path


def _model_blob_map(svc):
    """List once per process: model filename -> blob path."""
    global _MODEL_BLOB_MAP
    if _MODEL_BLOB_MAP is not None:
        return _MODEL_BLOB_MAP
    _MODEL_BLOB_MAP = {}
    try:
        container = svc.get_container_client("processed")
        for b in container.list_blobs(
                name_starts_with="processed/ml_models/sup/"):
            name = b["name"] if isinstance(b, dict) else b.name
            fname = name.rsplit("/", 1)[-1]
            if fname.endswith("_model.pkl"):
                _MODEL_BLOB_MAP[fname] = name
    except Exception as e:
        print(f"[smart-filter] model listing failed: {e}", flush=True)
    print(f"[smart-filter] found {len(_MODEL_BLOB_MAP)} trained ML models",
          flush=True)
    return _MODEL_BLOB_MAP


def get_model(svc, col: str):
    """Return the trained RF model for a column, or None (dictionary-only)."""
    if svc is None:
        return None
    safe = sanitize(col)
    if safe in _MODEL_CACHE:
        return _MODEL_CACHE[safe]
    blob_map = _model_blob_map(svc)
    target = f"{safe}_model.pkl"
    blob = blob_map.get(target)
    if blob is None:
        # Family-level fallback: any model whose sanitized name starts with
        # the family token (e.g. HR.1 -> HR model).
        fam = classify_family(col)
        for fname, bpath in blob_map.items():
            if fname.startswith(fam.replace("_", "")) or \
                    fname.upper().startswith(fam):
                blob = bpath
                break
    if blob is None:
        _MODEL_CACHE[safe] = None
        return None
    try:
        data = svc.get_blob_client(
            container="processed", blob=blob).download_blob().readall()
        model = pickle.loads(data)
        _MODEL_CACHE[safe] = model
        print(f"[smart-filter] loaded ML model for {col} ({blob})",
              flush=True)
        return model
    except Exception as e:
        print(f"[smart-filter] model load failed for {col}: {e}", flush=True)
        _MODEL_CACHE[safe] = None
        return None


def _is_art_signal(signal_name: str) -> bool:
    """Check if signal is arterial pressure (uses morphology features)."""
    low = signal_name.lower()
    return any(kw in low for kw in ["art", "rad ", "rad_", "bra ", "bra_",
                                     "abp", "arterial", "ibp", "nbp"])


def _art_morphology_features(wv: np.ndarray) -> list:
    """12 ART morphology features, IDENTICAL to tools/ml_supervised_train.py."""
    if len(wv) < 20:
        return [0.0] * 12
    sys_p = float(np.max(wv))
    dia_p = float(np.min(wv))
    pp = sys_p - dia_p
    feats = [sys_p, dia_p, pp]
    diffs = np.diff(wv)
    pos_diffs = diffs[diffs > 0]
    feats.append(float(np.max(pos_diffs)) if len(pos_diffs) > 0 else 0.0)
    feats.append(float(np.mean(pos_diffs)) if len(pos_diffs) > 0 else 0.0)
    try:
        from scipy.signal import find_peaks
        peaks, _ = find_peaks(wv, distance=10, prominence=pp * 0.1 if pp > 0 else 1)
        n_beats = len(peaks)
        feats.append(float(n_beats))
        if n_beats >= 2:
            intervals = np.diff(peaks)
            feats.append(float(np.mean(intervals)))
            feats.append(float(np.std(intervals) / (np.mean(intervals) + 1e-6)))
            notch_count = 0
            for i in range(len(peaks) - 1):
                segment = wv[peaks[i]:peaks[i+1]]
                if len(segment) > 10:
                    sub_peaks, _ = find_peaks(segment, distance=5)
                    if len(sub_peaks) > 0:
                        notch_count += 1
            feats.append(float(notch_count) / max(n_beats - 1, 1))
        else:
            feats.extend([0.0, 0.0, 0.0])
        feats.append(float(sys_p / (dia_p + 1e-6)))
        feats.append(float(np.trapz(wv - dia_p)))
    except Exception:
        feats.extend([0.0] * 6)
    # Ensure exactly 12
    while len(feats) < 12:
        feats.append(0.0)
    return feats[:12]


def _window_features(wv: np.ndarray, signal_name: str = "") -> list:
    """8 base features + 12 ART morphology (if arterial). IDENTICAL to ml_supervised_train."""
    diffs = np.abs(np.diff(wv)) if len(wv) > 1 else np.array([0.0])
    base = [
        float(np.mean(wv)), float(np.std(wv)),
        float(np.min(wv)), float(np.max(wv)),
        float(np.max(wv) - np.min(wv)), float(np.median(wv)),
        float(np.max(diffs)), float(len(np.unique(np.round(wv, 2)))),
    ]
    if _is_art_signal(signal_name):
        return base + _art_morphology_features(wv)
    else:
        return base + [0.0] * 12


def ml_artifact_mask(values: np.ndarray, model, signal_name: str = "") -> np.ndarray:
    """Classify 100-sample windows with the trained RF; True = artifact."""
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    if model is None or n < ML_WINDOW:
        return mask
    try:
        for i in range(0, n - ML_WINDOW + 1, ML_STEP):
            w = values[i:i + ML_WINDOW]
            if np.isnan(w).mean() > 0.5:
                continue  # not enough data to judge; leave alone
            wv = w[~np.isnan(w)]
            if len(wv) < 10:
                continue
            feats = np.array([_window_features(wv)])
            if int(model.predict(feats)[0]) == 1:
                mask[i:i + ML_WINDOW] = True
    except Exception as e:
        print(f"[smart-filter] ML predict failed: {e}", flush=True)
        return np.zeros(n, dtype=bool)
    return mask


# ---------------------------------------------------------------------------
# 4. Dictionary + signal-specific rules
# ---------------------------------------------------------------------------
def dict_artifact_mask(values: np.ndarray, t: np.ndarray,
                       family: str) -> tuple:
    """Return (mask, reasons dict). True = artifact per dictionary rules."""
    n = len(values)
    mask = np.zeros(n, dtype=bool)
    reasons = {}
    kb = KNOWLEDGE_BASE.get(family)
    if kb is None or kb.get("passthrough"):
        return mask, reasons
    lo, hi = kb["hard_lo"], kb["hard_hi"]
    valid = ~np.isnan(values)

    # 4a. Hard bounds — always artifact, no exceptions.
    bad = valid & ((values < lo) | (values > hi))
    if bad.any():
        mask |= bad
        reasons["hard_bounds"] = int(bad.sum())

    v = values.copy()
    dt = np.median(np.diff(t)) if len(t) > 1 else 1.0
    dt = dt if dt > 0 else 1.0

    # 4b. Signal-specific artifact patterns (from the norm docs + K).
    if family == "SPO2":
        # Flat at 98-99% is NORMAL — never touch it. Only: <70, sudden
        # drops >5% between consecutive samples.
        dv = np.abs(np.diff(np.where(valid, values, np.nan)))
        with np.errstate(invalid="ignore"):
            drop = np.zeros(n, dtype=bool)
            drop[1:] = (dv > 5.0)
        drop = drop & valid
        if drop.any():
            mask |= drop
            reasons["spo2_sudden_drop"] = int(drop.sum())
    elif family == "HR" or family == "PLS":
        # Step >20 bpm between samples; =0; flatline >10 s.
        dv = np.abs(np.diff(np.where(valid, values, np.nan)))
        with np.errstate(invalid="ignore"):
            step = np.zeros(n, dtype=bool)
            step[1:] = (dv > 20.0)
        step = step & valid
        if step.any():
            mask |= step
            reasons["hr_step_gt20"] = int(step.sum())
        zero = valid & (values == 0)
        if zero.any():
            mask |= zero
            reasons["hr_zero"] = int(zero.sum())
    elif family in ("ART_SYS", "ART_DIA", "ART_MEAN"):
        # Flatline >10 s (transducer zero / damped line).
        flat = _flatline_mask(values, t, min_seconds=10.0)
        if flat.any():
            mask |= flat
            reasons["art_flatline_10s"] = int(flat.sum())
    elif family in ("BIS", "NOL"):
        # Frozen >5 min.
        flat = _flatline_mask(values, t, min_seconds=300.0)
        if flat.any():
            mask |= flat
            reasons["index_frozen_5min"] = int(flat.sum())
    elif family == "TEMP":
        # Sudden drop >2 C within minutes.
        dv = np.diff(np.where(valid, values, np.nan))
        with np.errstate(invalid="ignore"):
            drop = np.zeros(n, dtype=bool)
            # >2 C drop over up to 10 min window
            wlen = max(2, int(600.0 / dt))
            for i in range(wlen, n):
                if valid[i] and valid[i - wlen] and \
                        (values[i - wlen] - values[i] > 2.0):
                    drop[i - wlen:i + 1] = True
        drop = drop & valid
        if drop.any():
            mask |= drop
            reasons["temp_sudden_drop"] = int(drop.sum())
    elif family == "RR":
        # Sudden doubling/halving vs local median.
        med = pd.Series(values).rolling(
            max(3, int(60.0 / dt)), center=True,
            min_periods=1).median().to_numpy()
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = values / np.where(med == 0, np.nan, med)
            dbl = valid & ((ratio > 1.8) | (ratio < 0.55))
        if dbl.any():
            mask |= dbl
            reasons["rr_double_half"] = int(dbl.sum())
    elif family == "ETCO2":
        zero = valid & (values == 0)
        if zero.any():
            mask |= zero
            reasons["etco2_zero"] = int(zero.sum())
    # NBP/CVP/PA/ST: hard bounds only (intermittent/gappy signals must not
    # be over-processed).
    return mask, reasons


def _flatline_mask(values: np.ndarray, t: np.ndarray,
                   min_seconds: float) -> np.ndarray:
    """True where the signal is bit-identical for >= min_seconds."""
    n = len(values)
    out = np.zeros(n, dtype=bool)
    if n < 2:
        return out
    dt = np.median(np.diff(t)) if len(t) > 1 else 1.0
    dt = dt if dt > 0 else 1.0
    need = max(2, int(min_seconds / dt))
    d = np.abs(np.diff(values))
    flat_run = 0
    for i in range(1, n):
        if not np.isnan(values[i]) and not np.isnan(values[i - 1]) \
                and d[i - 1] == 0:
            flat_run += 1
        else:
            if flat_run >= need:
                out[i - flat_run:i] = True
            flat_run = 0
    if flat_run >= need:
        out[n - flat_run:n] = True
    return out


# ---------------------------------------------------------------------------
# 5. Main entry point
# ---------------------------------------------------------------------------
TIME_LIKE = {"timestamp", "time", "datetime", "date_time", "time_ms",
             "time_s", "epoch_ms", "epoch_s"}


def smart_filter_frame(df: pd.DataFrame, t_seconds: np.ndarray,
                       svc=None) -> tuple:
    """Knowledge-driven filtering with safety rails.

    Returns (filt_df, qc_df, filter_log). filt_df has the same shape/columns
    as df. qc_df has one {col}__qc column per signal column with flags:
      VALID | ARTIFACT_DICT | ARTIFACT_ML | ARTIFACT_BOTH |
      KEPT_OVERFILTER_REVIEW | KEPT_WOULD_WIPE | PASSTHROUGH | NON_NUMERIC
    """
    n = len(df)
    t = np.asarray(t_seconds, dtype=float)
    if len(t) != n:
        t = np.arange(n, dtype=float)

    filt = df.copy()
    qc_cols = {}
    log = {"columns": {}, "models_used": 0, "dict_only": 0,
           "passthrough": 0, "review_flags": 0, "critical_flags": 0,
           "mode": "smart_filter"}

    for col in df.columns:
        if str(col).lower() in TIME_LIKE:
            continue
        entry = {"family": None, "model": None, "removed": 0,
                 "removal_frac": 0.0, "reasons": {}, "decision": None}
        try:
            if not pd.api.types.is_numeric_dtype(df[col]):
                qc_cols[col + "__qc"] = pd.Series(
                    ["NON_NUMERIC"] * n, index=df.index)
                entry["decision"] = "NON_NUMERIC"
                log["columns"][str(col)] = entry
                continue
            values = df[col].to_numpy(dtype=float)
            valid = ~np.isnan(values)
            n_valid = int(valid.sum())
            if n_valid == 0:
                qc_cols[col + "__qc"] = pd.Series(
                    ["EMPTY_INPUT"] * n, index=df.index)
                entry["decision"] = "EMPTY_INPUT"
                log["columns"][str(col)] = entry
                continue

            family = classify_family(col)
            entry["family"] = family
            kb = KNOWLEDGE_BASE.get(family)

            if kb is None or kb.get("passthrough") or family == "OTHER":
                # Waveforms and unmapped columns: never filtered here.
                qc_cols[col + "__qc"] = pd.Series(
                    ["PASSTHROUGH"] * n, index=df.index)
                entry["decision"] = "PASSTHROUGH"
                log["passthrough"] += 1
                log["columns"][str(col)] = entry
                continue

            # Dictionary pass.
            dmask, reasons = dict_artifact_mask(values, t, family)
            entry["reasons"].update(reasons)

            # ML pass (falls back gracefully to dictionary-only).
            model = get_model(svc, col)
            mmask = ml_artifact_mask(values, model, signal_name=col) if model is not None \
                else np.zeros(n, dtype=bool)
            if model is not None:
                entry["model"] = "ml_supervised_rf"
                log["models_used"] += 1
                if mmask.any():
                    entry["reasons"]["ml_predicted_artifact"] = \
                        int(mmask.sum())
            else:
                log["dict_only"] += 1

            combined = (dmask | mmask) & valid
            removal_frac = float(combined.sum()) / n_valid
            entry["removed"] = int(combined.sum())
            entry["removal_frac"] = round(removal_frac, 4)

            # ---- SAFETY RAILS (non-negotiable) ----
            flags = np.array(["VALID"] * n, dtype=object)
            if removal_frac >= RAIL_WIPE_FRAC and n_valid > 0:
                # Would wipe ~everything: NEVER do it.
                entry["decision"] = "KEPT_WOULD_WIPE"
                flags[valid] = "KEPT_WOULD_WIPE"
                log["critical_flags"] += 1
            elif removal_frac > RAIL_REVIEW_FRAC:
                # >50% removal: keep original, flag for human review.
                entry["decision"] = "KEPT_OVERFILTER_REVIEW"
                flags[valid] = "KEPT_OVERFILTER_REVIEW"
                log["review_flags"] += 1
            else:
                entry["decision"] = "FILTERED"
                out = values.copy()
                both = dmask & mmask & valid
                only_d = dmask & ~mmask & valid
                only_m = mmask & ~dmask & valid
                out[combined] = np.nan
                filt[col] = out
                flags[both] = "ARTIFACT_BOTH"
                flags[only_d] = "ARTIFACT_DICT"
                flags[only_m] = "ARTIFACT_ML"
            qc_cols[col + "__qc"] = pd.Series(flags, index=df.index)
            log["columns"][str(col)] = entry
        except Exception as e:
            # One bad column must never kill the patient.
            print(f"[smart-filter] column {col} failed: {e} "
                  f"-> passthrough", flush=True)
            qc_cols[col + "__qc"] = pd.Series(
                ["ERROR_PASSTHROUGH"] * n, index=df.index)
            entry["decision"] = "ERROR_PASSTHROUGH"
            entry["error"] = str(e)[:200]
            log["columns"][str(col)] = entry

    qc = pd.DataFrame(qc_cols, index=df.index)
    print(f"[smart-filter] done: {len(log['columns'])} cols, "
          f"{log['models_used']} with ML, {log['dict_only']} dict-only, "
          f"{log['passthrough']} passthrough, {log['review_flags']} review, "
          f"{log['critical_flags']} critical", flush=True)
    return filt, qc, log
