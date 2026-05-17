"""
match_and_upload.py
─────────────────────────────────────────────────────────────────────
อัปโหลด PDF จาก BabyPreechar → Google Drive แล้ว match กับ
insurance_policies ใน Supabase โดยใช้ filename matching:

  ประเภท 1: ทะเบียนรถ  →  match license_plate
  ประเภท 2: ที่อยู่     →  match insured_address
  ประเภท 3: ชื่อ        →  match insured_name

ถ้ามีปีในชื่อไฟล์ จะกรองโดยให้ coverage_start year อยู่ในช่วง year±1

USAGE:
    # dry-run 100 ไฟล์ (ไม่อัปโหลด ไม่อัปเดต DB)
    python match_and_upload.py --dry-run --limit 100

    # อัปโหลดจริง batch ละ 500 ไฟล์
    python match_and_upload.py --limit 500

    # อัปโหลดทั้งหมด
    python match_and_upload.py
"""
import os, io, json, time, re, sys, argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()

# ─── encoding for Thai output ──────────────────────────────────────
if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PDF_FOLDER    = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"
PROGRESS_FILE = str(Path(__file__).parent / "match_progress.json")

# ─── Google Drive (OAuth) ──────────────────────────────────────────
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
from google.oauth2.credentials import Credentials

SCOPES = ["https://www.googleapis.com/auth/drive"]

def get_drive():
    creds = Credentials(
        token=None,
        refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
        client_id=os.getenv("GOOGLE_CLIENT_ID"),
        client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)

def upload_to_drive(service, file_bytes, filename, folder_id):
    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype="application/pdf")
    meta  = {"name": filename, "parents": [folder_id]}
    f = service.files().create(body=meta, media_body=media, fields="id").execute()
    fid = f.get("id")
    service.permissions().create(
        fileId=fid, body={"type": "anyone", "role": "reader"}
    ).execute()
    return f"https://drive.google.com/file/d/{fid}/view"

# ─── Supabase ──────────────────────────────────────────────────────
from supabase import create_client

def get_sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

def load_all_policies(sb):
    """โหลด policies ทั้งหมดเข้า memory ครั้งเดียว (10k records ไหว)"""
    rows = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, license_plate, insured_address, insured_name, coverage_start, pdf_url"
        ).range(page * 1000, page * 1000 + 999).execute()
        if not r.data:
            break
        rows.extend(r.data)
        if len(r.data) < 1000:
            break
        page += 1
    return rows

# ─── Filename Parser ───────────────────────────────────────────────
THAI_CONSONANTS = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"

# ทะเบียนรถ format:
#   - 2-3 ตัวอักษรไทย + 1-4 ตัวเลข เช่น ฆก6755, กต8256
#   - หรือ 1 ตัวเลข + 1-2 ตัวอักษรไทย + 1-4 ตัวเลข เช่น 1ฒน1479, 1ค5163
#   - หรือ 6-7 ตัวเลขล้วน (ทะเบียนเก่า) เช่น 816814
# จับเฉพาะกรณีที่ตามด้วย space / doc-type / .pdf / end เพื่อไม่จับชื่อ
PLATE_PATTERNS = [
    rf"^(\d?[{THAI_CONSONANTS}]{{1,2}}\s?\d{{1,4}})(?=\s|พรบ|กธ|PA|สลักหลัง|ยกเลิก|\.pdf)",
    rf"^(\d{{6,7}})(?=\s|พรบ|กธ|\.pdf)",
]

# ปี: หลัง "กธ." หรือ "พรบ." หรือ "PA." หรือลำพัง 2 digits ก่อน .pdf
YEAR_PATTERN = r"(?:กธ|พรบ|PA|สลักหลัง|ยกเลิก)\s*\.?\s*(\d{2})|(?<!\d)(\d{2})\s*\.\s*pdf"

