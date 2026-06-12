"""
migrate_mdb_v2.py — ย้ายข้อมูลจาก Baby78_Safety.mdb → Supabase
                    (mapping ตรง MDB จริง + upsert by policy_number + filter by date)

วิธีใช้:
    python migrate_mdb_v2.py --mdb "C:\\path\\Baby78_Safety.mdb" --dry-run --limit 20
    python migrate_mdb_v2.py --mdb "C:\\path\\Baby78_Safety.mdb" --from-date 2024-01-01
    python migrate_mdb_v2.py --mdb "C:\\path\\Baby78_Safety.mdb" --from-date 2024-01-01 --insert-only

flags:
    --from-date YYYY-MM-DD  : กรอง datestart >= วันที่นี้ (default 2024-01-01 = ปี พ.ศ. 67+)
    --to-date YYYY-MM-DD    : กรอง datestart < (default 2030-01-01 — กัน data error)
    --dry-run               : ไม่บันทึก แค่ preview
    --limit N               : จำกัด N records (เพื่อทดสอบ)
    --insert-only           : insert เท่านั้น (skip ถ้ามี policy_number แล้ว — ใช้กรณีรัน first time)
    --no-update-manually-edited : ถ้า record บนเว็บถูก manually_edited แล้ว ไม่ update ทับ (default = on)
"""
import os, sys, argparse, re
from pathlib import Path
from datetime import datetime, date

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

import win32com.client
from services.supabase_shim import create_client

MDB_PWD = "4949"


# ── helpers ──────────────────────────────────────────────────────────
def safe_str(v):
    if v is None: return None
    s = str(v).strip()
    return s or None


def safe_float(v):
    if v is None or v == "": return None
    try:
        return float(str(v).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


def safe_int(v):
    if v is None or v == "": return None
    try:
        n = int(float(str(v).strip()))
        return n if n > 0 else None
    except (ValueError, TypeError):
        return None


def fmt_date(v):
    """แปลง datetime/string → 'YYYY-MM-DD' (ค.ศ.)"""
    if v is None or v == "": return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})', s)
    if m:
        y = int(m.group(1))
        if y >= 2500: y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        if y >= 2500: y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


# ── mapping ──────────────────────────────────────────────────────────
def row_to_supabase(r: dict) -> dict:
    # address: address1 + address2 + province + postcode
    addr_parts = [safe_str(r.get(k)) for k in ("address1", "address2", "province", "postcode")]
    address = " ".join(p for p in addr_parts if p) or None

    # car_year: modelyear (อาจเป็น พ.ศ.)
    my = safe_int(r.get("modelyear"))
    if my and my >= 2500: my -= 543
    car_year = my

    agent_code = safe_str(r.get("agent"))
    kpp = safe_str(r.get("kpp"))
    broker_name = kpp or agent_code

    return {
        "app_number":          safe_str(r.get("app")),
        "policy_number":       safe_str(r.get("policy")),
        "company_code":        safe_str(r.get("insurance")),
        "policy_type":         safe_str(r.get("policytype")),
        "new_renew":           safe_str(r.get("newrenew")),
        "insured_name":        safe_str(r.get("namethai")),
        "phone":               safe_str(r.get("telephone")),
        "insured_address":     address,
        "license_plate":       safe_str(r.get("license")),
        "license_province":    safe_str(r.get("licenseprovince")),
        "chassis_no":          safe_str(r.get("chasis")),
        "car_make":            None,                              # ไม่มีใน MDB
        "car_model":           safe_str(r.get("model")),
        "car_year":            car_year,
        "sum_insured":         safe_float(r.get("damage")),
        "coverage_start":      fmt_date(r.get("datestart")),
        "coverage_end":        fmt_date(r.get("dateend")),
        "date_notify":         fmt_date(r.get("datenotify")),
        "date_policy_receive": fmt_date(r.get("datereceive")),
        "date_cancel":         fmt_date(r.get("datecancel")),
        "net_premium":         safe_float(r.get("netpremium")),
        "stamp_duty":          safe_float(r.get("stamp")),
        "vat":                 safe_float(r.get("vat")),
        "total_premium":       safe_float(r.get("totalpremium")),
        "agent_code":          agent_code,
        "broker_name":         broker_name,
        "notes":               safe_str(r.get("remark1")),
        "manually_edited":     False,
    }


# ── MDB connection ───────────────────────────────────────────────────
def connect_mdb(mdb_path: str):
    conn = win32com.client.Dispatch("ADODB.Connection")
    cs = (f"Provider=Microsoft.ACE.OLEDB.16.0;"
          f"Data Source='{mdb_path}';"
          f"Jet OLEDB:Database Password='{MDB_PWD}'")
    conn.Open(cs)
    return conn


def fetch_all(conn, sql: str) -> list[dict]:
    rs = win32com.client.Dispatch("ADODB.Recordset")
    rs.Open(sql, conn)
    cols = [rs.Fields(i).Name for i in range(rs.Fields.Count)]
    rows = []
    while not rs.EOF:
        rows.append({c: rs.Fields(c).Value for c in cols})
        rs.MoveNext()
    rs.Close()
    return rows


