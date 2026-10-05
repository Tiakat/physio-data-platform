"""Count graphs per project in processed/level2."""
import os, sys
from pathlib import Path
from collections import Counter
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    proc = svc.get_container_client("processed")
    graphs_c = svc.get_container_client("graphs")

    # Count in processed/level2
    proc_counts = Counter()
    for b in proc.list_blobs(name_starts_with="processed/level2/"):
        name = b["name"] if isinstance(b, dict) else b.name
        if "/graphs/" in name and name.endswith(".png"):
            parts = name.split("/")
            if len(parts) >= 3:
                proc_counts[parts[2]] += 1

    # Count in graphs container
    graphs_counts = Counter()
    try:
        for b in graphs_c.list_blobs():
            name = b["name"] if isinstance(b, dict) else b.name
            if name.endswith(".png"):
                parts = name.split("/")
                if len(parts) >= 1:
                    graphs_counts[parts[0]] += 1
    except Exception as e:
        print(f"Graphs container error: {e}")

    print("\n=== Graphs in processed/level2 (source) ===")
    for proj in sorted(proc_counts):
        print(f"  {proj}: {proc_counts[proj]:,}")

    print("\n=== Graphs in graphs container (migrated) ===")
    for proj in sorted(graphs_counts):
        print(f"  {proj}: {graphs_counts[proj]:,}")

    print(f"\nTotal in processed: {sum(proc_counts.values()):,}")
    print(f"Total in graphs container: {sum(graphs_counts.values()):,}")

if __name__ == "__main__":
    main()
