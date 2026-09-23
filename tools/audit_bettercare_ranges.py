from pathlib import Path
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LOCAL_STORE = ROOT / "local_store"

records = []

for catalog_path in sorted(LOCAL_STORE.glob("*/catalog.json")):
    project = catalog_path.parent.name
    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    entries = data.get("entries", data) if isinstance(data, dict) else data

    for entry in entries:
        if str(entry.get("device") or "").strip().lower() != "bettercare":
            continue

        parquet = entry.get("parquet")
        if not parquet:
            continue

        path = LOCAL_STORE / project / parquet

        if path.exists():
            records.append((project, path))

print("=" * 100)
print("BETTERCARE VALUE-RANGE AUDIT")
print("=" * 100)
print(f"Parsed BetterCare recordings: {len(records)}")

frames = []

for project, path in records:
    try:
        df = pd.read_parquet(path)
        df["_project_"] = project
        frames.append(df)
    except Exception as exc:
        print(f"[ERROR] {project}: {path}")
        print(f"        {exc}")

if not frames:
    raise SystemExit("No BetterCare Parquet files could be read.")

df = pd.concat(frames, ignore_index=True)

print(f"Total rows: {len(df):,}")
print()

numeric = df.select_dtypes(include="number").drop(
    columns=["_project_"],
    errors="ignore",
)

quantiles = numeric.quantile(
    [0.001, 0.01, 0.05, 0.50, 0.95, 0.99, 0.999]
).T

quantiles.columns = [
    "q0.1%",
    "q1%",
    "q5%",
    "median",
    "q95%",
    "q99%",
    "q99.9%",
]

quantiles["min"] = numeric.min()
quantiles["max"] = numeric.max()
quantiles["NaN"] = numeric.isna().sum()
quantiles["non_NaN"] = numeric.notna().sum()

quantiles = quantiles[
    [
        "min",
        "q0.1%",
        "q1%",
        "q5%",
        "median",
        "q95%",
        "q99%",
        "q99.9%",
        "max",
        "NaN",
        "non_NaN",
    ]
]

print(quantiles.to_string())

print()
print("=" * 100)
print("AUDIT COMPLETE")
print("=" * 100)
