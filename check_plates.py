"""ดู license_plate ใน DB ว่า format ไหน + ลอง search ตัวอย่าง"""
import sys, os
sys.stdout.reconfigure(encoding="utf-8")
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

# ดู unique format ของ license_plate
r = sb.table("insurance_policies").select("license_plate").limit(2000).execute()
plates = [p["license_plate"] for p in r.data if p["license_plate"]]
print(f"Total non-null plates: {len(plates)} from first 2000\n")

# Distinct length & has-space stats
import re
fmts = {}
samples = {}
for p in plates:
    if not p: continue
    if p in ("OTHER","NULL","null"): continue
    has_sp = " " in p
    has_lead_digit = bool(re.match(r"^\d", p))
    key = f"lead_digit={has_lead_digit} has_space={has_sp} len={len(p)}"
    fmts[key] = fmts.get(key, 0) + 1
    samples.setdefault(key, []).append(p)

for k, c in sorted(fmts.items(), key=lambda x:-x[1])[:15]:
    eg = samples[k][:5]
    print(f"  [{c:>4}] {k:50s} examples: {eg}")

# ลอง search ทะเบียน "1กก" หรือ "1กก5226"
print("\n--- search '1กก' (with space variants) ---")
for q in ["1กก", "1กก5226", "กก5226"]:
    r = sb.table("insurance_policies").select("license_plate, insured_name").ilike("license_plate", f"%{q}%").limit(10).execute()
    print(f"  '%{q}%': {len(r.data)} matches  {[d['license_plate'] for d in r.data[:5]]}")

# ลองทะเบียนที่ปรากฏใน DB
print("\n--- sample real plates in DB ---")
import random
real = [p for p in plates if p not in ("OTHER",) and p.strip()][:10]
for p in real:
    print(f"  {p!r}")
