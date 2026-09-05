import os

import dropbox
from dotenv import load_dotenv

load_dotenv()

token = os.getenv("DROPBOX_ACCESS_TOKEN")

if not token:
    raise RuntimeError("DROPBOX_ACCESS_TOKEN is missing from .env")

dbx = dropbox.Dropbox(token)

account = dbx.users_get_current_account()

print("Dropbox authentication successful.")
print(f"Connected account: {account.name.display_name}")

paths = [
    "/Liam/Projects actifs/V-RAPS/Database/RawData/1/BIS",
    "/Liam/Projects actifs/V-RAPS/Database/RawData/RawData/1/BIS",
]

for path in paths:
    print("\n" + "=" * 70)
    print(f"Inspecting: {path}")
    print("=" * 70)

    try:
        result = dbx.files_list_folder(path)

        if not result.entries:
            print("(empty)")

        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                print(
                    f"- FILE: {entry.name} | "
                    f"size={entry.size} | "
                    f"modified={entry.server_modified}"
                )
            else:
                print(f"- {entry.name} [{entry.__class__.__name__}]")

    except Exception as exc:
        print(f"ERROR: {exc}")