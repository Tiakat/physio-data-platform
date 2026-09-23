import csv
from collections import Counter

folders = Counter()

with open("azure_full_inventory.csv", encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)

    for row in reader:
        parts = row["full_path"].split("/")

        for i in range(1, len(parts)):
            folders["/".join(parts[:i])] += 1

print()
print("FULL FOLDER TREE")
print("================")
print()

for folder, count in sorted(folders.items()):
    print(f"{count:7}  {folder}/")
