"""Pilot: Phase 9+ filtering on a few REAL decrypted parquets.

Runs inside GitHub Actions (PIPELINE_DATA_KEY never leaves the runner).
Decrypts a small sample of parquets, runs the signal-processing engine
with the current configs (status: draft -- intervals provisional), and
writes per-file filtered CSV + QC CSV + raw-vs-filtered PNG.

Outputs are de-identified signal traces (no names/dates); uploaded as a
workflow artifact for review.

Usage:
  python -m tools.pilot_filter --per-project 1 --max-total 3 \
      --signals HR,SPO2 --max-rows 20000 --out filter_pilot_out/
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402
from tools.signal_processing import (  # noqa: E402
    find_config, get_var_spec, load_signal_configs, process_frame)


def _project_of(blob: str) -> str:
    parts = blob.split("/")
    return parts[0] if parts else "unknown"


def _pick_signal_columns(df: pd.DataFrame, configs, index, wanted) -> list[str]:
    cols = []
    for c in df.columns:
        cfg, canonical = find_config(c, configs, index)
        if canonical and canonical.upper() in wanted:
            cols.append(c)
    return cols


def _data_rich_window(df: pd.DataFrame, configs, index, wanted,
                      max_rows: int) -> pd.DataFrame:
    """Select max_rows where the wanted signals actually have data.

    BetterCare files mix ~200 Hz waveforms with ~1 Hz parameters; the
    first N rows can be waveform-only. Find the first row where any
    wanted signal is non-null and take the window from there.
    """
    sig_cols = _pick_signal_columns(df, configs, index, wanted)
    if not sig_cols or len(df) <= max_rows:
        return df.iloc[:max_rows] if len(df) > max_rows else df
    # Prefer rows where wanted signals are plausibly VALID (in dictionary
    # range), not just non-null: device-off zeros and artifacts are
    # non-null but useless for a prototype.
    has_good = pd.Series(False, index=df.index)
    has_any = pd.Series(False, index=df.index)
    for c in sig_cols:
        cfg, canonical = find_config(c, configs, index)
        v = pd.to_numeric(df[c], errors="coerce")
        has_any |= v.notna()
        var_spec = get_var_spec(canonical, index) if canonical else None
        lo, hi = None, None
        if var_spec:
            lo, hi = var_spec.get("min"), var_spec.get("max")
        if lo is not None and hi is not None:
            has_good |= (v >= lo) & (v <= hi)
        else:
            has_good |= v.notna()
    target = has_good if has_good.any() else has_any
    if not target.any():
        print("[pilot-filter] wanted signals have no data in file; "
              "using first rows", flush=True)
        return df.iloc[:max_rows]
    first = target.idxmax()
    pos = df.index.get_loc(first)
    # Center the window on the data when possible.
    start = max(0, min(pos - max_rows // 4, len(df) - max_rows))
    print(f"[pilot-filter] window at row {start} "
          f"({'in-range' if has_good.any() else 'non-null'} data)", flush=True)
    return df.iloc[start:start + max_rows]


def _graph(raw: pd.DataFrame, filtered: pd.DataFrame, qc: pd.DataFrame,
           signals: list[str], path: Path, title: str) -> bool:
    t = pd.to_datetime(filtered["timestamp"], errors="coerce")
    n = len(signals)
    if n == 0:
        return False
    fig, axes = plt.subplots(n, 1, figsize=(12, 3.2 * n), sharex=True,
                             squeeze=False)
    axes = axes[:, 0]
    for ax, col in zip(axes, signals):
        ax.plot(t, raw[col], color="0.7", lw=0.8, label=f"{col} raw")
        ax.plot(t, filtered[col], color="tab:red", lw=1.2,
                label=f"{col} filtered")
        q = qc.get(col + "__qc")
        if q is not None:
            bad = q.to_numpy() != "VALID"
            if bad.any():
                ax.scatter(t.to_numpy()[bad], raw[col].to_numpy()[bad],
                           color="black", s=12, zorder=5, label="flagged")
        ax.set_ylabel(col)
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3)
    axes[0].set_title(title)
    axes[-1].set_xlabel("time")
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=100)
    plt.close(fig)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Pilot Phase 9+ filtering on sampled real parquets.")
    ap.add_argument("--per-project", type=int, default=1)
    ap.add_argument("--max-total", type=int, default=3)
    ap.add_argument("--signals", default="HR,SPO2")
    ap.add_argument("--max-rows", type=int, default=20000)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    wanted = {s.strip().upper() for s in args.signals.split(",") if s.strip()}
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    configs, index = load_signal_configs(
        Path("configs/signals"), Path("profiles/_variables.yaml"))
    missing_codes = list(index.get("global_missing_codes", []))
    for w in index["unimplemented_detectors"]:
        print(f"[warn] detector {w['detector']!r} ({w['config']}) not "
              f"implemented -- skipping.")
    drafts = [s for s, c in configs.items() if c.get("status") == "draft"]
    print(f"[pilot-filter] DRAFT-INTERVAL PILOT; draft configs: {drafts}",
          flush=True)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")
    by_project: dict[str, list[str]] = {}
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith(".parquet.enc"):
            by_project.setdefault(_project_of(name), []).append(name)
    chosen = []
    for code in sorted(by_project):
        chosen.extend(sorted(by_project[code])[: args.per_project])
        if len(chosen) >= args.max_total:
            break
    chosen = chosen[: args.max_total]
    print(f"[pilot-filter] filtering {len(chosen)} file(s)", flush=True)

    summary = {"tool": "pilot_filter",
               "ts": datetime.now(timezone.utc).isoformat(),
               "draft_intervals": True,
               "files": []}
    with tempfile.TemporaryDirectory(prefix="pilot_filter_") as tmp:
        for i, blob in enumerate(chosen):
            raw = svc.get_blob_client(
                container="rawdata", blob=blob).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
            df = _data_rich_window(df, configs, index, wanted,
                                   args.max_rows)
            token = _project_of(blob).lower() + f"_{i:02d}"
            filt, qc, review = process_frame(
                df, None, configs, index, missing_codes, source="pilot")
            sig_cols = _pick_signal_columns(df, configs, index, wanted)
            graph_ok = False
            if sig_cols:
                graph_ok = _graph(
                    df[sig_cols], filt, qc, sig_cols,
                    out / f"{token}_graph.png",
                    f"REAL DATA pilot (draft intervals) — {token}: "
                    f"raw vs filtered (black = flagged)")
            filt.to_csv(out / f"{token}_filtered.csv", index=False)
            qc.to_csv(out / f"{token}_qc.csv", index=False)
            n_flag = int((qc.filter(like="__qc")
                            .apply(lambda c: c != "VALID")).sum().sum())
            entry = {"blob_project": _project_of(blob),
                     "rows": len(df), "signals_graphed": sig_cols,
                     "n_flagged": n_flag,
                     "n_review": len(review), "graph": graph_ok}
            summary["files"].append(entry)
            print(f"[pilot-filter] {token}: rows={len(df)} "
                  f"flagged={n_flag} review={len(review)}", flush=True)

    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(f"[pilot-filter] done -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
