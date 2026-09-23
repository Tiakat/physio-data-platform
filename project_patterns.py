import os
from collections import Counter
from azure.storage.blob import BlobServiceClient

account = os.environ["AZURE_STORAGE_ACCOUNT"]
key = os.environ["AZURE_STORAGE_KEY"]

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key
)

cc = client.get_container_client("rawdata")

projects = [
    "DEXREM","ESMONOL","IPAMS","MONREPI",
    "POSBRAIN","PROMISES","SILVR","V-RAPS"
]

for project in projects:
    counter = Counter()

    for b in cc.list_blobs(name_starts_with=f"{project}/"):
        p = b.name[len(project)+1:]

        # Ignore the new template
        if p.startswith("Database/"):
            continue

        parts = p.split("/")

        if len(parts) >= 2:
            pattern = "/".join(parts[:2])
            counter[pattern] += 1

    print("\n" + "="*60)
    print(project)
    print("="*60)

    for pattern, count in counter.most_common():
        print(f"{count:5}  {pattern}")
