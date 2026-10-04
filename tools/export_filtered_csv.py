"""Export filtered parquets as CSVs to a dedicated container (K's request).

Reads encrypted filtered parquets from processed/, decrypts, converts to CSV,
uploads to 'filtered-csv' container in project/patient structure.
"""
import os, sys, io
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth
import pandas as pd

def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet
    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    # Create container if needed
    try:
        svc.create_container("filtered-csv")
        print("[export-csv] created 'filtered-csv' container", flush=True)
    except Exception as e:
        if "ContainerAlreadyExists" not in str(e):
            raise
        print("[export-csv] 'filtered-csv' already exists", flush=True)

    proc = svc.get_container_client("processed")
    csvc = svc.get_container_client("filtered-csv")

    # Find all filtered parquets
    blobs = []
    for b in proc.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith("/filtered.parquet.enc"):
            blobs.append(name)

    print(f"[export-csv] Found {len(blobs)} filtered parquets", flush=True)

    done, skipped = 0, 0
    for blob_name in blobs:
        parts = blob_name.split("/")
        # processed/level2/{PROJECT}/{patient}/filtered.parquet.enc
        if len(parts) < 5:
            continue
        proj, patient = parts[2], parts[3]
        csv_blob = f"{proj}/{patient}/filtered.csv"

        # Skip if already exists
        try:
            csvc.get_blob_client(csv_blob).get_blob_properties()
            skipped += 1
            continue
        except Exception:
            pass

        # Download, decrypt, convert
        raw = proc.get_blob_client(blob_name).download_blob().readall()
        df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))

        # Convert to CSV (in memory)
        csv_data = df.to_csv(index=False).encode('utf-8')
        csvc.get_blob_client(csv_blob).upload_blob(csv_data, overwrite=True)
        done += 1
        if done % 50 == 0:
            print(f"[export-csv] {done} converted...", flush=True)

    print(f"[export-csv] Done: {done} new, {skipped} already existed", flush=True)

if __name__ == "__main__":
    main()
