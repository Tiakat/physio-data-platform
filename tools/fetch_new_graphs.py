"""Fetch sample graphs from the graphs container for K's review."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("graphs")

    # Get 6 sample graphs from COLECTOMIE (different patients/signals)
    samples = []
    seen_patients = set()
    for b in container.list_blobs(name_starts_with="COLECTOMIE/"):
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith(".png"):
            parts = name.split("/")
            if len(parts) >= 3:
                patient = parts[1]
                if patient not in seen_patients and len(seen_patients) < 3:
                    seen_patients.add(patient)
                if patient in seen_patients and len([s for s in samples if patient in s]) < 2:
                    samples.append(name)
        if len(samples) >= 6:
            break

    print(f"[fetch-new-graphs] Found {len(samples)} samples", flush=True)
    outdir = Path("new_graph_samples")
    outdir.mkdir(exist_ok=True)
    for s in samples:
        data = container.get_blob_client(s).download_blob().readall()
        fname = s.replace("/", "_")
        (outdir / fname).write_bytes(data)
        print(f"  {fname} ({len(data)} bytes)", flush=True)
    print(f"[fetch-new-graphs] Saved to {outdir}/", flush=True)

if __name__ == "__main__":
    main()