def parse_filename(filename: str) -> dict:
    """แยก filename → {kind: 'plate'|'address'|'name', key: str, year_be: int|None, raw: str}"""
    stem = Path(filename).stem  # ตัด .pdf
    cleaned = re.sub(r"_\d{4}$", "", stem)                    # _0001
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", cleaned).strip()  # (แบบบ), (สลักหลัง58), (3ปี)

    # year (อ่านจาก stem เดิมเพราะอาจมี .pdf ใน text)
    year_be = None
    m = re.search(YEAR_PATTERN, filename)
    if m:
        y = m.group(1) or m.group(2)
        if y:
            year_be = 2500 + int(y)

    # ทะเบียนรถ
    for pat in PLATE_PATTERNS:
        m = re.match(pat, cleaned)
        if m:
            return {"kind": "plate", "key": m.group(1).replace(" ", ""),
                    "year_be": year_be, "raw": filename}

    # ที่อยู่ — ขึ้นต้นด้วยเลข + ตัวอักษรไทย → จับ "เลข + คำต่อ (ซ./ถ./หมู่/ฯ)"
    # ใช้ key ยาว: number + suffix text ก่อน space ตัวแรก
    m = re.match(rf"^(\d+(?:[-/]\d+)?(?:[-/]\d+)?)\s*([{THAI_CONSONANTS}][^\s]*?)(?:\s|$)", cleaned)
    if m:
        number_part = m.group(1)
        suffix_part = m.group(2)[:20]  # cap length
        full_key = f"{number_part} {suffix_part}".strip()
        return {"kind": "address", "key": full_key, "number": number_part,
                "suffix": suffix_part, "year_be": year_be, "raw": filename}

    # ชื่อ — ทุกอย่างก่อน " กธ" หรือ " พรบ" หรือ " PA"
    m = re.match(rf"^([{THAI_CONSONANTS}].+?)\s*(?:กธ|พรบ|PA|\d{{2}}\.|\.pdf|$)", cleaned)
    if m:
        name = m.group(1).strip()
        if len(name) >= 3:
            return {"kind": "name", "key": name, "year_be": year_be, "raw": filename}

    return {"kind": "unknown", "key": cleaned, "year_be": year_be, "raw": filename}

# ─── Matching ──────────────────────────────────────────────────────
def normalize(s: str) -> str:
    """ลบ space, ตัวอักษรพิเศษ"""
    if not s:
        return ""
    return re.sub(r"\s+", "", s).lower()

def year_matches(coverage_start: str, year_be: int) -> bool:
    """coverage_start (YYYY-MM-DD) ตรงปี BE ±1 ปีไหม"""
    if not coverage_start or not year_be:
        return True  # ไม่มีปี = match ทุก record (ใน address/name match)
    try:
        cov_year = int(coverage_start[:4])
        target_ad = year_be - 543
        return target_ad - 1 <= cov_year <= target_ad + 1
    except (ValueError, TypeError):
        return False

def find_matches(parsed: dict, policies: list) -> list:
    """หา records ที่ match — คืน list ของ ids"""
    kind = parsed["kind"]
    key  = parsed["key"]
    year = parsed["year_be"]
    if not key or kind == "unknown":
        return []

    key_norm = normalize(key)
    matches = []

    for p in policies:
        if kind == "plate":
            plate = normalize(p.get("license_plate") or "")
            # ทะเบียน OTHER, NULL → ข้าม
            if plate in ("other", "null", ""):
                continue
            if key_norm in plate or plate in key_norm:
                if year_matches(p.get("coverage_start"), year):
                    matches.append(p["id"])

        elif kind == "address":
            addr = normalize(p.get("insured_address") or "")
            # address ต้อง match ทั้งเลข + suffix word
            number = parsed.get("number", "")
            suffix = parsed.get("suffix", "")
            num_variants = {
                normalize(number),
                normalize(number).replace("-", "/"),
                normalize(number).replace("/", "-"),
            }
            suf_norm = normalize(suffix)
            num_in = any(v in addr for v in num_variants if v)
            suf_in = (suf_norm[:5] in addr) if len(suf_norm) >= 3 else True
            if num_in and suf_in:
                if year_matches(p.get("coverage_start"), year):
                    matches.append(p["id"])

        elif kind == "name":
            name = normalize(p.get("insured_name") or "")
            if key_norm in name:
                if year_matches(p.get("coverage_start"), year):
                    matches.append(p["id"])

    return matches

# ─── Progress ──────────────────────────────────────────────────────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "uploaded": 0, "matched_files": 0, "matched_records": 0,
            "no_match": [], "errors": []}

def save_progress(p):
    # atomic write: เขียน .tmp แล้ว rename — กัน lock / partial write
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    # Windows: ต้อง remove ก่อน rename
    if os.path.exists(PROGRESS_FILE):
        try:
            os.remove(PROGRESS_FILE)
        except PermissionError:
            time.sleep(0.5)
            os.remove(PROGRESS_FILE)
    os.rename(tmp, PROGRESS_FILE)

