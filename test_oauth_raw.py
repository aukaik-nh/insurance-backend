"""ทดสอบ OAuth refresh ผ่าน HTTP request ตรงๆ — bypass google-auth library"""
import os, sys, json, requests
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()

client_id     = os.getenv("GOOGLE_CLIENT_ID")
client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")

print(f"CLIENT_ID ends with: ...{client_id[-30:]}")
print(f"CLIENT_SECRET ends:   ...{client_secret[-10:]}")
print(f"REFRESH_TOKEN len:    {len(refresh_token)}")
print(f"REFRESH_TOKEN start:  {refresh_token[:8]}...")
print(f"REFRESH_TOKEN end:    ...{refresh_token[-10:]}")
print()

# Refresh access token via raw HTTP
resp = requests.post(
    "https://oauth2.googleapis.com/token",
    data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
        "client_secret": client_secret,
    },
    timeout=30,
)
print(f"HTTP {resp.status_code}")
print(f"Response: {resp.text[:500]}")

if resp.status_code == 200:
    access_token = resp.json()["access_token"]
    print(f"\n✓ Got access token: {access_token[:30]}...")
    # Test Drive API
    r2 = requests.get(
        f"https://www.googleapis.com/drive/v3/files",
        params={"q": f"'{os.getenv('GOOGLE_DRIVE_FOLDER_ID')}' in parents", "pageSize": 3},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    print(f"Drive files API: HTTP {r2.status_code}")
    if r2.ok:
        for f in r2.json().get("files", [])[:3]:
            print(f"  {f['name']}")
