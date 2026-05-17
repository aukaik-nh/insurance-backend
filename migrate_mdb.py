"""
migrate_mdb.py — ย้ายข้อมูลจาก Access MDB (Baby78) → Supabase

ต้องการ:
  1. ติดตั้ง Microsoft Access Database Engine (32-bit หรือ 64-bit แล้วแต่ Python)
     ดาวน์โหลดฟรีที่: https://www.microsoft.com/en-us/download/details.aspx?id=54920
  2. รู้รหัสผ่านของ MDB

วิธีใช้ (ใน 32-bit PowerShell — เพราะ Jet driver เป็น 32-bit):
    "D:/insurance-backend/venv/Scripts/python.exe" migrate_mdb.py \
        --mdb "C:/path/Baby78_Safety.mdb" \
        --password "your_password"

Column mapping (Baby78 → Supabase):
    zzapp.app           → app_number       (ใบคำขอ)
    zzapp.policy        → policy_number    (เลขกรมธรรม์)
    zzapp.insurance     → company_code     (บริษัทประกัน)
    zzapp.policytype    → policy_type      (ประเภท: ASSET/STY/…)
    zzapp.newrenew      → new_renew        (N=ใหม่ R=ต่ออายุ)
    zzapp.namethai      → insured_name     (ผู้เอาประกัน)
    zzapp.phone         → phone            (เบอร์โทร)
    zzapp.address1+2    → insured_address  (ที่อยู่)
    zzapp.license       → license_plate    (ทะเบียนรถ)
    zzapp.licenseprovince→license_province (จังหวัดทะเบียน)
    zzapp.chasis        → chassis_no       (เลขตัวถัง)
    zzapp.model         → car_model        (ยี่ห้อ/รุ่น)
    zzapp.suminsure     → sum_insured      (ทุนเอาประกัน)
    zzapp.netpremium    → net_premium      (เบี้ยสุทธิ)
    zzapp.stamp         → stamp_duty       (อากร)
    zzapp.vat           → vat              (ภาษี)
    zzapp.totalpremium  → total_premium    (เบี้ยรวม)
    zzapp.datestart     → coverage_start   (คุ้มครองเริ่ม)
    zzapp.dateend       → coverage_end     (คุ้มครองสิ้นสุด)
    zzapp.datereceive   → date_notify      (แจ้งงาน)
    zzapp.datecancel    → date_cancel      (วันยกเลิก)
    zzapp.datepolicyreceive → date_policy_receive (วันรับกรมธรรม์)
    zzapp.agent         → agent_code       (รหัสตัวแทน)
    zzapp.agentname     → broker_name      (ชื่อตัวแทน)
"""

import os, sys, re, argparse
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))
from supabase import create_client

# ── map zzapp → insurance_policies ───────────────────────────────────
def row_to_supabase(r: dict) -> dict:
    """แปลง zzapp row → Supabase row"""

    def safe(v): return str(v).strip() if v else None
    def safe_float(v):
        try: return float(str(v).replace(",",""))
        except: return None
    def fmt_date(v):
        if not v: return None
        if isinstance(v, datetime): return v.strftime("%Y-%m-%d")
        s = str(v).strip()
        # DD/MM/YYYY หรือ YYYY-MM-DD
        m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
        if m:
            d,mo,y = int(m.group(1)),int(m.group(2)),int(m.group(3))
            if y >= 2500: y -= 543
            if y < 100:   y += 2000
            return f"{y:04d}-{mo:02d}-{d:02d}"
        m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
        if m:
            y = int(m.group(1))
            if y >= 2500: y -= 543
            return f"{y:04d}-{m.group(2)}-{m.group(3)}"
        return None

    # address: รวม address1 + address2 + province + postcode
    addr_parts = [safe(r.get(k)) for k in ("address1","address2","province","postcode")]
    address = " ".join(p for p in addr_parts if p) or None

    # model → car_make + car_model (เก็บทั้งหมดใน car_model)
    model_str = safe(r.get("model")) or ""
    model_ext = safe(r.get("modelextend")) or ""
    car_model = (model_str + (" " + model_ext if model_ext else "")).strip() or None

    # ตัวแทน: agent = รหัส, agentname = ชื่อ (ถ้าไม่มี agentname ใช้ agent แทน)
    agent_code  = safe(r.get("agent"))
    broker_name = safe(r.get("agentname")) or agent_code

    return {
        # ── ฟิลด์ใหม่จาก MDB ──
        "app_number":          safe(r.get("app")),
        "policy_type":         safe(r.get("policytype")),
        "new_renew":           safe(r.get("newrenew")),
        "date_notify":         fmt_date(r.get("datereceive")),
        "date_cancel":         fmt_date(r.get("datecancel")),
        "date_policy_receive": fmt_date(r.get("datepolicyreceive")),
        "license_province":    safe(r.get("licenseprovince")),
        "sum_insured":         safe_float(r.get("suminsure")),
        "agent_code":          agent_code,
        # ── ฟิลด์เดิม ──
        "policy_number":   safe(r.get("policy")),
        "company_code":    safe(r.get("insurance")),
        "insured_name":    safe(r.get("namethai")),
        "phone":           safe(r.get("phone")),
        "insured_address": address,
        "license_plate":   safe(r.get("license")),
        "chassis_no":      safe(r.get("chasis")),
        "car_make":        None,
        "car_model":       car_model,
        "car_year":        None,
        "coverage_start":  fmt_date(r.get("datestart")),
        "coverage_end":    fmt_date(r.get("dateend")),
        "net_premium":     safe_float(r.get("netpremium")),
        "stamp_duty":      safe_float(r.get("stamp")),
        "vat":             safe_float(r.get("vat")),
        "total_premium":   safe_float(r.get("totalpremium")),
        "broker_name":     broker_name,
        "manually_edited": False,
    }


