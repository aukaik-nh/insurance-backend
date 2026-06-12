"""
preflight_audit.py
─────────────────────────────────────────────────────────────────────
Audit ทุกอย่างก่อนลง DB / R2 จริง:

A. DATA audit  — เทียบ MDB (datestart >= 2024) vs DB ปัจจุบัน
   - policies ใน MDB ที่ไม่อยู่บนเว็บ (ตกค้าง)
   - policies บนเว็บที่ไม่อยู่ใน MDB (ส่วนเกิน)
   - policies ที่ field ไม่ตรงกัน (เปรียบเทียบ key fields)

B. PDF audit — วิเคราะห์ BabyScan folder
   - ขนาดรวม / จำนวนไฟล์ / breakdown ตาม doc_type
   - ไฟล์ที่ year >= 2567 (พ.ศ. 67+) → จะ upload
   - ไฟล์ที่ year < 2567 → จะ skip
   - ไฟล์ที่ match กับ DB → กี่ %
   - upload size estimate

ไม่แตะ DB, ไม่แตะ R2 — แค่ดูและรายงาน
"""
import os, sys, json, re
from pathlib import Path
from collections import Counter, defaultdict

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

import win32com.client
from services.supabase_shim import create_client
from services.filename_matcher import parse_filename, find_matches, normalize

MDB         = r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyFolder\Baby78_Safety.mdb"
PWD         = "4949"
PDF_FOLDER  = r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan"
FROM_DATE   = "2024-01-01"
TO_DATE     = "2030-01-01"
MIN_YEAR_BE = 2567


# ── doc_type detection (mirror of match_and_upload_r2.py) ────────────
DOC_TYPE_PATTERNS = [
    (r"พรบ|พ\.?ร\.?บ\.?|prb|compulsory", "prb"),
    (r"สลักหลัง|สลัก|endorsement",        "endorsement"),
    (r"กธ|กรมธรรม",                       "main"),
]
SKIP_PATTERNS = [
    r"ใบแจ้งหนี้", r"บัตรเครดิต", r"บัตรประชาชน",
    r"ใบขับขี่", r"ตรวจสภาพ", r"ทะเบียนรถ",
    r"หนังสือมอบ", r"หนังสือรับรอง", r"แต่งตั้งนายหน้า",
    r"คำขอ", r"ปฎิเสธ", r"เรียกร้อง", r"สินไหม",
    r"\.xls", r"\.jpg", r"\.JPG", r"\.jpeg", r"\.png",
    r"แลป|^F$",
]
OTHER_PATTERNS = [r"ยกเลิก", r"แก้ไข", r"ชำระเบี้ย", r"เอกสาร"]


def detect_doc_type(filename: str) -> str | None:
    for pat in SKIP_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE): return None
    for pat, dt in DOC_TYPE_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE): return dt
    for pat in OTHER_PATTERNS:
        if re.search(pat, filename): return "other"
    return None


