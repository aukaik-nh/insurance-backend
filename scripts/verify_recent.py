"""Verify the 15 recently re-inserted policies exist."""
import os, sys, io
from pathlib import Path
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))
from dotenv import load_dotenv
load_dotenv(BACKEND_ROOT / ".env")
from services.supabase_shim import create_client

sb = create_client(None, None)

ids = [
    "29f13333-b0eb-4353-be8b-ab298dacb92b",  # img113
    "7510f24c-ebb5-4078-9fe6-41a8b0f3999e",  # img115
    "44c2d8f2-ad07-4237-98a6-f4aa5bf43957",  # img116
    "a8ad7977-218b-4edb-9c34-7e59599f6936",  # img117
    "d2dd6866-6741-4f5e-a4ee-fcfad0959c47",  # img118
    "b20d95b3-a6bb-44e7-bedf-d5b792568364",  # img119
    "e8a16c30-7164-4e65-a6c0-eef6491aeb5f",  # img120
    "73f29e17-1afd-430c-a002-5f0b3749a8e8",  # img121
    "ebb81101-6ddd-45b1-a004-d2888ec8852b",  # img122
    "c8413aa9-a350-415d-8d09-bd9206794451",  # img123
    "13581cd5-f930-40cd-8a83-9c919553c2bc",  # img124
    "f7e1359b-657d-46e9-8033-ccf37f0fb539",  # img125
    "d47573ce-a600-4b38-86b0-03426f7f9033",  # img126
    "0a528e2a-fbb6-44b9-9722-1d65e6dde5a1",  # img127
    "12242729-74fe-4adb-bc3b-0ae17294149b",  # img128
]

print(f"checking {len(ids)} ids...")
missing = []
for pid in ids:
    r = sb.table("insurance_policies").select("id,policy_number,insured_name,license_plate,pdf_filename,pdf_url,coverage_start").eq("id", pid).execute()
    if r.data:
        row = r.data[0]
        print(f"OK {pid[:8]} pol={row.get('policy_number') or '-':<25} plate={row.get('license_plate') or '-':<15} start={row.get('coverage_start')} name={row.get('insured_name')[:30] if row.get('insured_name') else '-'}")
    else:
        print(f"MISSING {pid}")
        missing.append(pid)

print(f"\ntotal found: {len(ids)-len(missing)}/{len(ids)}")
