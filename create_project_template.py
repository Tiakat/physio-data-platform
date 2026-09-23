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
    "DEXREM",
    "ESMONOL",
    "IPAMS",
    "MONREPI",
    "POSBRAIN",
    "PROMISES",
    "SILVR",
    "V-RAPS"
]

folders = [
    "Database/RawData/",
    "Database/ExtractedData/",
    "Database/ProcessedData/",
    "Database/Metadata/",
    "Database/Documentation/"
]

print("CREATING STANDARD PROJECT TEMPLATE")
print("===================================")

for project in projects:
    print(f"\n{project}")

    for folder in folders:
        blob_name = f"{project}/{folder}.keep"

        cc.upload_blob(
            name=blob_name,
            data=b"",
            overwrite=True
        )

        print(f"  + {folder}")

print("\nDONE")
