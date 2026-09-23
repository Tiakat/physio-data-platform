from pathlib import Path
import pandas as pd

root = Path(
    r"C:\Users\katia\Dropbox\Liam\Projects actifs\V-RAPS\Database\RawData\RawData"
)

files = sorted(root.rglob("*.csv"))

bc = [
    p for p in files
    if "bettercare" in str(p).lower()
]

print("BetterCare CSV files:", len(bc))

headers = {}

for f in bc:
    header = tuple(
        pd.read_csv(
            f,
            sep=None,
            engine="python",
            nrows=0,
        ).columns.tolist()
    )

    headers.setdefault(header, []).append(f)

print("Unique header layouts:", len(headers))

for header, paths in headers.items():
    print()
    print("COUNT:", len(paths))
    print("EXAMPLE:", paths[0])
    print("COLUMNS:")

    for column in header:
        print("  ", column)