# ─── Main ──────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="ไม่อัปโหลด, ไม่อัปเดต DB — แค่ทดสอบ matching")
    ap.add_argument("--limit",   type=int, default=0, help="จำกัดจำนวนไฟล์ (0 = ทั้งหมด)")
    ap.add_argument("--no-resume", action="store_true", help="เริ่มใหม่ ไม่ใช้ progress เดิม")
    ap.add_argument("--upload-all", action="store_true",
                    help="upload ทั้งหมดรวม unmatched (default = skip unmatched)")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  match_and_upload.py {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"{'='*65}\n")

    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    all_files = sorted([f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")])

    progress = {"done": [], "uploaded": 0, "matched_files": 0, "matched_records": 0,
                "no_match": [], "errors": []} if args.no_resume else load_progress()
    done_set = set(progress["done"])

    pending = [f for f in all_files if f not in done_set]
    if args.limit:
        pending = pending[:args.limit]

    print(f"ไฟล์ทั้งหมด : {len(all_files):,}")
    print(f"ทำแล้ว     : {len(done_set):,}")
    print(f"จะทำรอบนี้  : {len(pending):,}\n")

    if not pending:
        print("ไม่มีไฟล์รอทำ")
        return

    sb = get_sb()
    print("โหลด policies จาก Supabase...")
    policies = load_all_policies(sb)
    print(f"โหลด policies: {len(policies):,} records\n")

    drive = None if args.dry_run else get_drive()

    # สถิติ
    kind_count = {"plate": 0, "address": 0, "name": 0, "unknown": 0}
    match_count = {"plate": 0, "address": 0, "name": 0, "unknown": 0}

    for i, filename in enumerate(pending, 1):
        try:
            parsed = parse_filename(filename)
            kind_count[parsed["kind"]] += 1

            matched_ids = find_matches(parsed, policies)
            if matched_ids:
                match_count[parsed["kind"]] += 1

            short_name = filename[:40].ljust(40)
            year_str   = f"y{parsed['year_be']}" if parsed['year_be'] else "y? "
            mc         = len(matched_ids)
            status     = f"{parsed['kind']:7s} {year_str} {mc:>3} match"

            if args.dry_run:
                print(f"[{i}/{len(pending)}] {short_name} → {status}")
                if not matched_ids and parsed["kind"] != "unknown":
                    progress["no_match"].append({"file": filename, "kind": parsed["kind"], "key": parsed["key"]})
                progress["done"].append(filename)
                continue

            # ── ถ้า match ไม่ได้และไม่บังคับ upload-all → skip ──
            if not matched_ids and not args.upload_all:
                progress["no_match"].append({"file": filename, "kind": parsed["kind"], "key": parsed["key"]})
                progress["done"].append(filename)
                save_progress(progress)
                print(f"[{i}/{len(pending)}] {short_name} → {status} ⊘ skip")
                continue

            # ── อัปโหลดจริง ──
            filepath   = os.path.join(PDF_FOLDER, filename)
            file_bytes = Path(filepath).read_bytes()
            pdf_url    = upload_to_drive(drive, file_bytes, filename, folder_id)
            progress["uploaded"] += 1

            if matched_ids:
                # update ทุก record ที่ match
                sb.table("insurance_policies").update({
                    "pdf_url": pdf_url, "pdf_filename": filename
                }).in_("id", matched_ids).execute()
                progress["matched_files"] += 1
                progress["matched_records"] += len(matched_ids)

            progress["done"].append(filename)
            save_progress(progress)

            print(f"[{i}/{len(pending)}] {short_name} → {status} {'✓ uploaded' if matched_ids else '↑ uploaded (no match)'}")
            time.sleep(0.3)

        except Exception as e:
            err = f"{filename}: {str(e)[:200]}"
            progress["errors"].append(err)
            print(f"[{i}/{len(pending)}] ERROR {err}")
            save_progress(progress)
            time.sleep(1)

    if not args.dry_run:
        save_progress(progress)

    print(f"\n{'='*65}")
    print(f"  สรุปประเภทไฟล์")
    print(f"{'='*65}")
    for k, c in kind_count.items():
        mc = match_count[k]
        rate = (mc / c * 100) if c > 0 else 0
        print(f"  {k:10s}: {c:>5,} ไฟล์   match: {mc:>5,} ({rate:5.1f}%)")
    print(f"\n  รวม match files   : {sum(match_count.values()):,}/{sum(kind_count.values()):,}")
    if not args.dry_run:
        print(f"  รวม update records: {progress['matched_records']:,}")
        print(f"  upload สำเร็จ     : {progress['uploaded']:,}")
        print(f"  errors            : {len(progress['errors']):,}")

if __name__ == "__main__":
    main()
