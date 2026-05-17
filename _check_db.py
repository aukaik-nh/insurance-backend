import os
from dotenv import load_dotenv
load_dotenv()
from supabase import create_client

sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))
total = sb.table("insurance_policies").select("id", count="exact").limit(1).execute()
has_pdf = sb.table("insurance_policies").select("id", count="exact").not_.is_("pdf_url", "null").limit(1).execute()
no_pdf = sb.table("insurance_policies").select("id", count="exact").is_("pdf_url", "null").limit(1).execute()

print(f"Total records   : {total.count or 0:,}")
print(f"Has PDF (linked): {has_pdf.count or 0:,}")
print(f"No PDF          : {no_pdf.count or 0:,}")

print("\nSample 5 records with PDF:")
sample = sb.table("insurance_policies").select("policy_number, insured_name, license_plate, pdf_filename, pdf_url").not_.is_("pdf_url", "null").limit(5).execute()
for r in sample.data:
    print(f"  {r.get('policy_number')} | {r.get('insured_name','')[:25]} | {r.get('license_plate')} | {r.get('pdf_filename','')[:30]}")
