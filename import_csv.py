"""
import_csv.py — นำเข้าข้อมูลจาก zzapp_export.csv → Supabase insurance_policies

Column mapping (Baby78 zzapp → Supabase):
    app            → app_number
    policy         → policy_number
    insurance      → company_code
    policytype     → policy_type      (M/ASSET/STY/…)
    newrenew       → new_renew        (N=ใหม่ R=ต่ออายุ)
    namethai       → insured_name
    telephone      → phone
    address1+2+province+postcode → insured_address
    license        → license_plate
    licenseprovince→ license_province
    chasis         → chassis_no
    model+modelextend → car_model
    modelyear      → car_year
    damage         → sum_insured      (ทุนเสียหาย)
    datestart      → coverage_start
    dateend        → coverage_end
    datereceive    → date_notify      (แจ้งงาน)
    datecancel     → date_cancel
    datepolicyreceive → date_policy_receive
    netpremium     → net_premium
    stamp          → stamp_duty
    vat            → vat
    totalpremium   → total_premium
    agent          → agent_code
    (kpp field)    → broker_name      (รหัส/ชื่อตัวแทน)

วิธีใช้:
    python import_csv.py --dry-run          # ทดสอบ ไม่บันทึก
    python import_csv.py                    # นำเข้าจริง
    python import_csv.py --skip-existing    # ข้ามที่มี policy_number อยู่แล้ว
    python import_csv.py --limit 50         # ทดสอบ 50 records แรก
"""

import os, sys, csv, re, argparse
from pathlib import Path
from datetime import datetime
from dotenv import load_dotenv

load_dotenv()

CSV_PATH = Path(__file__).parent / "zzapp_export.csv"


def fmt_date(v: str) -> str | None:
    if not v: return None
    v = v.strip()
    # DD/MM/YYYY or DD/MM/YY
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', v)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:  y += 2000
        if y >= 2500: y -= 543   # พ.ศ. → ค.ศ.
        return f"{y:04d}-{mo:02d}-{d:02d}"
    # YYYY-MM-DD
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', v)
    if m:
        y = int(m.group(1))
        if y >= 2500: y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    return None


def safe_float(v: str) -> float | None:
    if not v: return None
    try:
        return float(str(v).replace(",", "").strip()) or None
    except Exception:
        return None


def safe_int(v: str) -> int | None:
    try:
        n = int(str(v).strip())
        return n if n > 0 else None
    except Exception:
        return None


def safe(v: str) -> str | None:
    s = str(v).strip() if v else ""
    return s or None


