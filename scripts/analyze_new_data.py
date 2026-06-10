"""
analyze_new_data.py — สรุปข้อมูลใหม่ก่อน import (อย่าลบของเก่าจนกว่า user confirm!)

Plan:
1. นับ rows ใน new MDB (zzapp) แยกตามปี + ประเภท
2. หา "active customers" = มี record ปี พ.ศ. 67-69 (ค.ศ. 2024-2026)
3. นับ records ทั้งหมดของ active customers (ทุกปี)
4. สแกน BabyScan folder → นับ PDF แยกตามปี
5. Match PDF ของ active customers (ทุกปี)
"""
import os, sys, re, win32com.client
from pathlib import Path
from collections import Counter, defaultdict

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

MDB = r"D:\tmp\Baby78_NEW.mdb"
PWD = "4949"
SCAN_DIR = Path(r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan")
ACTIVE_YEARS_AD = {2024, 2025, 2026}   # พ.ศ. 67-69


def open_mdb():
    conn = win32com.client.Dispatch("ADODB.Connection")
    for prov in ("Microsoft.Jet.OLEDB.4.0", "Microsoft.ACE.OLEDB.12.0", "Microsoft.ACE.OLEDB.16.0"):
        try:
            conn.Open(f"Provider={prov};Data Source={MDB};Jet OLEDB:Database Password={PWD};")
            return conn
        except Exception as e:
            last = e
    raise RuntimeError(f"เปิด MDB ไม่ได้: {last}")


def query(conn, sql):
    rs = conn.Execute(sql)[0]
    cols = [f.Name for f in rs.Fields]
    rows = []
    while not rs.EOF:
        rows.append({c: rs.Fields(c).Value for c in cols})
        rs.MoveNext()
    rs.Close()
    return rows


def main():
    print("="*70)
    print("  วิเคราะห์ข้อมูลใหม่ก่อน import")
    print("="*70)

    # ─── 1. MDB ─────────────────────────────────────────────
    print("\n[1] เปิด MDB ใหม่...")
    conn = open_mdb()
    total = query(conn, "SELECT COUNT(*) AS n FROM zzapp")[0]["n"]
    print(f"  Total zzapp: {total:,} rows")

    # 2. By year
    by_year = query(conn,
        "SELECT YEAR(datestart) AS y, COUNT(*) AS n FROM zzapp "
        "WHERE datestart IS NOT NULL GROUP BY YEAR(datestart) ORDER BY YEAR(datestart) DESC")
    valid_years = [r for r in by_year if r["y"] and 2000 <= r["y"] <= 2030]
    bad_years = [r for r in by_year if not r["y"] or not (2000 <= r["y"] <= 2030)]

    print(f"\n[2] By year (ปีปกติ):")
    for r in valid_years[:15]:
        be = r["y"] - 1957
        mark = " ← ACTIVE" if r["y"] in ACTIVE_YEARS_AD else ""
        print(f"  พ.ศ. {be} ({r['y']}): {r['n']:,}{mark}")
    if bad_years:
        bad_n = sum(r["n"] for r in bad_years)
        print(f"  [outlier ปีผิดปกติ {len(bad_years)} ปี, รวม {bad_n} rows — skip]")

    active_n = sum(r["n"] for r in valid_years if r["y"] in ACTIVE_YEARS_AD)
    print(f"\n  ปี 67-69 (active): {active_n:,} rows")

    # 3. Active customers = unique plates ที่มี record ปี 2024-2026
    print(f"\n[3] หา active customers (มี record ปี 67-69)...")
    active_plates = query(conn,
        "SELECT DISTINCT license FROM zzapp "
        "WHERE license IS NOT NULL AND license <> '' "
        "AND YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")
    plates_set = {(r["license"] or "").strip() for r in active_plates if r["license"]}
    plates_set.discard("")
    print(f"  Unique active plates: {len(plates_set):,}")

    active_names = query(conn,
        "SELECT DISTINCT namethai FROM zzapp "
        "WHERE namethai IS NOT NULL AND namethai <> '' "
        "AND YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026")
    names_set = {(r["namethai"] or "").strip() for r in active_names if r["namethai"]}
    names_set.discard("")
    print(f"  Unique active customer names: {len(names_set):,}")

    # 4. ดึง records ทั้งหมดของ active plates (ทุกปี)
    if plates_set:
        # split into chunks (SQL IN limit)
        all_for_active = 0
        chunk_size = 100
        plates = list(plates_set)
        for i in range(0, len(plates), chunk_size):
            chunk = plates[i:i+chunk_size]
            placeholders = ",".join(f"'{p.replace(chr(39), chr(39)*2)}'" for p in chunk)
            r = query(conn, f"SELECT COUNT(*) AS n FROM zzapp WHERE license IN ({placeholders})")
            all_for_active += r[0]["n"]
        print(f"\n  records ทั้งหมดของ active plates (ทุกปี): {all_for_active:,}")

    # 5. by policytype สำหรับ active records
    print(f"\n[4] Active records by policytype (ปี 67-69):")
    by_type = query(conn,
        "SELECT policytype, COUNT(*) AS n FROM zzapp "
        "WHERE YEAR(datestart) >= 2024 AND YEAR(datestart) <= 2026 "
        "GROUP BY policytype ORDER BY COUNT(*) DESC")
    for r in by_type:
        print(f"  {r['policytype'] or '(null)'}: {r['n']:,}")

    conn.Close()

    # ─── BabyScan folder ─────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"[5] สแกน BabyScan folder ({SCAN_DIR})...")
    if not SCAN_DIR.exists():
        print("  ❌ ไม่พบ folder")
        return

    pdfs = [p for p in SCAN_DIR.iterdir() if p.is_file() and p.suffix.lower() in (".pdf",".jpg",".jpeg",".xls",".xlsx")]
    print(f"  Total files: {len(pdfs):,}")
    total_size = sum(p.stat().st_size for p in pdfs)
    print(f"  Total size: {total_size/1024/1024/1024:.2f} GB")

    # parse ปี + plate จากชื่อไฟล์
    # ตัวอย่าง: "1กฎ4104 พรบ.67.pdf" → plate="1กฎ4104", year=67
    # ตัวอย่าง: "104-10ถนนผดุงกรุงเกษม กธ.65.pdf" → address="104-10ถนนผดุง...", year=65
    year_pat = re.compile(r'(\d{2})(?:\.\.\.|\.|_|\s|\))', re.UNICODE)
    by_year_files = Counter()
    plate_to_files = defaultdict(list)

    for p in pdfs:
        nm = p.stem
        # หาปี — เลขสองหลักท้ายๆ ที่อยู่หลัง "กธ" "พรบ" "PA" หรือ space
        m = re.search(r'(?:กธ|พรบ|PA|พรล)\.?(\d{2})(?:\D|$)', nm)
        if not m:
            # หา "ชื่อ NN.pdf" หรือ "ชื่อ NN ..."
            m = re.search(r'\s(\d{2})(?:\.|\s|$|_)', nm)
        if m:
            yr = int(m.group(1))
            by_year_files[yr] += 1
        # หา plate (ขึ้นต้นด้วยเลข + ตัวอักษรไทย + เลข)
        pm = re.match(r'^(\d{1,2}[ก-ฮ]{1,2}\d{1,5}(?:[ก-ฮ]{1,2})?)\s', nm)
        if pm:
            plate_to_files[pm.group(1)].append(p)

    print(f"\n  Files by ปี (พ.ศ.):")
    for yr in sorted(by_year_files.keys(), reverse=True)[:15]:
        mark = " ← ACTIVE" if yr in (67, 68, 69) else ""
        print(f"    พ.ศ. {yr}: {by_year_files[yr]:,}{mark}")

    active_pdfs_by_year = sum(by_year_files[y] for y in (67, 68, 69))
    print(f"\n  Files ปี 67-69: {active_pdfs_by_year:,}")

    print(f"\n  Unique plates ใน filenames: {len(plate_to_files):,}")

    # match: plates ใน BabyScan ที่ active ใน MDB
    matched_active_plates = set()
    for plate_db in plates_set:
        # MDB เก็บ '1กก 8803 กท' / BabyScan เก็บ '1กก8803'
        compact = plate_db.replace(" ", "")
        # ลอง match แบบ partial (filename plate มักไม่มี province)
        for plate_file in plate_to_files:
            if plate_file in compact or compact.startswith(plate_file):
                matched_active_plates.add(plate_file)
                break
    print(f"\n  Active plates ที่หา PDF ได้: {len(matched_active_plates):,} / {len(plates_set):,}")

    # นับไฟล์ของ active plates (ทุกปี)
    files_for_active = sum(len(plate_to_files[p]) for p in matched_active_plates)
    size_for_active = sum(f.stat().st_size for p in matched_active_plates for f in plate_to_files[p])
    print(f"  Files ทั้งหมดของ active plates (ทุกปี): {files_for_active:,}")
    print(f"  Size: {size_for_active/1024/1024:.1f} MB")

    # ─── สรุป ─────────────────────────────────────
    print(f"\n{'='*70}")
    print("  📊 สรุปก่อน import")
    print(f"{'='*70}")
    print(f"\n  MDB ใหม่ (Baby78_Safety.mdb):")
    print(f"     Total: {total:,} rows")
    print(f"     ปี 67-69 (active): {active_n:,} rows")
    print(f"     Active plates: {len(plates_set):,}")
    print(f"     Records ทั้งหมดของ active plates (ทุกปี): {all_for_active:,}")
    print(f"\n  BabyScan PDFs:")
    print(f"     Total files: {len(pdfs):,} ({total_size/1024/1024/1024:.2f} GB)")
    print(f"     Files ปี 67-69: {active_pdfs_by_year:,}")
    print(f"     Files ของ active plates (ทุกปี): {files_for_active:,} ({size_for_active/1024/1024:.1f} MB)")
    print(f"\n  Neon ตอนนี้ (จะถูกลบ):")
    print(f"     insurance_policies: 4,017 rows (ข้อมูลเก่า)")
    print(f"     R2: 16 PDFs")
    print(f"\n  ⚠️  รอ user confirm ก่อน DELETE Neon + IMPORT ใหม่")


if __name__ == "__main__":
    main()
