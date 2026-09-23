import os
import csv
from azure.storage.blob import BlobServiceClient

account = os.environ["AZURE_STORAGE_ACCOUNT"]
key = os.environ["AZURE_STORAGE_KEY"]

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key
)

container = client.get_container_client("rawdata")

output = "azure_full_inventory.csv"

with open(output, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.writer(f)

    writer.writerow([
        "project",
        "full_path",
        "depth",
        "level_1",
        "level_2",
        "level_3",
        "level_4",
        "level_5",
        "level_6",
        "filename",
        "extension",
        "size_bytes",
        "size_mb"
    ])

    count = 0

    for blob in container.list_blobs():
        path = blob.name
        parts = path.split("/")
        filename = parts[-1]

        if "." in filename:
            extension = filename.rsplit(".", 1)[-1].lower()
        else:
            extension = ""

        levels = parts[:-1]

        writer.writerow([
            parts[0] if len(parts) > 0 else "",
            path,
            len(parts),
            levels[0] if len(levels) > 0 else "",
            levels[1] if len(levels) > 1 else "",
            levels[2] if len(levels) > 2 else "",
            levels[3] if len(levels) > 3 else "",
            levels[4] if len(levels) > 4 else "",
            levels[5] if len(levels) > 5 else "",
            filename,
            extension,
            blob.size,
            round(blob.size / 1024 / 1024, 2)
        ])

        count += 1

print()
print("DONE")
print("Blobs:", count)
print("Inventory:", output)
