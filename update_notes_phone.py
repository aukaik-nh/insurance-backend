"""
update_notes_phone.py — อัปเดต notes และ phone ของ records ที่ import ไปแล้ว
โดยดึงจาก zzapp_export.csv (remark1 + remarkyok → notes, telephone → phone)

วิธีใช้:
    python update_notes_phone.py --dry-run     # ดูว่าจะอัปเดตอะไรบ้าง
    python update_notes_phone.py               # อัปเดตจริง
"""

import os, csv, sys, argparse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()
CSV_PATH = Path(__file__).parent / "zzapp_export.csv"


def build_notes(r: dict) -> str | None:
    rm1 = (r.get("remark1") or "").strip()
    rmy = (r.get("remarkyok") or "").strip()
    parts = [p for p in [rm1, rmy] if p]
    return " | ".join(parts) if parts else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--csv", default=str(CSV_PATH))
    args = parser.parse_args()

    with open(args.csv, encoding="utf-16") as f:
        rows = list(csv.DictReader(f))

    # เก็บเฉพาะ rows ที่มี app_number และมีข้อมูล notes หรือ phone
    to_update = []
    for r in rows:
        app = (r.get("app") or "").strip()
        if not app:
            continue
        notes = build_notes(r)
        phone = (r.get("telephone") or "").strip() or None
        if notes or phone:
            to_update.append({"app_number": app, "notes": notes, "phone": phone})

    print(f"Records ที่มีข้อมูล notes/phone: {len(to_update)}")

    if args.dry_run:
        print("\n[DRY RUN] ตัวอย่าง 15 รายการแรก:")
        for r in to_update[:15]:
            print(f"  app={r['app_number']}  phone={r['phone']}  notes={repr((r['notes'] or '')[:60])}")
        return

    from supabase import create_client
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    ok = failed = not_found = 0
    for r in to_update:
        payload = {}
        if r["notes"]:
            payload["notes"] = r["notes"]
        if r["phone"]:
            payload["phone"] = r["phone"]

        try:
            res = supabase.table("insurance_policies")\
                .update(payload)\
                .eq("app_number", r["app_number"])\
                .execute()
            if res.data:
                ok += 1
                print(f"  OK  app={r['app_number']}  notes={repr((r['notes'] or '')[:50])}")
            else:
                not_found += 1
                print(f"  --  app={r['app_number']} (ไม่พบใน DB)")
        except Exception as e:
            failed += 1
            print(f"  ERR app={r['app_number']} : {e}")

    print(f"\n{'='*50}")
    print(f"  อัปเดตสำเร็จ : {ok}")
    print(f"  ไม่พบใน DB  : {not_found}")
    print(f"  ล้มเหลว     : {failed}")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
