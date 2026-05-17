"""ดูข้อมูลในเทเบิล insurance_policies สำหรับลูกค้าสุจิตต์ — ดู format ที่เก็บ"""
import os, sys
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

sys.stdout.reconfigure(encoding='utf-8')

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

r = sb.table("insurance_policies").select(
    "id, policy_number, app_number, insured_name, insured_address, license_plate, coverage_start, coverage_end, pdf_url, pdf_filename"
).or_(
    "insured_name.ilike.%สุจิตต์%,insured_address.ilike.%481/175%"
).order("coverage_start").execute()

print(f"Total: {len(r.data)} records\n")
for rec in r.data:
    print(f"[{rec.get('app_number','?'):>8}] policy={rec.get('policy_number','?')}")
    print(f"  name   : {rec.get('insured_name')}")
    print(f"  addr   : {rec.get('insured_address')}")
    print(f"  license: {rec.get('license_plate')}")
    print(f"  cov    : {rec.get('coverage_start')} → {rec.get('coverage_end')}")
    print(f"  pdf    : {rec.get('pdf_filename') or '(none)'}")
    print()
