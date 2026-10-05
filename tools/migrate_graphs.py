"""Migrate graphs from processed/level2 to the graphs container, one project at a time.

Usage: python -m tools.migrate_graphs --project COLECTOMIE
"""
import os, sys, argparse
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True, help="Project code (e.g., COLECTOMIE)")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")
    graphs = svc.get_container_client("graphs")

    # Find all graphs under processed/level2/{PROJECT}/*/graphs/
    prefix = f"processed/level2/{args.project}/"
    blobs = []
    for b in proc.list_blobs(name_starts_with=prefix):
        name = b["name"] if isinstance(b, dict) else b.name
        if "/graphs/" in name and name.endswith(".png"):
            blobs.append(name)

    print(f"[migrate-graphs] Found {len(blobs)} graphs for {args.project}", flush=True)

    done, skipped = 0, 0
    for blob_name in blobs:
        # processed/level2/{PROJECT}/{patient}/graphs/{file}.png
        # -> graphs/{PROJECT}/{patient}/{file}.png
        parts = blob_name.split("/")
        # parts[0]=processed, parts[1]=level2, parts[2]=PROJECT, parts[3]=patient, parts[4]=graphs, parts[5]=file
        if len(parts) < 6:
            continue
        proj, patient, fname = parts[2], parts[3], parts[5]
        dest = f"{proj}/{patient}/{fname}"

        # Skip if already exists
        try:
            graphs.get_blob_client(dest).get_blob_properties()
            skipped += 1
            continue
        except Exception:
            pass

        data = proc.get_blob_client(blob_name).download_blob().readall()
        graphs.get_blob_client(dest).upload_blob(
            data, overwrite=True,
            content_settings={"content_type": "image/png"})
        done += 1
        if done % 100 == 0:
            print(f"[migrate-graphs] {done} migrated...", flush=True)

    print(f"[migrate-graphs] {args.project}: {done} new, {skipped} already existed", flush=True)

if __name__ == "__main__":
    main()
