import os
from azure.storage.blob import BlobServiceClient

account = os.environ["AZURE_STORAGE_ACCOUNT"]
key = os.environ["AZURE_STORAGE_KEY"]

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key
)

cc = client.get_container_client("rawdata")

projects = ["DEXREM","ESMONOL","IPAMS","MONREPI","POSBRAIN","PROMISES","SILVR","V-RAPS"]

for project in projects:
    print(f"\n{'='*70}")
    print(project)
    print(f"{'='*70}")

    normal = {}
    rawdata = {}

    for b in cc.list_blobs(name_starts_with=project + "/"):
        p = b.name

        if p.startswith(project + "/RawData/"):
            relative = p[len(project + "/RawData/"):]
            rawdata[relative] = b.size

        elif p.startswith(project + "/"):
            relative = p[len(project + "/"):]
            normal[relative] = b.size

    common = set(normal) & set(rawdata)

    print("Normal files :", len(normal))
    print("RawData files:", len(rawdata))
    print("Duplicates   :", len(common))

    if common:
        print("\nEXAMPLES:")
        for x in sorted(common)[:15]:
            print(f"  {x}")
