"""Cross-check: ทะเบียนที่ปรากฏในชื่อไฟล์ vs DB"""
import sys, os, re
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# โหลด plates ทั้งหมด
r = sb.table("insurance_policies").select("license_plate").execute()
db_plates = set()
for row in r.data:
    p = row.get("license_plate")
    if p and p not in ("OTHER","NULL","null","FIRE","PA","ป้ายแดง"):
        # normalize: ลบ space
        db_plates.add(p.replace(" ","").lower())

print(f"DB plates (normalized, distinct): {len(db_plates)}")
print(f"Sample DB plates: {list(db_plates)[:15]}\n")

# แยก plate จาก filenames
THAI = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"
PDF_FOLDER = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"

file_plates = set()
file_to_plate = {}
for f in os.listdir(PDF_FOLDER):
    if not f.lower().endswith(".pdf"): continue
    # plate pattern: [optional digit] + 1-2 Thai consonants + 1-4 digits
    m = re.match(rf"^(\d?[{THAI}]{{1,2}}\d{{1,4}}|\d{{6,7}})\s*[\sก-ฮพ\(\.]", f)
    if m:
        p = m.group(1).replace(" ","").lower()
        file_plates.add(p)
        file_to_plate[f] = p

print(f"Filename plates (normalized, distinct): {len(file_plates)}")
print(f"Sample file plates: {list(file_plates)[:15]}\n")

# Intersection
common = db_plates & file_plates
print(f"Intersection (perfect match): {len(common)}")
print(f"  examples: {list(common)[:15]}\n")

# Files that match
matched_files = [f for f,p in file_to_plate.items() if p in db_plates]
unmatched_files_plate = [f for f,p in file_to_plate.items() if p not in db_plates]
print(f"Files with plate matching DB: {len(matched_files)} / {len(file_to_plate)}")
print(f"Files with plate NOT in DB: {len(unmatched_files_plate)}")
print(f"  examples: {unmatched_files_plate[:10]}\n")

# Cross-format check: ลอง partial (last 4 digits)
plate_digits = {p: re.search(r"\d{3,4}$", p).group() if re.search(r"\d{3,4}$", p) else "" for p in file_plates}
db_plate_digits = {p: re.search(r"\d{3,4}$", p).group() if re.search(r"\d{3,4}$", p) else "" for p in db_plates}
common_digits = set(plate_digits.values()) & set(db_plate_digits.values())
print(f"Common last-4-digit plates: {len(common_digits)}")
