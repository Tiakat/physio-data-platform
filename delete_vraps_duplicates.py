import os
from azure.storage.blob import BlobServiceClient

account = os.environ["AZURE_STORAGE_ACCOUNT"]
key = os.environ["AZURE_STORAGE_KEY"]

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key
)

cc = client.get_container_client("rawdata")

normal = set()
rawdata = {}

for b in cc.list_blobs(name_starts_with="V-RAPS/"):
    p = b.name

    if p.startswith("V-RAPS/RawData/"):
        rel = p[len("V-RAPS/RawData/"):]
        rawdata[rel] = b.size
    else:
        rel = p[len("V-RAPS/"):]
        normal.add(rel)

duplicates = sorted(normal & set(rawdata))

print(f"Duplicate RawData files to delete: {len(duplicates)}")

for rel in duplicates:
    blob_name = "V-RAPS/RawData/" + rel
    print("DELETE:", blob_name)
    cc.delete_blob(blob_name)

print(f"\nDeleted: {len(duplicates)}")
