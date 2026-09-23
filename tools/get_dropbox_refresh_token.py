import os
import dropbox
from dotenv import load_dotenv

load_dotenv(r"C:\Users\katia\Desktop\lab\labdata\.env")

app_key = os.getenv("DROPBOX_APP_KEY")
app_secret = os.getenv("DROPBOX_APP_SECRET")

if not app_key:
    raise RuntimeError("DROPBOX_APP_KEY is missing from .env")

if not app_secret:
    raise RuntimeError("DROPBOX_APP_SECRET is missing from .env")

flow = dropbox.DropboxOAuth2FlowNoRedirect(
    app_key,
    app_secret,
    token_access_type="offline",
)

authorize_url = flow.start()

print()
print("=" * 70)
print("DROPBOX AUTHORIZATION")
print("=" * 70)
print()
print("Open this URL in your browser:")
print()
print(authorize_url)
print()
print("After authorizing the app, Dropbox will give you a code.")
print("Paste that code below.")
print()

auth_code = input("Authorization code: ").strip()

oauth_result = flow.finish(auth_code)

print()
print("=" * 70)
print("SUCCESS")
print("=" * 70)
print()
print("Refresh token:")
print(oauth_result.refresh_token)
print()
print("Do NOT share this token in chat.")