# ── Connect MDB ───────────────────────────────────────────────────────
def connect_mdb(mdb_path: str, password: str = ""):
    """เชื่อมต่อ MDB ผ่าน ADODB (ต้องใช้ 32-bit Python หรือ 64-bit ACE driver)"""
    import win32com.client
    conn = win32com.client.Dispatch("ADODB.Connection")
    cs   = (f"Provider=Microsoft.Jet.OLEDB.4.0;"
            f"Data Source='{mdb_path}';"
            f"Jet OLEDB:Database Password='{password}'")
    conn.Open(cs)
    return conn


def fetch_all(conn, sql: str) -> list[dict]:
    rs = win32com.client.Dispatch("ADODB.Recordset")
    rs.Open(sql, conn)
    cols = [rs.Fields(i).Name for i in range(rs.Fields.Count)]
    rows = []
    while not rs.EOF:
        row = {c: (rs.Fields(c).Value) for c in cols}
        rows.append(row)
        rs.MoveNext()
    rs.Close()
    return rows


# ── MAIN ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Migrate Baby78 MDB → Supabase")
    parser.add_argument("--mdb",      required=True, help="path to .mdb file")
    parser.add_argument("--password", default="",    help="database password")
    parser.add_argument("--limit",    type=int, default=0)
    parser.add_argument("--dry-run",  action="store_true")
    args = parser.parse_args()

    try:
        import win32com.client
    except ImportError:
        print("❌ ต้องการ pywin32: pip install pywin32")
        sys.exit(1)

    print(f"\n{'='*55}")
    print(f"  Migrate MDB → Supabase")
    print(f"  MDB: {args.mdb}")
    print(f"{'='*55}\n")

    # เชื่อมต่อ MDB
    try:
        conn = connect_mdb(args.mdb, args.password)
        print("✓ เชื่อมต่อ MDB สำเร็จ")
    except Exception as e:
        print(f"❌ เชื่อมต่อ MDB ไม่ได้: {e}")
        print("   → ตรวจสอบ: 1) รหัสผ่าน  2) ติดตั้ง Access Database Engine")
        print("   → https://www.microsoft.com/en-us/download/details.aspx?id=54920")
        sys.exit(1)

    # ดึงข้อมูล
    sql = """
    SELECT app, policy, insurance, policytype, newrenew,
           namethai, phone,
           address1, address2, province, postcode,
           license, licenseprovince, chasis,
           model, modelextend,
           suminsure,
           datestart, dateend, datecancel, datereceive, datepolicyreceive,
           netpremium, stamp, vat, totalpremium,
           agent, agentname
    FROM zzapp
    WHERE policy IS NOT NULL
    ORDER BY app
    """
    if args.limit: sql = sql.rstrip() + f" LIMIT {args.limit}"

    print("กำลังดึงข้อมูลจาก zzapp...")
    rows = fetch_all(conn, sql)
    conn.Close()
    print(f"✓ พบ {len(rows):,} records\n")

    supabase = None if args.dry_run else create_client(
        os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY")
    )

    ok = failed = 0
    for i, r in enumerate(rows):
        sb_row = row_to_supabase(r)
        pol    = sb_row.get("policy_number","?")
        name   = str(sb_row.get("insured_name","?"))[:20]

        if args.dry_run:
            print(f"[{i+1:>5}] DRY policy={pol} name={name}")
            ok += 1
            continue

        try:
            supabase.table("insurance_policies").insert(sb_row).execute()
            print(f"[{i+1:>5}] ✓ policy={pol:<20} {name}")
            ok += 1
        except Exception as e:
            print(f"[{i+1:>5}] ✗ policy={pol} ERROR: {e}")
            failed += 1

    print(f"\n{'='*55}")
    print(f"  สำเร็จ : {ok:,}  ล้มเหลว : {failed:,}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    main()
