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

folders = [
    "RawData",
    "ExtractedData",
    "ProcessedData",
    "QC",
    "Metadata",
    "Documentation"
]

for project in projects:
    for folder in folders:
        cc.upload_blob(
            name=f"{project}/Database/{folder}/.keep",
            data=b"",
            overwrite=True
        )

print("Standard template updated.")
