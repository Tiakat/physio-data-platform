"""Fetch sample graphs from Azure for K to review."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("processed")
    
    # Get sample graphs: 2 patients from different projects
    # Look for graphs in processed/level2/*/*/graphs/
    samples = []
    seen_projects = set()
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if "/graphs/" in name and name.endswith(".png"):
            parts = name.split("/")
            # processed/level2/{PROJECT}/{patient}/graphs/{file}.png
            if len(parts) >= 5:
                proj = parts[2]
                if proj not in seen_projects and len(seen_projects) < 3:
                    seen_projects.add(proj)
                    samples.append(name)
                elif proj in seen_projects and len([s for s in samples if proj in s]) < 4:
                    samples.append(name)
        if len(samples) >= 12:
            break
    
    print(f"Found {len(samples)} sample graphs", flush=True)
    outdir = Path("graph_samples")
    outdir.mkdir(exist_ok=True)
    for s in samples:
        blob = container.get_blob_client(s)
        data = blob.download_blob().readall()
        # Save with project prefix
        fname = "_".join(s.split("/")[2:4]) + "_" + s.split("/")[-1]
        (outdir / fname).write_bytes(data)
        print(f"  {fname}", flush=True)
    print(f"Saved to {outdir}/", flush=True)

if __name__ == "__main__":
    main()
