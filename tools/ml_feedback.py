"""ML feedback loop: supervised models learn from filtering mistakes.

Reads the overfiltering report, extracts "falsely removed" points
(within norms but filtered out), adds them as negative examples (clean),
and retrains the supervised models with corrected labels.

The loop: Filter → Validate (strict) → Learn from mistakes → Better filter
"""
import os, sys, io, json, argparse
from pathlib import Path
from collections import defaultdict
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth
import pandas as pd
import numpy as np

# Same norms as detect_overfilter
NORMS = {
    "HR": (30, 200), "SPO2": (70, 100), "NBP_SYS": (60, 250), "NBP_DIA": (30, 150),
    "NBP_MEAN": (40, 180), "ART_SYS": (60, 250), "ART_DIA": (30, 150),
    "ART_MEAN": (40, 180), "TEMP": (32, 42), "RR": (4, 40), "ETCO2": (15, 60),
    "BIS": (0, 100), "NOL": (0, 100),
}

def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet
    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)

def get_norm(col: str):
    upper = col.upper()
    for k, v in NORMS.items():
        if k in upper:
            return v
    return None

def extract_correction_windows(sig: np.ndarray, norm) -> tuple:
    """Extract falsely-removed points as clean training examples.

    Strategy: Find NaN gaps. For short gaps (<10 points) where neighbors
    are within norms, the removed points were likely good → mark as clean.
    Returns (X_corrections, y_corrections) where y=0 (clean).
    """
    if norm is None:
        return np.array([]).reshape(0, 5), np.array([])

    lo, hi = norm
    n = len(sig)
    X_corr, y_corr = [], []

    # Find NaN gaps
    is_nan = np.isnan(sig)
    i = 0
    while i < n:
        if is_nan[i]:
            # Start of gap
            gap_start = i
            while i < n and is_nan[i]:
                i += 1
            gap_end = i
            gap_len = gap_end - gap_start

            # Only consider short gaps (likely false positives)
            if gap_len <= 10 and gap_len >= 2:
                # Check neighbors
                before_idx = gap_start - 1
                after_idx = gap_end
                before_ok = (before_idx >= 0 and not np.isnan(sig[before_idx])
                             and lo <= sig[before_idx] <= hi)
                after_ok = (after_idx < n and not np.isnan(sig[after_idx])
                            and lo <= sig[after_idx] <= hi)

                if before_ok and after_ok:
                    # Neighbors are good → gap was likely falsely removed
                    # Create synthetic clean windows from interpolated values
                    before_val = sig[before_idx]
                    after_val = sig[after_idx]
                    for j in range(gap_start, gap_end):
                        # Linear interpolation
                        alpha = (j - gap_start + 1) / (gap_len + 1)
                        interp_val = before_val * (1 - alpha) + after_val * alpha
                        # Feature vector: [value, local_mean, local_std, norm_dist, gap_pos]
                        window = sig[max(0, j-2):j+3]
                        window = window[~np.isnan(window)]
                        if len(window) > 0:
                            feat = [
                                interp_val,
                                np.mean(window),
                                np.std(window) if len(window) > 1 else 0,
                                min(abs(interp_val - lo), abs(interp_val - hi)),
                                (j - gap_start) / gap_len,
                            ]
                            X_corr.append(feat)
                            y_corr.append(0)  # 0 = clean (not artifact)
        else:
            i += 1

    if X_corr:
        return np.array(X_corr), np.array(y_corr)
    return np.array([]).reshape(0, 5), np.array([])

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--overfilter-report", default="overfilter/report.json",
                    help="Path to overfilter report")
    ap.add_argument("--out", default="ml_feedback",
                    help="Output directory")
    ap.add_argument("--max-patients", type=int, default=10,
                    help="Max patients to process for corrections")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "retrained_models").mkdir(exist_ok=True)

    # Load overfilter report
    report_path = Path(args.overfilter_report)
    if not report_path.exists():
        print(f"[ml-feedback] No overfilter report at {report_path}", flush=True)
        print(f"[ml-feedback] Run detect-overfilter first", flush=True)
        return 1

    report = json.loads(report_path.read_text())
    flagged = [r for r in report["results"]
               if r["flag"] in ("CRITICAL", "WARN", "CRITICAL_EMPTY")]

    print(f"[ml-feedback] {len(flagged)} flagged signal instances", flush=True)

    if not flagged:
        print(f"[ml-feedback] No overfiltering found — nothing to learn", flush=True)
        (out / "report.json").write_text(json.dumps({
            "corrections_extracted": 0,
            "models_retrained": 0,
            "message": "No overfiltering detected",
        }, indent=1))
        return 0

    # Group by project/patient/signal
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")

    corrections_by_signal = defaultdict(lambda: {"X": [], "y": []})
    patients_processed = 0

    # Process flagged patients (limit to max_patients)
    seen = set()
    for f in flagged:
        key = (f["project"], f["patient"])
        if key in seen:
            continue
        if len(seen) >= args.max_patients:
            break
        seen.add(key)

        proj, patient, signal = f["project"], f["patient"], f["signal"]
        blob = f"processed/level2/{proj}/{patient}/filtered.parquet.enc"

        try:
            raw = proc.get_blob_client(blob).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
        except Exception as e:
            print(f"[ml-feedback] Skip {proj}/{patient}: {e}", flush=True)
            continue

        if signal not in df.columns:
            continue

        sig = pd.to_numeric(df[signal], errors="coerce").values
        norm = get_norm(signal)

        X_corr, y_corr = extract_correction_windows(sig, norm)
        if len(X_corr) > 0:
            family = signal.split("_")[0].upper() if "_" in signal else signal[:4].upper()
            corrections_by_signal[family]["X"].append(X_corr)
            corrections_by_signal[family]["y"].append(y_corr)
            print(f"[ml-feedback] {proj}/{patient}/{signal}: "
                  f"{len(X_corr)} correction examples", flush=True)

        patients_processed += 1

    # Retrain models with corrections
    # For now, save the correction datasets — full retraining happens
    # in the next ml-sup-train run which will pick these up
    total_corrections = 0
    for family, data in corrections_by_signal.items():
        if data["X"]:
            X = np.vstack(data["X"])
            y = np.concatenate(data["y"])
            total_corrections += len(X)
            # Save correction dataset
            np.save(out / "retrained_models" / f"{family}_X_corr.npy", X)
            np.save(out / "retrained_models" / f"{family}_y_corr.npy", y)
            print(f"[ml-feedback] {family}: saved {len(X)} correction examples",
                  flush=True)

    # Write report
    feedback_report = {
        "patients_processed": patients_processed,
        "flagged_signals": len(flagged),
        "corrections_extracted": int(total_corrections),
        "families_corrected": list(corrections_by_signal.keys()),
        "method": "Short NaN gaps with in-norm neighbors marked as clean (y=0). "
                  "These correction datasets will be merged into the next "
                  "supervised training run as negative examples.",
        "next_step": "Run ml-sup-train with --corrections-dir ml_feedback/retrained_models "
                     "to retrain models with the corrected labels.",
    }
    (out / "report.json").write_text(json.dumps(feedback_report, indent=1))

    print(f"\n[ml-feedback] Done: {total_corrections} correction examples "
          f"from {patients_processed} patients", flush=True)
    print(f"[ml-feedback] Report: {out}/report.json", flush=True)

    return 0

if __name__ == "__main__":
    sys.exit(main())
