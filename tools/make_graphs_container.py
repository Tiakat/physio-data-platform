"""Create the graphs container in Azure (K's request)."""
import os, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    try:
        svc.create_container("graphs")
        print("[make-graphs-container] created 'graphs' container", flush=True)
    except Exception as e:
        if "ContainerAlreadyExists" in str(e):
            print("[make-graphs-container] 'graphs' already exists", flush=True)
        else:
            raise

if __name__ == "__main__":
    main()
