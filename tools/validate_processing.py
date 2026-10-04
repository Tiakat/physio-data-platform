"""Validate processing output quality: check filtered data against signal norms,
verify graphs exist and are valid, report anomalies."""
import os, sys, io, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth
import pandas as pd
import numpy as np

# Signal norms from the dictionary (normal ranges)
NORMS = {
    "HR": (30, 200), "SPO2": (70, 100), "NBP_SYS": (60, 250), "NBP_DIA": (30, 150),
    "ART_SYS": (60, 250), "ART_DIA": (30, 150), "ART_MEAN": (40, 180),
    "TEMP": (32, 42), "RR": (4, 40), "ETCO2": (15, 60), "BIS": (0, 100),
    "NOL": (0, 100),
}

def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet
    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")
    graphs_c = svc.get_container_client("graphs")

    # Sample 3 patients from different projects
    samples = []
    seen = set()
    for b in proc.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith("/filtered.parquet.enc"):
            parts = name.split("/")
            proj = parts[2] if len(parts) > 2 else "?"
            if proj not in seen and len(seen) < 3:
                seen.add(proj)
                samples.append(name)
        if len(samples) >= 3:
            break

    print(f"[validate] Checking {len(samples)} patients", flush=True)
    issues = []

    for s in samples:
        parts = s.split("/")
        proj, patient = parts[2], parts[3]
        print(f"\n[validate] {proj}/{patient}", flush=True)

        # Download filtered data
        raw = proc.get_blob_client(s).download_blob().readall()
        df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
        print(f"  Rows: {len(df)}, Cols: {len(df.columns)}", flush=True)

        # Check each column against norms
        for col in df.columns:
            if col == "timestamp":
                continue
            vals = df[col].dropna()
            if len(vals) == 0:
                print(f"  {col}: EMPTY (all NaN)", flush=True)
                continue
            # Find matching norm
            norm = None
            for k, v in NORMS.items():
                if k in col.upper():
                    norm = v
                    break
            if norm:
                lo, hi = norm
                out = ((vals < lo) | (vals > hi)).sum()
                pct = 100 * out / len(vals)
                status = "OK" if pct < 5 else "WARN"
                print(f"  {col}: range [{vals.min():.1f}, {vals.max():.1f}] "
                      f"norm [{lo}, {hi}] out-of-range: {pct:.1f}% [{status}]", flush=True)
                if pct >= 5:
                    issues.append(f"{proj}/{patient}/{col}: {pct:.1f}% out of norm range")
            else:
                print(f"  {col}: range [{vals.min():.1f}, {vals.max():.1f}] (no norm)", flush=True)

        # Check graphs exist
        gprefix = f"{proj}/{patient}/"
        gfiles = [b["name"] if isinstance(b, dict) else b.name
                  for b in graphs_c.list_blobs(name_starts_with=gprefix)]
        n_data_cols = len([c for c in df.columns if c != "timestamp"
                          and df[c].notna().sum() > 0])
        print(f"  Graphs in Azure: {len(gfiles)}, data columns: {n_data_cols}", flush=True)
        if len(gfiles) < n_data_cols:
            issues.append(f"{proj}/{patient}: only {len(gfiles)} graphs for {n_data_cols} columns")
            print(f"  WARNING: missing graphs!", flush=True)

    print(f"\n[validate] Done. {len(issues)} issues found.", flush=True)
    for i in issues:
        print(f"  ISSUE: {i}", flush=True)

    # Write report
    Path("validation_report").mkdir(exist_ok=True)
    Path("validation_report/issues.json").write_text(json.dumps({
        "samples": samples, "issues": issues}, indent=1))
    return 1 if issues else 0

if __name__ == "__main__":
    sys.exit(main())
