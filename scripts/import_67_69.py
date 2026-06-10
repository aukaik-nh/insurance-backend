"""
import_67_69.py — ลบของเก่า + import zzapp ปี พ.ศ. 67-69 จาก MDB ใหม่
─────────────────────────────────────────────────────────────────────
ขั้นตอน:
1. ลบ insurance_policies + policy_attachments ทั้งหมด (มี backup แล้ว)
2. ลบ R2 PDFs ทั้งหมด (16 ไฟล์)
3. Export zzapp WHERE YEAR(datestart) BETWEEN 2024-2026 → CSV (via vbs)
4. Read CSV → map columns → bulk insert into Neon

ห้ามรันถ้ายังไม่ confirm — มี backup แล้ว
"""
import os, sys, csv, json
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
import psycopg2.extras
import boto3
from botocore.client import Config


CSV_OUT = r"D:\tmp\zzapp_67_69.csv"


def delete_neon():
    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor()
    cur.execute("SELECT count(*) FROM insurance_policies")
    n_pol = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM policy_attachments")
    n_att = cur.fetchone()[0]
    print(f"  Before: insurance_policies={n_pol:,}, policy_attachments={n_att:,}")
    cur.execute("TRUNCATE TABLE policy_attachments CASCADE")
    cur.execute("TRUNCATE TABLE insurance_policies CASCADE")
    conn.commit()
    conn.close()
    print(f"  ✓ ลบเสร็จ")


def delete_r2():
    s3 = boto3.client("s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"))
    bucket = os.getenv("R2_BUCKET")
    n = 0
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket):
        objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
        if objs:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
            n += len(objs)
    print(f"  ✓ ลบ {n} ไฟล์จาก R2")
    return n


def buddhist_year_from_date(s):
    if not s: return None
    try:
        y = int(str(s)[:4])
        return y - 1957 if y >= 2000 else None
    except: return None


def parse_date(s):
    """parse หลาย format → ISO 'YYYY-MM-DD':
    - 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS'
    - 'D/M/YYYY' (Thai พ.ศ. ลบ 543 ถ้าปี >= 2400)
    - 'M/D/YYYY' (US)
    """
    if s is None: return None
    s = str(s).strip()
    if not s: return None

    # ISO format
    if len(s) >= 10 and s[4] == "-":
        try:
            datetime.strptime(s[:10], "%Y-%m-%d")
            return s[:10]
        except: pass

    # D/M/YYYY format (มี / 2 ตัว)
    import re as _re
    m = _re.match(r'^\s*(\d{1,2})/(\d{1,2})/(\d{2,4})', s)
    if m:
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            # ลบ 543 ถ้าเป็น พ.ศ.
            if y >= 2400: y -= 543
            elif y < 100: y += 2000
            if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
        except: pass
    return None


def safe_str(v, maxlen=None):
    if v is None: return None
    s = str(v).strip()
    if not s: return None
    return s[:maxlen] if maxlen else s


def safe_num(v):
    if v is None or v == "": return None
    try: return float(str(v).replace(",", ""))
    except: return None


def map_row(r: dict) -> dict:
    """zzapp row → insurance_policies row"""
    # address: address1 + address2 + province + postcode
    addr_parts = [safe_str(r.get(k)) for k in ("address1", "address2", "province", "postcode")]
    address = " ".join(p for p in addr_parts if p) or None

    return {
        "app_number":          safe_str(r.get("app"), 50),
        "policy_number":       safe_str(r.get("policy"), 100),
        "company_code":        safe_str(r.get("insurance"), 50),
        "policy_type":         safe_str(r.get("policytype"), 50),
        "new_renew":           safe_str(r.get("newrenew"), 5),
        "insured_name":        safe_str(r.get("namethai"), 255),
        "phone":               safe_str(r.get("telephone"), 50),
        "insured_address":     address,
        "license_plate":       safe_str(r.get("license"), 50),
        "license_province":    safe_str(r.get("licenseprovince"), 100),
        "chassis_no":          safe_str(r.get("chasis"), 100),
        "car_make":            None,
        "car_model":           safe_str(r.get("model"), 100),
        "car_year":            None,
        "coverage_start":      parse_date(r.get("datestart")),
        "coverage_end":        parse_date(r.get("dateend")),
        "date_notify":         parse_date(r.get("datenotify")),
        "date_cancel":         parse_date(r.get("datecancel")),
        "date_policy_receive": parse_date(r.get("datereceive")),
        "net_premium":         safe_num(r.get("netpremium")),
        "stamp_duty":          safe_num(r.get("stamp")),
        "vat":                 safe_num(r.get("vat")),
        "total_premium":       safe_num(r.get("totalpremium")),
        "sum_insured":         safe_num(r.get("damage")) or safe_num(r.get("totalpremium")),
        "agent_code":          safe_str(r.get("agent"), 50),
        "broker_name":         None,
        "notes":               safe_str(r.get("remark1"), 1000),
        "manually_edited":     False,
    }


def import_csv():
    """Read CSV → bulk insert"""
    path = Path(CSV_OUT)
    if not path.exists():
        print(f"  ❌ ไม่พบ CSV: {path}")
        print(f"  → รัน vbs dump ก่อน")
        return 0

    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor()

    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = []
        skipped = 0
        for r in reader:
            mapped = map_row(r)
            if not mapped["policy_number"] and not mapped["app_number"]:
                skipped += 1
                continue
            rows.append(mapped)

    if not rows:
        print("  ⚠️  ไม่มีแถวที่จะ insert")
        return 0

    columns = list(rows[0].keys())
    insert_sql = (
        f"INSERT INTO insurance_policies "
        f"({', '.join(chr(34)+c+chr(34) for c in columns)}) VALUES %s"
    )
    values = [tuple(r[c] for c in columns) for r in rows]
    psycopg2.extras.execute_values(cur, insert_sql, values, page_size=500)
    n = cur.rowcount
    conn.commit()
    conn.close()
    print(f"  ✓ Insert {n:,} rows (skip {skipped})")
    return n


def main():
    print(f"\n{'='*65}")
    print(f"  IMPORT 67-69 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}")

    print(f"\n[1] ลบของเก่าใน Neon...")
    delete_neon()

    print(f"\n[2] ลบของเก่าใน R2...")
    delete_r2()

    print(f"\n[3] Import จาก CSV...")
    n = import_csv()

    print(f"\n{'='*65}")
    print(f"  ✓ เสร็จ: import {n:,} rows")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
