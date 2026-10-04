"""Validate processed signal distributions against published external references.

Compares pooled filtered-signal distributions from Azure (processed container)
against:
  1. Literature norms in docs/signal_norms_labeling_ground_truth.md and
     docs/norms_batch{1,2,3}.md (intraoperative monitoring context) -- primary gate.
  2. Published MIMIC-III / MIMIC-IV vital-sign distributions -- secondary,
     informational reference (ICU population; intraoperative values under
     general anesthesia are expected to trend lower, so MIMIC z-scores are
     reported, not used as hard gates).

For each major signal it computes pooled mean/std/percentiles across sampled
patients, the fraction of values outside the literature plausible range, and
z-scores vs each MIMIC reference. Flags are written to
external_validation_report/report.json.

Exit code is always 0; flags live in the report (avoids failure-email noise).
"""
import os, sys, io, json, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth
import pandas as pd
import numpy as np

# ----------------------------------------------------------------------------
# Reference definitions.
# lit_normal: literature normal/target range (docs/signal_norms_*.md)
# lit_plausible: literature plausible/clean bounds; values outside are
#                treated as likely residual artifacts (docs, "Clean vs artifact")
# mimic: list of (source_label, mean, sd_or_None) from published MIMIC studies
# ----------------------------------------------------------------------------
SIGNAL_DEFS = {
    "HR": {
        "tokens": [("HR",)],
        "unit": "bpm",
        "lit_normal": (60, 100), "lit_intraop": (50, 100), "lit_plausible": (15, 300),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 85.0, 18.4),
            ("MIMIC-III AMI n=1735 springermedizin.de", 85.4, 17.8),
            ("Validated vitals adults 21+ TOMINFOJ", 83.49, None),
        ],
    },
    "SPO2": {
        "tokens": [("SPO2",)],
        "unit": "%",
        "lit_normal": (95, 100), "lit_intraop": (95, 100), "lit_plausible": (70, 100),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 96.18, 5.20),
            ("Validated vitals adults 21+ TOMINFOJ", 98.02, None),
        ],
    },
    "NBP_SYS": {
        "tokens": [("NBP", "SYS"), ("NIBP", "SYS")],
        "unit": "mmHg",
        "lit_normal": (90, 140), "lit_intraop": (90, 140), "lit_plausible": (70, 250),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 113.43, 17.87),
            ("MIMIC-III AMI n=1735 springermedizin.de", 120.8, 22.8),
            ("Validated vitals adults 21+ TOMINFOJ", 130.97, None),
        ],
    },
    "NBP_DIA": {
        "tokens": [("NBP", "DIA"), ("NIBP", "DIA")],
        "unit": "mmHg",
        "lit_normal": (50, 90), "lit_intraop": (50, 90), "lit_plausible": (30, 150),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 62.20, 12.16),
            ("MIMIC-III AMI n=1735 springermedizin.de", 65.2, 15.9),
            ("Validated vitals adults 21+ TOMINFOJ", 73.94, None),
        ],
    },
    "NBP_MEAN": {
        "tokens": [("NBP", "MEAN"), ("NIBP", "MEAN"), ("NBP", "MAP"), ("NIBP", "MAP")],
        "unit": "mmHg",
        "lit_normal": (65, 100), "lit_intraop": (65, 100), "lit_plausible": (40, 180),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 76.69, 12.26),
        ],
    },
    "ART_SYS": {
        "tokens": [("ART", "SYS")],
        "unit": "mmHg",
        "lit_normal": (90, 140), "lit_intraop": (90, 140), "lit_plausible": (70, 250),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 113.43, 17.87),
            ("MIMIC-III AMI n=1735 springermedizin.de", 120.8, 22.8),
        ],
    },
    "ART_DIA": {
        "tokens": [("ART", "DIA")],
        "unit": "mmHg",
        "lit_normal": (50, 90), "lit_intraop": (50, 90), "lit_plausible": (30, 150),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 62.20, 12.16),
            ("MIMIC-III AMI n=1735 springermedizin.de", 65.2, 15.9),
        ],
    },
    "ART_MEAN": {
        "tokens": [("ART", "MEAN"), ("ART", "MAP")],
        "unit": "mmHg",
        "lit_normal": (65, 100), "lit_intraop": (65, 100), "lit_plausible": (40, 180),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 76.69, 12.26),
        ],
    },
    "TEMP": {
        "tokens": [("TEMP",)],
        "unit": "degC",
        "lit_normal": (36.5, 37.5), "lit_intraop": (36.0, 37.5), "lit_plausible": (32, 42),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 36.44, 1.16),
            ("Validated vitals adults 21+ TOMINFOJ", 36.15, None),
        ],
    },
    "RR": {
        "tokens": [("RR",), ("RESP", "RATE"), ("RESP",)],
        "unit": "/min",
        "lit_normal": (12, 20), "lit_intraop": (10, 20), "lit_plausible": (4, 40),
        "mimic": [
            ("MIMIC-IV ICU n=1722 PMC10210383", 20.92, 4.43),
            ("MIMIC-III AMI n=1735 springermedizin.de", 18.1, 5.6),
            ("Validated vitals adults 21+ TOMINFOJ", 18.96, None),
        ],
    },
    "ETCO2": {
        "tokens": [("ETCO2",), ("CO2", "ET"), ("END", "TIDAL", "CO2")],
        "unit": "mmHg",
        "lit_normal": (35, 45), "lit_intraop": (35, 45), "lit_plausible": (15, 60),
        "mimic": [],
    },
    "BIS": {
        "tokens": [("BIS",)],
        "unit": "index",
        "lit_normal": (40, 60), "lit_intraop": (40, 60), "lit_plausible": (0, 100),
        "mimic": [],
    },
    "NOL": {
        "tokens": [("NOL",)],
        "unit": "index",
        "lit_normal": (10, 25), "lit_intraop": (10, 25), "lit_plausible": (0, 100),
        "mimic": [],
    },
}

