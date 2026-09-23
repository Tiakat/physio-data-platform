import os
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
    print(f"\n{project}")
    for b in cc.list_blobs(name_starts_with=f"{project}/Database/"):
        print(" ", b.name)