# ─────────────────────────────────────────────────────────────────────
# A. DATA AUDIT
# ─────────────────────────────────────────────────────────────────────
def audit_data():
    print("\n" + "="*70)
    print("  A.  DATA AUDIT  (MDB 2024+ vs DB ปัจจุบัน)")
    print("="*70 + "\n")

    # ── MDB ──
    conn = win32com.client.Dispatch("ADODB.Connection")
    cs = f"Provider=Microsoft.ACE.OLEDB.16.0;Data Source='{MDB}';Jet OLEDB:Database Password='{PWD}'"
    conn.Open(cs)

    rs = win32com.client.Dispatch("ADODB.Recordset")
    rs.Open(f"""SELECT app, policy, policytype, namethai, license, datestart, dateend,
                       netpremium, totalpremium
                FROM zzapp
                WHERE policy IS NOT NULL
                  AND datestart >= #{FROM_DATE}#
                  AND datestart < #{TO_DATE}#""", conn)
    mdb_rows = []
    while not rs.EOF:
        mdb_rows.append({
            "app":      rs.Fields("app").Value,
            "policy":   rs.Fields("policy").Value,
            "policytype": rs.Fields("policytype").Value,
            "namethai": rs.Fields("namethai").Value,
            "license":  rs.Fields("license").Value,
            "datestart": rs.Fields("datestart").Value,
            "dateend":   rs.Fields("dateend").Value,
            "netpremium": rs.Fields("netpremium").Value,
            "totalpremium": rs.Fields("totalpremium").Value,
        })
        rs.MoveNext()
    rs.Close()
    conn.Close()
    print(f"MDB records (2024+, with policy) : {len(mdb_rows):,}")

    # MDB unique policies
    mdb_policies = defaultdict(list)
    for r in mdb_rows:
        mdb_policies[str(r["policy"]).strip()].append(r)
    print(f"  unique policy_numbers           : {len(mdb_policies):,}")
    dupes = {k: v for k, v in mdb_policies.items() if len(v) > 1}
    print(f"  duplicate policy_numbers in MDB : {len(dupes):,}  "
          f"(total dup rows: {sum(len(v) for v in dupes.values()):,})")

    # ── DB ──
    sb = create_client()
    db_rows = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, policy_number, policy_type, insured_name, license_plate, "
            "coverage_start, coverage_end, net_premium, total_premium, "
            "pdf_url, pdf_filename, manually_edited"
        ).range(page*1000, page*1000+999).execute()
        if not r.data: break
        db_rows.extend(r.data)
        if len(r.data) < 1000: break
        page += 1
    print(f"\nDB records total                  : {len(db_rows):,}")
    db_with_policy = [r for r in db_rows if r.get("policy_number")]
    print(f"  with policy_number              : {len(db_with_policy):,}")
    print(f"  with pdf_url                    : {sum(1 for r in db_rows if r.get('pdf_url')):,}")
    print(f"  manually_edited                 : {sum(1 for r in db_rows if r.get('manually_edited')):,}")

    db_by_policy = {r["policy_number"]: r for r in db_with_policy if r.get("policy_number")}

    # ── Diff ──
    mdb_set = set(mdb_policies.keys())
    db_set  = set(db_by_policy.keys())

    missing_in_db = mdb_set - db_set           # ใน MDB แต่ไม่อยู่บนเว็บ ⇒ ต้อง insert
    extra_in_db   = db_set - mdb_set           # บนเว็บแต่ไม่อยู่ใน MDB ⇒ อาจเป็น data outside 67-69
    in_both       = mdb_set & db_set

    print(f"\n— DIFF —")
    print(f"  ✓ in both (MDB & DB)           : {len(in_both):,}")
    print(f"  ⚠ in MDB but NOT in DB (ตกค้าง) : {len(missing_in_db):,}")
    print(f"  ℹ in DB but NOT in MDB         : {len(extra_in_db):,}")

    if missing_in_db:
        print(f"\n  ── ตัวอย่างที่ตกค้าง 10 อันแรก ──")
        for pn in list(missing_in_db)[:10]:
            r = mdb_policies[pn][0]
            print(f"    {pn:30s} {r['policytype']:6s} {str(r['namethai'])[:25]:25s} {r['license']}")

    if extra_in_db:
        print(f"\n  ── ตัวอย่าง DB ส่วนเกิน 10 อันแรก ──")
        for pn in list(extra_in_db)[:10]:
            r = db_by_policy[pn]
            print(f"    {pn:30s} type={r.get('policy_type','?'):6s} name={str(r.get('insured_name'))[:25]:25s}")

    # ── Field-level diff (เฉพาะที่อยู่ทั้ง MDB & DB) ──
    print(f"\n— FIELD MISMATCH (เฉพาะ key fields) —")
    field_diff = {"insured_name": 0, "license_plate": 0,
                  "coverage_start": 0, "coverage_end": 0,
                  "net_premium": 0, "total_premium": 0}
    sample_diffs = []
    for pn in in_both:
        mdb_r = mdb_policies[pn][0]
        db_r  = db_by_policy[pn]
        diffs = []
        # name
        mname = str(mdb_r["namethai"] or "").strip()
        dname = str(db_r.get("insured_name") or "").strip()
        if mname != dname:
            field_diff["insured_name"] += 1
            diffs.append(("name", mname[:30], dname[:30]))
        # plate
        mp = str(mdb_r["license"] or "").strip()
        dp = str(db_r.get("license_plate") or "").strip()
        if mp != dp:
            field_diff["license_plate"] += 1
            diffs.append(("plate", mp, dp))
        # dates (mdb is datetime, db is str)
        if mdb_r["datestart"]:
            mds = mdb_r["datestart"].strftime("%Y-%m-%d")
            dds = str(db_r.get("coverage_start") or "")[:10]
            if mds != dds:
                field_diff["coverage_start"] += 1
                diffs.append(("cstart", mds, dds))
        if mdb_r["dateend"]:
            mde = mdb_r["dateend"].strftime("%Y-%m-%d")
            dde = str(db_r.get("coverage_end") or "")[:10]
            if mde != dde:
                field_diff["coverage_end"] += 1
                diffs.append(("cend", mde, dde))
        # premium
        mnp = float(mdb_r["netpremium"] or 0)
        dnp = float(db_r.get("net_premium") or 0)
        if abs(mnp - dnp) > 0.01:
            field_diff["net_premium"] += 1
        mtp = float(mdb_r["totalpremium"] or 0)
        dtp = float(db_r.get("total_premium") or 0)
        if abs(mtp - dtp) > 0.01:
            field_diff["total_premium"] += 1
        if diffs and len(sample_diffs) < 5:
            sample_diffs.append((pn, diffs))

    for f, c in field_diff.items():
        print(f"  {f:18s} : {c:>5,} records differ")
    if sample_diffs:
        print(f"\n  ── ตัวอย่าง field mismatch ──")
        for pn, ds in sample_diffs:
            print(f"    {pn}:")
            for fname, mv, dv in ds:
                print(f"      {fname}: MDB='{mv}'  vs  DB='{dv}'")

    # write report
    report_path = Path(__file__).parent / "preflight_data_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "mdb_total": len(mdb_rows),
            "mdb_unique_policies": len(mdb_policies),
            "mdb_duplicates": len(dupes),
            "db_total": len(db_rows),
            "db_with_policy": len(db_with_policy),
            "in_both": len(in_both),
            "missing_in_db": sorted(missing_in_db),
            "extra_in_db": sorted(extra_in_db),
            "field_diff": field_diff,
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  → รายงานเต็มที่: {report_path}")

    return {
        "missing_in_db": missing_in_db,
        "extra_in_db":   extra_in_db,
        "db_policies":   db_by_policy,
    }


