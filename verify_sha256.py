from dotenv import load_dotenv
import os
import hashlib
from azure.storage.blob import BlobServiceClient

load_dotenv()

account = os.getenv("AZURE_STORAGE_ACCOUNT")
key = os.getenv("AZURE_STORAGE_KEY")

local_file = r"C:\Users\katia\Dropbox\Liam\Projects actifs\V-RAPS\Database\RawData\19\ExtractedData\NOL\PMD_LOG.csv"
blob_name = "V-RAPS/19/ExtractedData/NOL/PMD_LOG.csv"

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(8 * 1024 * 1024)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

print("Calculating Dropbox SHA-256...")
local_hash = sha256_file(local_file)

client = BlobServiceClient(
    account_url=f"https://{account}.blob.core.windows.net",
    credential=key,
)

blob = client.get_blob_client(
    container="rawdata",
    blob=blob_name,
)

print("Downloading Azure copy temporarily...")
data = blob.download_blob().readall()

azure_hash = hashlib.sha256(data).hexdigest()

print()
print("========================================")
print("SHA-256 VERIFICATION")
print("========================================")
print("File:", blob_name)
print()
print("Dropbox SHA-256:")
print(local_hash)
print()
print("Azure SHA-256:")
print(azure_hash)
print()
print("MATCH:", local_hash == azure_hash)
print()
print("Dropbox size:", os.path.getsize(local_file), "bytes")
print("Azure size:", len(data), "bytes")
print("========================================")

if local_hash == azure_hash:
    print("VERIFICATION PASSED")
    print("The Azure copy is byte-for-byte identical.")
else:
    print("VERIFICATION FAILED")
    print("The Azure copy differs from the Dropbox file.")
