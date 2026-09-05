from dotenv import load_dotenv
import os
import hashlib
from azure.storage.blob import BlobServiceClient

load_dotenv()

account = os.getenv("AZURE_STORAGE_ACCOUNT")
key = os.getenv("AZURE_STORAGE_KEY")

local_file = r"C:\Users\katia\Dropbox\Liam\Projects actifs\V-RAPS\Database\RawData\19\ExtractedData\NOL\PMD_LOG.csv"
blob_name = "V-RAPS/19/ExtractedData/NOL/PMD_LOG.csv"

sha256 = hashlib.sha256()
size = 0

with open(local_file, "rb") as f:
    while True:
        chunk = f.read(8 * 1024 * 1024)
        if not chunk:
            break
        sha256.update(chunk)
        size += len(chunk)

local_hash = sha256.hexdigest()

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key,
)

blob = client.get_blob_client(
    container="rawdata",
    blob=blob_name,
)

properties = blob.get_blob_properties()

print("Azure blob:", blob_name)
print("Local size:", size)
print("Azure size:", properties.size)
print("Size match:", size == properties.size)
print("Local SHA-256:", local_hash)
print("Azure blob exists: YES")