# ─────────────────────────────────────────────────────────────────────
# B. PDF AUDIT
# ─────────────────────────────────────────────────────────────────────
def audit_pdfs(db_policies_info):
    print("\n" + "="*70)
    print("  B.  PDF AUDIT  (BabyScan folder)")
    print("="*70 + "\n")

    files = [f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")]
    print(f"Total PDF files : {len(files):,}")

    # โหลด policies (พร้อม type) สำหรับ matching
    sb = create_client()
    policies = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, license_plate, insured_address, insured_name, "
            "coverage_start, coverage_end, policy_type, pdf_url"
        ).range(page*1000, page*1000+999).execute()
        if not r.data: break
        policies.extend(r.data)
        if len(r.data) < 1000: break
        page += 1
    print(f"DB policies     : {len(policies):,}")

    # Stats
    by_doc_type = Counter()      # main/prb/endorsement/other/skipped
    by_year     = Counter()      # year_be → count
    by_match    = Counter()      # match/no-match/skipped
    upload_size = 0
    skip_size   = 0
    upload_files = []
    nomatch_files = []
    upload_by_type = Counter()

    for fname in files:
        fpath = os.path.join(PDF_FOLDER, fname)
        try:
            fsize = os.path.getsize(fpath)
        except OSError:
            fsize = 0

        doc_type = detect_doc_type(fname)
        if doc_type is None:
            parsed_chk = parse_filename(fname)
            if parsed_chk["kind"] in ("plate", "address"):
                doc_type = "main"
            else:
                by_doc_type["skip_filetype"] += 1
                skip_size += fsize
                continue

        parsed = parse_filename(fname)
        year_be = parsed.get("year_be")
        by_year[year_be or "no_year"] += 1

        # year filter
        if year_be and year_be < MIN_YEAR_BE:
            by_doc_type["skip_year"] += 1
            skip_size += fsize
            continue

        # try match
        matched = find_matches(parsed, policies, best_only=True)
        if not matched:
            by_doc_type[f"{doc_type}_no_match"] += 1
            by_match["no_match"] += 1
            nomatch_files.append({"file": fname, "kind": parsed["kind"], "key": parsed["key"], "year": year_be})
            continue

        by_doc_type[doc_type] += 1
        by_match["matched"] += 1
        upload_size += fsize
        upload_by_type[doc_type] += 1
        upload_files.append(fname)

    # ── output ──
    print(f"\n— Breakdown by classification —")
    for k in ("main", "prb", "endorsement", "other",
              "main_no_match", "prb_no_match", "endorsement_no_match", "other_no_match",
              "skip_year", "skip_filetype"):
        v = by_doc_type.get(k, 0)
        if v:
            print(f"  {k:25s} : {v:>6,}")

    print(f"\n— Upload prediction —")
    total_match  = sum(upload_by_type.values())
    print(f"  Will upload (matched)  : {total_match:,} files")
    print(f"  Upload size estimate   : {upload_size/(1024*1024):.1f} MB  ({upload_size/(1024**3):.2f} GB)")
    print(f"  Will skip              : {by_doc_type['skip_year'] + by_doc_type['skip_filetype']:,} files  ({skip_size/(1024*1024):.1f} MB)")
    print(f"  No-match (year ok, not in DB): {by_match['no_match']:,} files")

    print(f"\n— R2 storage check —")
    print(f"  R2 free tier limit     : 10 GB storage / 1M class-A ops/month")
    print(f"  Predicted usage        : ~{upload_size/(1024**3):.2f} GB / {total_match:,} ops")
    if upload_size/(1024**3) > 10:
        print(f"  ⚠️  WARNING: Predicted upload may exceed free tier!")
    else:
        print(f"  ✓ Within free tier")

    print(f"\n— Year breakdown —")
    for y in sorted([k for k in by_year.keys() if isinstance(k, int)], reverse=True)[:10]:
        print(f"  พ.ศ. {y}  : {by_year[y]:>5,}")
    print(f"  no year   : {by_year.get('no_year', 0):>5,}")

    # write report
    report_path = Path(__file__).parent / "preflight_pdf_report.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "total_files": len(files),
            "by_doc_type": dict(by_doc_type),
            "by_year": {str(k): v for k, v in by_year.items()},
            "by_match": dict(by_match),
            "upload_count": total_match,
            "upload_size_bytes": upload_size,
            "upload_by_type": dict(upload_by_type),
            "no_match_samples": nomatch_files[:50],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  → รายงานเต็มที่: {report_path}")


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    data_result = audit_data()
    audit_pdfs(data_result)
    print("\n" + "="*70)
    print("  AUDIT COMPLETE — ไม่มีการแก้ไข DB หรือ R2")
    print("="*70 + "\n")
