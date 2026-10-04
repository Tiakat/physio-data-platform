"""Find PROMISES labels in Dropbox: search for label files and inspect zips."""
import os, sys, json, zipfile, io

import requests

APP_KEY = os.environ["DROPBOX_APP_KEY"]
APP_SECRET = os.environ["DROPBOX_APP_SECRET"]
REFRESH = os.environ["DROPBOX_REFRESH_TOKEN"]

# Get access token
r = requests.post("https://api.dropboxapi.com/oauth2/token", data={
    "grant_type": "refresh_token",
    "refresh_token": REFRESH,
    "client_id": APP_KEY,
    "client_secret": APP_SECRET,
}, timeout=30)
r.raise_for_status()
token = r.json()["access_token"]
H = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def api(endpoint, payload):
    r = requests.post(f"https://api.dropboxapi.com/2/{endpoint}",
                      headers=H, json=payload, timeout=60)
    r.raise_for_status()
    return r.json()

def download(path):
    r = requests.post("https://content.dropboxapi.com/2/files/download",
                      headers={**H, "Dropbox-API-Arg": json.dumps({"path": path})},
                      timeout=120)
    r.raise_for_status()
    return r.content

print("=== Searching Dropbox for label files ===", flush=True)
results = []
# Search for "label" in filenames
res = api("files/search_v2", {"query": "label", "options": {"filename_only": True}})
for m in res.get("matches", []):
    md = m["metadata"]["metadata"]
    results.append((md["path_lower"], md.get("size", 0)))
    print(f"  {md['path_lower']} ({md.get('size',0)} bytes)", flush=True)

# Also search for the promises zip / article
for q in ["promises", "PROMISES"]:
    res = api("files/search_v2", {"query": q, "options": {"filename_only": True}})
    for m in res.get("matches", [])[:20]:
        md = m["metadata"]["metadata"]
        if md["name"].lower().endswith(".zip"):
            print(f"  ZIP: {md['path_lower']} ({md.get('size',0)} bytes)", flush=True)
            results.append((md["path_lower"], md.get("size", 0)))

print(f"\n=== Found {len(results)} candidates ===", flush=True)

# Download and inspect each candidate (limit to reasonable sizes)
for path, size in results:
    if size > 500 * 1024 * 1024:
        print(f"SKIP {path}: too large ({size} bytes)", flush=True)
        continue
    try:
        print(f"\n--- Inspecting {path} ---", flush=True)
        data = download(path)
        if path.endswith(".zip"):
            zf = zipfile.ZipFile(io.BytesIO(data))
            names = zf.namelist()
            print(f"  zip contains {len(names)} files", flush=True)
            for n in names[:30]:
                print(f"    {n}", flush=True)
            # Look for label-like files inside
            for n in names:
                nl = n.lower()
                if "label" in nl and (nl.endswith(".csv") or nl.endswith(".xlsx") or nl.endswith(".txt")):
                    print(f"  LABEL CANDIDATE INSIDE ZIP: {n}", flush=True)
                    # Save it for inspection
                    out = f"/tmp/labels/extracted_{os.path.basename(n)}"
                    with open(out, "wb") as f:
                        f.write(zf.read(n))
                    print(f"  saved to {out}", flush=True)
        elif path.endswith(".csv"):
            text = data.decode("utf-8", errors="replace")
            lines = text.split("\n")
            print(f"  csv: {len(lines)} lines", flush=True)
            for l in lines[:5]:
                print(f"    {l[:150]}", flush=True)
        else:
            print(f"  {len(data)} bytes, not a zip/csv", flush=True)
    except Exception as e:
        print(f"  ERROR: {e}", flush=True)

print("\n=== Done ===", flush=True)