MAX_FILES_PER_PROJECT = 2
MAX_ROWS_PER_FILE = 20000
OUT_OF_PLAUSIBLE_FLAG_PCT = 2.0   # >2% outside literature plausible -> flag
Z_WARN, Z_FLAG = 2.0, 3.0         # MIMIC z-score warn/flag thresholds


def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet
    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)


def col_tokens(col: str):
    return [t for t in re.split(r"[^A-Z0-9]+", col.upper()) if t]


def match_signal(col: str):
    toks = set(col_tokens(col))
    for sig, d in SIGNAL_DEFS.items():
        for tup in d["tokens"]:
            if all(t in toks for t in tup):
                return sig
    return None


def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")

    # Sample up to MAX_FILES_PER_PROJECT filtered parquets per project.
    per_proj = {}
    for b in proc.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith("/filtered.parquet.enc"):
            continue
        parts = name.split("/")
        proj = parts[2] if len(parts) > 2 else "?"
        lst = per_proj.setdefault(proj, [])
        if len(lst) < MAX_FILES_PER_PROJECT:
            lst.append(name)

    files = [f for lst in per_proj.values() for f in lst]
    print(f"[validate-external] sampling {len(files)} files "
          f"across {len(per_proj)} projects", flush=True)

    acc = {sig: [] for sig in SIGNAL_DEFS}
    file_means = {sig: [] for sig in SIGNAL_DEFS}
    n_ok, n_fail = 0, 0

    for f in files:
        try:
            raw = proc.get_blob_client(f).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
        except Exception as e:
            print(f"[validate-external] SKIP {f}: {e}", flush=True)
            n_fail += 1
            continue
        n_ok += 1
        if len(df) > MAX_ROWS_PER_FILE:
            df = df.iloc[:: len(df) // MAX_ROWS_PER_FILE]
        for col in df.columns:
            if col.lower() == "timestamp":
                continue
            sig = match_signal(col)
            if not sig:
                continue
            vals = pd.to_numeric(df[col], errors="coerce").dropna().to_numpy()
            vals = vals[np.isfinite(vals)]
            if len(vals) == 0:
                continue
            acc[sig].append(vals)
            file_means[sig].append(float(np.mean(vals)))

    print(f"[validate-external] loaded {n_ok} files ({n_fail} failed)",
          flush=True)

    report = {"files_sampled": n_ok, "files_failed": n_fail,
              "projects": sorted(per_proj), "signals": {}}
    n_flags = 0
    for sig, d in SIGNAL_DEFS.items():
        chunks = acc[sig]
        entry = {"unit": d["unit"], "n_patients": len(chunks),
                 "lit_normal": d["lit_normal"],
                 "lit_plausible": d["lit_plausible"],
                 "flags": [], "mimic_comparison": []}
        if not chunks:
            entry["flags"].append("no data sampled for this signal")
            n_flags += 1
            report["signals"][sig] = entry
            print(f"[validate-external] {sig}: NO DATA", flush=True)
            continue
        vals = np.concatenate(chunks)
        mean, sd = float(np.mean(vals)), float(np.std(vals))
        p5, p50, p95 = (float(x) for x in np.percentile(vals, [5, 50, 95]))
        lo, hi = d["lit_plausible"]
        oop = 100.0 * float(np.mean((vals < lo) | (vals > hi)))
        entry.update({
            "n_values": int(len(vals)),
            "mean": round(mean, 3), "std": round(sd, 3),
            "p5": round(p5, 3), "p50": round(p50, 3), "p95": round(p95, 3),
            "out_of_plausible_pct": round(oop, 3),
            "file_mean_min": round(float(np.min(file_means[sig])), 3),
            "file_mean_max": round(float(np.max(file_means[sig])), 3),
        })
        if oop > OUT_OF_PLAUSIBLE_FLAG_PCT:
            entry["flags"].append(
                f"{oop:.2f}% of values outside literature plausible range "
                f"{d['lit_plausible']} -- possible residual artifacts")
            n_flags += 1
        for src, rmean, rsd in d["mimic"]:
            if rsd:
                z = (mean - rmean) / rsd
                verdict = ("FLAG" if abs(z) > Z_FLAG
                           else "warn" if abs(z) > Z_WARN else "ok")
                if verdict != "ok":
                    n_flags += 1
                entry["mimic_comparison"].append({
                    "source": src, "ref_mean": rmean, "ref_sd": rsd,
                    "z": round(z, 2), "verdict": verdict,
                    "note": "ICU reference; intraop values trend lower",
                })
            else:
                pdiff = 100.0 * (mean - rmean) / rmean if rmean else 0.0
                verdict = "warn" if abs(pdiff) > 20 else "ok"
                if verdict != "ok":
                    n_flags += 1
                entry["mimic_comparison"].append({
                    "source": src, "ref_mean": rmean, "ref_sd": None,
                    "pct_diff": round(pdiff, 2), "verdict": verdict,
                    "note": "mean-only reference; ICU population",
                })
        report["signals"][sig] = entry
        flag_txt = f" FLAGS={entry['flags']}" if entry["flags"] else ""
        print(f"[validate-external] {sig}: n={len(vals)} mean={mean:.2f} "
              f"sd={sd:.2f} p5/p50/p95={p5:.1f}/{p50:.1f}/{p95:.1f} "
              f"oop={oop:.2f}%{flag_txt}", flush=True)

    outdir = Path("external_validation_report")
    outdir.mkdir(exist_ok=True)
    (outdir / "report.json").write_text(json.dumps(report, indent=1))
    print(f"[validate-external] done: {n_flags} flags, "
          f"report -> {outdir}/report.json", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
