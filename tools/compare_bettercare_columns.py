from pathlib import Path
import json
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
LOCAL_STORE = ROOT / "local_store"
OUT = ROOT / "reports" / "bettercare"
OUT.mkdir(parents=True, exist_ok=True)

rows = []

for catalog_path in sorted(LOCAL_STORE.glob("*/catalog.json")):
    project = catalog_path.parent.name

    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
        entries = data.get("entries", data) if isinstance(data, dict) else data
    except Exception as exc:
        print(f"[ERROR] {project}: {exc}")
        continue

    for entry in entries:
        if str(entry.get("device") or "").strip().lower() != "bettercare":
            continue

        parquet = entry.get("parquet")
        if not parquet:
            continue

        path = LOCAL_STORE / project / parquet
        if not path.exists():
            print(f"[MISSING] {project}: {path}")
            continue

        try:
            df = pd.read_parquet(path)
        except Exception as exc:
            print(f"[ERROR] {project}: {path} -> {exc}")
            continue

        for col in df.columns:
            rows.append({
                "project": project,
                "patient": entry.get("patient"),
                "column": str(col),
                "dtype": str(df[col].dtype),
                "rows": len(df),
                "non_null": int(df[col].notna().sum()),
                "nan": int(df[col].isna().sum()),
            })

audit = pd.DataFrame(rows)

if audit.empty:
    raise SystemExit("No parsed BetterCare Parquet files found.")

# One row per project × column
project_columns = (
    audit.groupby(["project", "column"], as_index=False)
    .agg(
        recordings=("patient", "count"),
        rows=("rows", "sum"),
        non_null=("non_null", "sum"),
        nan=("nan", "sum"),
    )
)

project_columns["missing_pct"] = (
    project_columns["nan"] / project_columns["rows"] * 100
).round(2)

project_columns.to_csv(
    OUT / "bettercare_columns_by_project.csv",
    index=False,
    encoding="utf-8-sig",
)

# Matrix: columns × projects
matrix = (
    project_columns
    .assign(present="YES")
    .pivot(index="column", columns="project", values="present")
    .fillna("")
    .reset_index()
)

matrix.to_csv(
    OUT / "bettercare_column_comparison.csv",
    index=False,
    encoding="utf-8-sig",
)

# Overall list
overall = (
    project_columns.groupby("column")
    .agg(
        projects=("project", "nunique"),
        recordings=("recordings", "sum"),
        total_rows=("rows", "sum"),
        total_non_null=("non_null", "sum"),
        total_nan=("nan", "sum"),
    )
    .reset_index()
)

overall["missing_pct"] = (
    overall["total_nan"] / overall["total_rows"] * 100
).round(2)

overall = overall.sort_values(
    ["projects", "column"],
    ascending=[False, True]
)

overall.to_csv(
    OUT / "bettercare_columns_overall.csv",
    index=False,
    encoding="utf-8-sig",
)

print()
print("=" * 100)
print("BETTERCARE — CROSS-PROJECT COLUMN COMPARISON")
print("=" * 100)
print(f"BetterCare Parquet recordings: {audit['patient'].notna().sum()}")
print(f"Projects containing BetterCare: {project_columns['project'].nunique()}")
print(f"Unique columns: {overall['column'].nunique()}")
print()

print("COLUMN × PROJECT")
print()
print(matrix.to_string(index=False))

print()
print("=" * 100)
print("OVERALL COLUMN SUMMARY")
print("=" * 100)
print(overall.to_string(index=False))

print()
print(f"Reports:")
print(f"  {OUT / 'bettercare_column_comparison.csv'}")
print(f"  {OUT / 'bettercare_columns_by_project.csv'}")
print(f"  {OUT / 'bettercare_columns_overall.csv'}")