def row_to_supabase(r: dict) -> dict:
    # ที่อยู่รวม
    addr_parts = [safe(r.get(k, "")) for k in ("address1", "address2", "province", "postcode")]
    address = " ".join(p for p in addr_parts if p) or None

    # โมเดลรถ
    model_str = safe(r.get("model", "")) or ""
    model_ext = safe(r.get("modelextend", "")) or ""
    car_model = (model_str + (" " + model_ext if model_ext else "")).strip() or None

    # ปีรถ (modelyear อาจเป็น พ.ศ.)
    my = safe_int(r.get("modelyear", ""))
    if my and my >= 2500: my -= 543
    car_year = my

    # ตัวแทน: agent = รหัส, kpp = อาจเก็บชื่อ/รหัสเพิ่ม
    agent_code = safe(r.get("agent", ""))
    kpp        = safe(r.get("kpp", ""))
    broker_name = kpp or agent_code   # ถ้าไม่มีชื่อตัวแทน ใช้รหัสแทน

    return {
        "app_number":          safe(r.get("app")),
        "policy_number":       safe(r.get("policy")),
        "company_code":        safe(r.get("insurance")),
        "policy_type":         safe(r.get("policytype")),
        "new_renew":           safe(r.get("newrenew")),
        "insured_name":        safe(r.get("namethai")),
        "phone":               safe(r.get("telephone")),
        "insured_address":     address,
        "license_plate":       safe(r.get("license")),
        "license_province":    safe(r.get("licenseprovince")),
        "chassis_no":          safe(r.get("chasis")),
        "car_make":            None,
        "car_model":           car_model,
        "car_year":            car_year,
        "sum_insured":         safe_float(r.get("damage")),
        "coverage_start":      fmt_date(r.get("datestart")),
        "coverage_end":        fmt_date(r.get("dateend")),
        "date_notify":         fmt_date(r.get("datereceive")),
        "date_cancel":         fmt_date(r.get("datecancel")),
        "date_policy_receive": fmt_date(r.get("datepolicyreceive")),
        "net_premium":         safe_float(r.get("netpremium")),
        "stamp_duty":          safe_float(r.get("stamp")),
        "vat":                 safe_float(r.get("vat")),
        "total_premium":       safe_float(r.get("totalpremium")),
        "agent_code":          agent_code,
        "broker_name":         broker_name,
        "manually_edited":     False,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv",           default=str(CSV_PATH))
    parser.add_argument("--dry-run",       action="store_true")
    parser.add_argument("--skip-existing", action="store_true",
                        help="ข้าม policy_number ที่มีอยู่แล้วใน Supabase")
    parser.add_argument("--limit",         type=int, default=0)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"❌ ไม่พบไฟล์: {csv_path}")
        sys.exit(1)

    # อ่าน CSV
    with open(csv_path, encoding="utf-16") as f:
        rows = list(csv.DictReader(f))

    # กรองเฉพาะ rows ที่มี policy
    rows = [r for r in rows if safe(r.get("policy"))]
    print(f"✓ พบ {len(rows):,} records ที่มี policy number")

    if args.limit:
        rows = rows[:args.limit]
        print(f"  (จำกัด {args.limit} records)")

    if args.dry_run:
        print("\n[DRY RUN] แสดง 10 records แรก:\n")
        for i, r in enumerate(rows[:10]):
            sb = row_to_supabase(r)
            print(f"  [{i+1}] app={sb['app_number']} policy={sb['policy_number']}"
                  f" company={sb['company_code']} type={sb['policy_type']}"
                  f" name={str(sb['insured_name'] or '')[:20]}"
                  f" plate={sb['license_plate']}"
                  f" start={sb['coverage_start']} end={sb['coverage_end']}"
                  f" premium={sb['net_premium']}")
        print(f"\n[DRY RUN] จะนำเข้า {len(rows):,} records ทั้งหมด")
        return

    from supabase import create_client
    supabase = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    # โหลด policy_number ที่มีอยู่แล้ว
    existing = set()
    if args.skip_existing:
        print("กำลังโหลด policy_number ที่มีอยู่แล้ว...")
        page = 0
        while True:
            res = supabase.table("insurance_policies")\
                .select("policy_number")\
                .range(page*1000, page*1000+999).execute()
            if not res.data: break
            for d in res.data:
                if d.get("policy_number"):
                    existing.add(d["policy_number"])
            if len(res.data) < 1000: break
            page += 1
        print(f"  มีอยู่แล้ว {len(existing):,} records")

    print(f"\nกำลังนำเข้า {len(rows):,} records...\n")

    ok = failed = skipped = 0
    BATCH = 50   # insert ทีละ 50 rows

    batch_rows = []
    for i, r in enumerate(rows):
        sb = row_to_supabase(r)
        pol = sb.get("policy_number", "?") or "?"

        if args.skip_existing and pol in existing:
            skipped += 1
            continue

        batch_rows.append(sb)

        if len(batch_rows) >= BATCH or i == len(rows) - 1:
            try:
                supabase.table("insurance_policies").insert(batch_rows).execute()
                ok += len(batch_rows)
                last = batch_rows[-1]
                print(f"[{ok:>5}/{len(rows):>5}] ✓ batch inserted ({len(batch_rows)} rows)"
                      f" last policy={last.get('policy_number','?')}")
                batch_rows = []
            except Exception as e:
                # ถ้า batch ล้มเหลว ลอง insert ทีละตัว
                for rb in batch_rows:
                    try:
                        supabase.table("insurance_policies").insert(rb).execute()
                        ok += 1
                    except Exception as e2:
                        print(f"  ✗ policy={rb.get('policy_number','?')} ERROR: {e2}")
                        failed += 1
                batch_rows = []

    print(f"\n{'='*55}")
    print(f"  สำเร็จ  : {ok:,}")
    print(f"  ข้าม    : {skipped:,}")
    print(f"  ล้มเหลว : {failed:,}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
