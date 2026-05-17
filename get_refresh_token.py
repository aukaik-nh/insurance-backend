"""
One-time script to get a Google OAuth refresh token for Drive access.
Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET env vars before running.
"""
import os
from dotenv import load_dotenv
load_dotenv()

from google_auth_oauthlib.flow import InstalledAppFlow

client_id     = os.environ["GOOGLE_CLIENT_ID"]
client_secret = os.environ["GOOGLE_CLIENT_SECRET"]

CLIENT_CONFIG = {
    "web": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost:8080/"],
    }
}

SCOPES = ["https://www.googleapis.com/auth/drive"]

# fixed port 8080 — ต้องเพิ่ม http://localhost:8080/ ใน OAuth client redirect URIs
flow = InstalledAppFlow.from_client_config(CLIENT_CONFIG, SCOPES)
creds = flow.run_local_server(port=8080, prompt="consent")

print("SUCCESS!")
print("GOOGLE_CLIENT_ID=" + client_id)
print("GOOGLE_REFRESH_TOKEN=" + str(creds.refresh_token))
