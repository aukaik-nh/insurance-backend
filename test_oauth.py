"""Test OAuth Drive connection only"""
import os, sys
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()

print(f"CLIENT_ID:     {os.getenv('GOOGLE_CLIENT_ID', 'NOT SET')[:30]}...")
print(f"CLIENT_SECRET: {os.getenv('GOOGLE_CLIENT_SECRET', 'NOT SET')[:15]}...")
print(f"REFRESH_TOKEN: {os.getenv('GOOGLE_REFRESH_TOKEN', 'NOT SET')[:30]}...")
print(f"FOLDER_ID:     {os.getenv('GOOGLE_DRIVE_FOLDER_ID', 'NOT SET')}")

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials

creds = Credentials(
    token=None,
    refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
    client_id=os.getenv("GOOGLE_CLIENT_ID"),
    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
    token_uri="https://oauth2.googleapis.com/token",
    scopes=["https://www.googleapis.com/auth/drive"],
)
print("\nTrying to refresh token...")
from google.auth.transport.requests import Request
try:
    creds.refresh(Request())
    print(f"OK! token: {creds.token[:30]}...")
    print(f"expires: {creds.expiry}")
except Exception as e:
    print(f"FAIL: {e}")
    sys.exit(1)

print("\nTrying to list files in folder...")
svc = build("drive", "v3", credentials=creds, cache_discovery=False)
r = svc.files().list(
    q=f"'{os.getenv('GOOGLE_DRIVE_FOLDER_ID')}' in parents",
    fields="files(id,name,size)",
    pageSize=5,
).execute()
print(f"Files in folder: {len(r.get('files', []))}")
for f in r.get("files", []):
    print(f"  {f['name']}  ({f.get('size','?')} bytes)")
