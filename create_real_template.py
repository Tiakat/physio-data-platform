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
    "Database/RawData",
    "Database/ExtractedData",
    "Database/ProcessedData",
    "Database/QC",
    "Database/Metadata",
    "Database/Documentation"
]

for project in projects:
    for folder in folders:
        path = f"{project}/{folder}/README.txt"

        content = (
            f"{project}\n"
            f"{folder}\n\n"
            "Standard database directory.\n"
        )

        cc.upload_blob(
            name=path,
            data=content.encode("utf-8"),
            overwrite=True
        )

print("REAL DIRECTORY MARKERS CREATED.")
