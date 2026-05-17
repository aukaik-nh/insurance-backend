"""
รัน script นี้ทุกวัน — อัปโหลด PDF ไป Drive + อ่าน policy number + match DB
progress เก็บใน batch_progress.json (ไม่ซ้ำไฟล์ที่ทำแล้ว)
"""
import os, io, json, base64, time
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

PDF_FOLDER   = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"
PROGRESS_FILE = "batch_progress.json"
BATCH_SIZE    = int(os.getenv("BATCH_SIZE", "1000"))  # ไฟล์ต่อรอบ (ปลอดภัยภายใต้ 1,500/วัน)

# ─── Google Drive (OAuth user delegation) ─────────────────────────────────────
# ใช้ OAuth ไม่ใช่ Service Account เพราะ SA ไม่มี storage quota ใน personal Drive
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive():
    refresh_token = os.getenv("GOOGLE_REFRESH_TOKEN")
    client_id     = os.getenv("GOOGLE_CLIENT_ID")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET")
    if not all([refresh_token, client_id, client_secret]):
        raise RuntimeError("ต้องมี GOOGLE_REFRESH_TOKEN, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET ใน .env")
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def upload_to_drive(service, file_bytes, filename, folder_id):
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/pdf")
    meta  = {"name": filename, "parents": [folder_id]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    fid = f.get("id")
    service.permissions().create(fileId=fid, body={"type":"anyone","role":"reader"}).execute()
    return f"https://drive.google.com/file/d/{fid}/view"

# ─── Gemini ───────────────────────────────────────────────────────────────────
from google import genai
from google.genai import types

_PROMPT = """อ่านกรมธรรม์นี้และตอบเฉพาะ JSON:
{"policy_number": "เลขกรมธรรม์", "insured_name": "ชื่อผู้เอาประกัน", "license_plate": "ทะเบียนรถ"}
ถ้าไม่พบให้ใส่ null ตอบเป็น JSON เท่านั้น"""

def extract_policy_info(file_bytes):
    api_key = os.getenv("GEMINI_API_KEY")
    client  = genai.Client(api_key=api_key)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_text(text=_PROMPT),
            types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"),
        ],
    )
    import re
    text = resp.text
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        return json.loads(m.group())
    return {}

# ─── Supabase ─────────────────────────────────────────────────────────────────
from supabase import create_client

def get_sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def find_record(sb, info):
    pn = info.get("policy_number")
    if pn:
        r = sb.table("insurance_policies").select("id").eq("policy_number", pn).execute()
        if r.data:
            return r.data[0]["id"]
    lp = info.get("license_plate")
    if lp:
        r = sb.table("insurance_policies").select("id").eq("license_plate", lp).execute()
        if r.data:
            return r.data[0]["id"]
    return None

def update_pdf_url(sb, record_id, pdf_url, filename):
    sb.table("insurance_policies").update({
        "pdf_url": pdf_url,
        "pdf_filename": filename,
    }).eq("id", record_id).execute()

# ─── Main ─────────────────────────────────────────────────────────────────────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "matched": 0, "uploaded": 0, "no_match": []}

def save_progress(p):
    with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)

def main():
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    all_files = [f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")]
    progress  = load_progress()
    done_set  = set(progress["done"])

    pending = [f for f in all_files if f not in done_set]
    batch   = pending[:BATCH_SIZE]

    print(f"ไฟล์ทั้งหมด: {len(all_files)} | ทำแล้ว: {len(done_set)} | เหลือ: {len(pending)} | รอบนี้: {len(batch)}")

    drive = get_drive()
    sb    = get_sb()

    for i, filename in enumerate(batch, 1):
        filepath = os.path.join(PDF_FOLDER, filename)
        try:
            file_bytes = Path(filepath).read_bytes()

            # upload Drive
            pdf_url = upload_to_drive(drive, file_bytes, filename, folder_id)
            progress["uploaded"] += 1

            # อ่าน policy number
            info = extract_policy_info(file_bytes)

            # match DB
            rid = find_record(sb, info)
            if rid:
                update_pdf_url(sb, rid, pdf_url, filename)
                progress["matched"] += 1
                status = f"MATCHED {info.get('policy_number','')}"
            else:
                progress["no_match"].append({"file": filename, "url": pdf_url, "info": info})
                status = f"no match ({info.get('policy_number','?')})"

            progress["done"].append(filename)
            save_progress(progress)

            print(f"[{i}/{len(batch)}] {filename[:40]} → {status}")
            time.sleep(0.5)  # หน่วงเล็กน้อยป้องกัน rate limit

        except Exception as e:
            print(f"[{i}/{len(batch)}] ERROR {filename[:40]}: {e}")
            time.sleep(2)

    print(f"\nเสร็จรอบนี้! อัปโหลด: {progress['uploaded']} | match: {progress['matched']}")
    print(f"เหลืออีก: {len(pending) - len(batch)} ไฟล์ — รันใหม่พรุ่งนี้")

if __name__ == "__main__":
    main()