# ── MAIN ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mdb", required=True)
    ap.add_argument("--from-date", default="2024-01-01")
    ap.add_argument("--to-date",   default="2030-01-01")
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--limit",     type=int, default=0)
    ap.add_argument("--insert-only", action="store_true",
                    help="skip rows whose policy_number already exists (don't update)")
    args = ap.parse_args()

    print(f"\n{'='*60}")
    print(f"  Migrate MDB → Supabase  (v2)")
    print(f"  MDB    : {args.mdb}")
    print(f"  Filter : datestart >= {args.from_date} AND < {args.to_date}")
    if args.dry_run:     print("  Mode   : DRY-RUN")
    if args.insert_only: print("  Insert : insert-only (skip existing)")
    print(f"{'='*60}\n")

    conn = connect_mdb(args.mdb)
    print("✓ MDB connected")

    sql = f"""
    SELECT app, policy, insurance, policytype, newrenew,
           namethai, telephone,
           address1, address2, province, postcode,
           license, licenseprovince, chasis,
           model, modelyear, damage,
           datestart, dateend, datereceive, datecancel, datenotify,
           netpremium, stamp, vat, totalpremium,
           agent, kpp, remark1
    FROM zzapp
    WHERE policy IS NOT NULL
      AND datestart >= #{args.from_date}#
      AND datestart < #{args.to_date}#
    ORDER BY datestart, app
    """
    print("Fetching from MDB...")
    rows = fetch_all(conn, sql)
    conn.Close()
    print(f"✓ Fetched {len(rows):,} records")

    if args.limit and args.limit > 0:
        rows = rows[:args.limit]
        print(f"  (limited to {len(rows):,})")

    if args.dry_run:
        print(f"\n=== DRY-RUN: preview 10 records ===")
        for i, r in enumerate(rows[:10]):
            sb = row_to_supabase(r)
            print(f"\n[{i+1}] app={sb['app_number']} policy={sb['policy_number']}")
            print(f"     type={sb['policy_type']} renew={sb['new_renew']} "
                  f"company={sb['company_code']}")
            print(f"     name={sb['insured_name']}")
            print(f"     plate={sb['license_plate']} {sb['license_province'] or ''} "
                  f"chassis={sb['chassis_no']}")
            print(f"     car={sb['car_model']} year={sb['car_year']}")
            print(f"     cover={sb['coverage_start']} → {sb['coverage_end']}")
            print(f"     premium net={sb['net_premium']} total={sb['total_premium']} "
                  f"sum_insured={sb['sum_insured']}")
            print(f"     agent={sb['agent_code']} broker={sb['broker_name']}")
        # summarize
        by_type = {}
        for r in rows:
            t = (r.get("policytype") or "?").strip().upper()
            by_type[t] = by_type.get(t, 0) + 1
        print(f"\n=== By policy_type ({len(rows):,} total) ===")
        for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
            print(f"  {t:10s} : {c:>5,}")
        return

    # ── Insert / Upsert mode ──
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

    # โหลด existing policy_numbers ลง memory (รวดเร็ว)
    print("\nLoading existing policy_numbers from Supabase...")
    existing = {}  # policy_number → {"id": ..., "manually_edited": ...}
    page = 0
    while True:
        res = sb.table("insurance_policies").select(
            "id, policy_number, manually_edited"
        ).range(page*1000, page*1000+999).execute()
        if not res.data: break
        for d in res.data:
            pn = d.get("policy_number")
            if pn:
                existing[pn] = {"id": d["id"], "manually_edited": d.get("manually_edited") or False}
        if len(res.data) < 1000: break
        page += 1
    print(f"  found {len(existing):,} existing policies on web")

    inserted = updated = skipped_existing = skipped_protected = failed = 0
    batch_insert = []
    BATCH = 50

    def flush_batch():
        nonlocal inserted, batch_insert
        if not batch_insert: return
        try:
            sb.table("insurance_policies").insert(batch_insert).execute()
            inserted += len(batch_insert)
            print(f"  ✓ inserted batch of {len(batch_insert)}  (total inserted: {inserted})")
        except Exception as e:
            # batch failed → try one-by-one
            for r in batch_insert:
                try:
                    sb.table("insurance_policies").insert(r).execute()
                    inserted += 1
                except Exception as e2:
                    print(f"    ✗ policy={r.get('policy_number')} {e2}")
                    nonlocal_failed_inc()
        batch_insert = []

    def nonlocal_failed_inc():
        nonlocal failed
        failed += 1

    for i, r in enumerate(rows):
        sb_row = row_to_supabase(r)
        pn = sb_row.get("policy_number")
        if not pn:
            failed += 1
            continue

        if pn in existing:
            if args.insert_only:
                skipped_existing += 1
                continue
            # update existing — but skip if manually_edited
            if existing[pn]["manually_edited"]:
                skipped_protected += 1
                continue
            # update, but don't set manually_edited
            row_id = existing[pn]["id"]
            update_data = {k: v for k, v in sb_row.items() if k != "manually_edited"}
            try:
                sb.table("insurance_policies").update(update_data).eq("id", row_id).execute()
                updated += 1
                if updated % 50 == 0:
                    print(f"  ✓ updated {updated} (latest: {pn})")
            except Exception as e:
                print(f"  ✗ update policy={pn} {e}")
                failed += 1
        else:
            batch_insert.append(sb_row)
            if len(batch_insert) >= BATCH:
                flush_batch()

    flush_batch()

    print(f"\n{'='*60}")
    print(f"  Done")
    print(f"  inserted          : {inserted:,}")
    print(f"  updated           : {updated:,}")
    print(f"  skipped (exists)  : {skipped_existing:,}")
    print(f"  skipped (manual)  : {skipped_protected:,}")
    print(f"  failed            : {failed:,}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
