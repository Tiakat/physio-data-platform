"""Aggressive overfiltering detector — "on edge" validator for filtering quality.

For each patient/signal, checks:
- Removal rate: % of data points filtered out (NaN in filtered parquet)
- Signal-specific thresholds (SpO2 stricter, NBP exempt from NaN check)
- Empty columns: <10% data remaining = critical
- Good data loss: flags when filtering removes too much

Output: overfilter/report.json with per-patient/per-signal rates and flags.
Exit code 1 if critical overfiltering found, 0 otherwise.
"""
import os, sys, io, json, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth
import pandas as pd
import numpy as np

# Signal norms (physiological ranges)
NORMS = {
    "HR": (30, 200), "SPO2": (70, 100), "NBP_SYS": (60, 250), "NBP_DIA": (30, 150),
    "NBP_MEAN": (40, 180), "ART_SYS": (60, 250), "ART_DIA": (30, 150),
    "ART_MEAN": (40, 180), "TEMP": (32, 42), "RR": (4, 40), "ETCO2": (15, 60),
    "BIS": (0, 100), "NOL": (0, 100),
}

# Signal-specific overfiltering thresholds (max acceptable NaN %)
# SpO2: very strict (flat at 98-99% is NORMAL, not artifact)
# NBP: exempt (intermittent cuff — gaps are normal)
SIGNAL_THRESHOLDS = {
    "SPO2": {"warn": 15, "critical": 30},
    "HR": {"warn": 30, "critical": 50},
    "ART": {"warn": 30, "critical": 50},
    "RR": {"warn": 30, "critical": 50},
    "TEMP": {"warn": 20, "critical": 40},
    "ETCO2": {"warn": 30, "critical": 50},
    "BIS": {"warn": 25, "critical": 45},
    "NOL": {"warn": 25, "critical": 45},
    "DEFAULT": {"warn": 30, "critical": 50},
}

# Signals exempt from NaN-rate check (intermittent by design)
NAN_EXEMPT = {"NBP", "NIBP"}

def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet
    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)

def get_signal_family(col: str) -> str:
    """Map column name to signal family for threshold lookup."""
    upper = col.upper()
    for fam in ["SPO2", "HR", "ART", "NBP", "NIBP", "RR", "TEMP", "ETCO2", "BIS", "NOL"]:
        if fam in upper:
            return fam
    return "DEFAULT"

def get_norm(col: str):
    """Get physiological norm range for a column."""
    upper = col.upper()
    for k, v in NORMS.items():
        if k in upper:
            return v
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-patients", type=int, default=20,
                    help="Max patients to check (default 20)")
    ap.add_argument("--warn-threshold", type=float, default=30,
                    help="Default warn threshold % (default 30)")
    ap.add_argument("--critical-threshold", type=float, default=50,
                    help="Default critical threshold % (default 50)")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")

    # Sample patients across projects
    samples = []
    seen_projects = set()
    for b in proc.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith("/filtered.parquet.enc"):
            parts = name.split("/")
            if len(parts) >= 4:
                proj = parts[2]
                # Get up to 3 per project, max 20 total
                proj_count = sum(1 for s in samples if s.split("/")[2] == proj)
                if proj_count < 3 and len(samples) < args.max_patients:
                    samples.append(name)
        if len(samples) >= args.max_patients:
            break

    print(f"[detect-overfilter] Checking {len(samples)} patients", flush=True)

    results = []
    critical_count = 0
    warn_count = 0

    for s in samples:
        parts = s.split("/")
        proj, patient = parts[2], parts[3]
        print(f"\n[detect-overfilter] {proj}/{patient}", flush=True)

        try:
            raw = proc.get_blob_client(s).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
        except Exception as e:
            print(f"  ERROR loading: {e}", flush=True)
            continue

        print(f"  Rows: {len(df)}, Cols: {len(df.columns)}", flush=True)

        for col in df.columns:
            if col.lower() in ("timestamp", "time", "t"):
                continue

            vals = pd.to_numeric(df[col], errors="coerce")
            total = len(vals)
            nan_count = vals.isna().sum()
            nan_pct = 100 * nan_count / total if total > 0 else 0
            remaining_pct = 100 - nan_pct

            family = get_signal_family(col)
            is_exempt = any(ex in family for ex in NAN_EXEMPT)

            # Get thresholds for this signal family
            thresh = SIGNAL_THRESHOLDS.get(family, SIGNAL_THRESHOLDS["DEFAULT"])
            warn_th = thresh["warn"]
            crit_th = thresh["critical"]

            # Check for empty columns (<10% remaining)
            is_empty = remaining_pct < 10

            # Determine flag level
            flag = None
            if is_empty:
                flag = "CRITICAL_EMPTY"
                critical_count += 1
            elif not is_exempt:
                if nan_pct >= crit_th:
                    flag = "CRITICAL"
                    critical_count += 1
                elif nan_pct >= warn_th:
                    flag = "WARN"
                    warn_count += 1

            # Check remaining values against norms (are we keeping bad data?)
            norm = get_norm(col)
            out_of_norm_pct = None
            if norm and remaining_pct > 0:
                clean = vals.dropna()
                if len(clean) > 0:
                    lo, hi = norm
                    out = ((clean < lo) | (clean > hi)).sum()
                    out_of_norm_pct = 100 * out / len(clean)

            result = {
                "project": proj,
                "patient": patient,
                "signal": col,
                "family": family,
                "total_points": int(total),
                "removed_pct": round(float(nan_pct), 2),
                "remaining_pct": round(float(remaining_pct), 2),
                "flag": flag,
                "threshold_warn": warn_th,
                "threshold_critical": crit_th,
                "nan_exempt": is_exempt,
                "out_of_norm_pct": round(float(out_of_norm_pct), 2) if out_of_norm_pct is not None else None,
            }
            results.append(result)

            if flag:
                print(f"  {flag}: {col} — {nan_pct:.1f}% removed "
                      f"(warn>{warn_th}%, crit>{crit_th}%)", flush=True)
            elif not is_exempt and nan_pct > 5:
                print(f"  OK: {col} — {nan_pct:.1f}% removed", flush=True)

    # Write report
    outdir = Path("overfilter")
    outdir.mkdir(exist_ok=True)
    report = {
        "summary": {
            "patients_checked": len(samples),
            "signals_checked": len(results),
            "warn_count": warn_count,
            "critical_count": critical_count,
            "thresholds": {
                "default_warn": args.warn_threshold,
                "default_critical": args.critical_threshold,
                "signal_specific": SIGNAL_THRESHOLDS,
            }
        },
        "results": results,
    }
    (outdir / "report.json").write_text(json.dumps(report, indent=1))

    print(f"\n[detect-overfilter] Done: {warn_count} warnings, "
          f"{critical_count} critical", flush=True)
    print(f"[detect-overfilter] Report: overfilter/report.json", flush=True)

    # Exit 1 if critical found (K's "on edge" requirement)
    # But don't crash on warnings — only critical
    return 1 if critical_count > 0 else 0

if __name__ == "__main__":
    sys.exit(main())
