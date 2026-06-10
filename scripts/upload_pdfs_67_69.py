"""
upload_pdfs_67_69.py — match BabyScan files → Neon records → upload R2 + update DB
─────────────────────────────────────────────────────────────────────
สำหรับแต่ละ record ใน insurance_policies:
- หา PDF ที่ตรงใน BabyScan โดยใช้:
    - Motor/PRB: ทะเบียน (license_plate compact) + ปีคุ้มครอง (พ.ศ.)
    - Fire/Asset: ที่อยู่ + ปี
    - PA/TA: ชื่อ หรือ ที่อยู่ + ปี
- Upload ไปที่ R2 ด้วยชื่อตาม Baby78 convention
- Update pdf_url + pdf_filename ใน DB

Match priority:
1. Exact year match (พ.ศ. ของ coverage_end ตรงกับ year ในชื่อไฟล์)
2. ไม่มี year match → ใช้ไฟล์ที่ไม่ระบุปี (e.g. "1กก5226 กธ..pdf")
"""
import os, sys, re
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter

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


SCAN_DIR = Path(r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan")
# รองรับทั้งทะเบียนแบบใหม่ (1กก5226) และแบบเก่า (กท1101)
PLATE_RE = re.compile(r'^((?:\d{1,2})?[ก-ฮ]{1,2}\d{1,5})')


def be_year_2digit(s) -> str:
    if not s: return ""
    try:
        y = int(str(s)[:4])
        return f"{(y + 543) % 100:02d}"
    except: return ""


def plate_compact(p):
    if not p: return ""
    return re.sub(r'\s+', '', p)


def sanitize_fs(s, maxlen=80):
    if not s: return ""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(s))
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:maxlen]


def build_filename(row: dict, is_prb: bool = False) -> str:
    """ตั้งชื่อตาม Baby78 convention"""
    pt = (row.get("policy_type") or "").upper().strip()
    yr = be_year_2digit(row.get("coverage_end") or row.get("coverage_start"))
    yr_part = f".{yr}" if yr else ""

    if is_prb or pt == "P":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} พรบ{yr_part}.pdf"
    if pt == "M":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} กธ{yr_part}.pdf"
    if pt in ("FIRE", "IAR", "ASSET", "BURGLAR"):
        addr = sanitize_fs(row.get("insured_address"), 80) or "ไม่ระบุที่อยู่"
        return f"{addr} กธ{yr_part}.pdf"
    if pt in ("PA", "TA", "GOLF", "3RD", "PUBLIC"):
        label = sanitize_fs(row.get("insured_address") or row.get("insured_name"), 80) or "ไม่ระบุ"
        return f"{label} PA{yr_part}.pdf"
    label = sanitize_fs(row.get("insured_name") or row.get("license_plate"), 80) or "unknown"
    return f"{label} กธ{yr_part}.pdf"


def index_scan_dir():
    """ทำ index ของ BabyScan: plate → [(file, year_in_name, is_prb)]
       และ first_chars → [(file, year, is_prb)] สำหรับ address-based"""
    plate_idx = defaultdict(list)
    addr_idx = defaultdict(list)

    year_pat = re.compile(r'(?:กธ|พรบ|พรล|PA|พ\.ร\.บ)\.?\s*(\d{2})(?:\D|$)')
    alt_pat = re.compile(r'\s(\d{2})(?:\.|\s|$|_|\))')
    prb_pat = re.compile(r'พรบ|พรล|พ\.ร\.บ')

    for p in SCAN_DIR.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".jpg", ".jpeg"): continue
        nm = p.stem
        # year
        yr = None
        m = year_pat.search(nm)
        if not m: m = alt_pat.search(nm)
        if m:
            try: yr = int(m.group(1))
            except: pass
        is_prb = bool(prb_pat.search(nm))

        pm = PLATE_RE.match(nm)
        if pm:
            plate_idx[pm.group(1)].append((p, yr, is_prb))
        else:
            # address-based: ใช้ 40 chars แรก เป็น key
            key = nm.split(" ")[0][:40]
            addr_idx[key].append((p, yr, is_prb))
    return plate_idx, addr_idx


def find_pdf_for_record(row, plate_idx, addr_idx):
    """หา PDF ที่ match record นี้ คืน (file_path, is_prb_hint) หรือ None"""
    pt = (row.get("policy_type") or "").upper().strip()
    target_yr = None
    try:
        yr2 = be_year_2digit(row.get("coverage_end") or row.get("coverage_start"))
        if yr2: target_yr = int(yr2)
    except: pass

    candidates = []
    # plate-based (Motor, PRB)
    if pt in ("M", "P") or row.get("license_plate"):
        plate = plate_compact(row.get("license_plate"))
        if plate:
            # ลอง match กับ plate compact (อาจมีจังหวัด)
            for key in plate_idx:
                if key in plate or plate.startswith(key):
                    candidates.extend(plate_idx[key])
    # address-based (Fire, Asset)
    if not candidates and (pt in ("FIRE", "ASSET", "IAR", "BURGLAR")):
        addr = (row.get("insured_address") or "").strip()
        if addr:
            key = addr[:40].split(" ")[0]
            if key in addr_idx:
                candidates.extend(addr_idx[key])

    if not candidates:
        return None, False

    # filter by type (M needs non-PRB, P needs PRB)
    is_prb_record = (pt == "P")
    typed = [c for c in candidates if c[2] == is_prb_record]
    if not typed: typed = candidates

    # filter by year
    if target_yr:
        same_year = [c for c in typed if c[1] == target_yr]
        if same_year: return same_year[0][0], same_year[0][2]

    # fallback: ใช้ตัวที่ไม่มีปีระบุ (น่าจะเป็นเอกสารหลัก)
    no_year = [c for c in typed if c[1] is None]
    if no_year: return no_year[0][0], no_year[0][2]
    return typed[0][0], typed[0][2]


def get_r2():
    return boto3.client("s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"))


def main():
    print(f"\n{'='*65}")
    print(f"  UPLOAD PDFs — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}\n")

    print("[1] Index BabyScan folder...")
    plate_idx, addr_idx = index_scan_dir()
    print(f"  Unique plates: {len(plate_idx):,}")
    print(f"  Unique address keys: {len(addr_idx):,}")

    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    # เฉพาะ records ที่ยังไม่มี PDF (idempotent — รันซ้ำได้)
    cur.execute("""
        SELECT id, policy_number, policy_type, license_plate, insured_name,
               insured_address, coverage_start, coverage_end
        FROM insurance_policies
        WHERE pdf_url IS NULL
        ORDER BY coverage_end DESC NULLS LAST
    """)
    records = cur.fetchall()
    print(f"\n[2] Records ใน Neon: {len(records):,}")

    s3 = get_r2()
    bucket = os.getenv("R2_BUCKET")
    r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    stats = Counter()
    update_cur = conn.cursor()
    update_sql = ("UPDATE insurance_policies SET pdf_url=%s, pdf_filename=%s, pdf_size=%s "
                  "WHERE id=%s")

    print(f"\n[3] Match + Upload...")
    for i, row in enumerate(records, 1):
        d = dict(row)
        pdf_path, is_prb = find_pdf_for_record(d, plate_idx, addr_idx)
        if not pdf_path:
            stats["no_match"] += 1
            continue

        fname = build_filename(d, is_prb=is_prb)
        # R2 key: policies/{id_prefix}/{filename} (ใช้ id เพื่อไม่ชน)
        key = f"policies/{str(d['id'])[:8]}_{fname}"
        try:
            data = pdf_path.read_bytes()
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
            pdf_url = f"{r2_pub}/{key}"
            update_cur.execute(update_sql, (pdf_url, fname, len(data), d['id']))
            stats["uploaded"] += 1
            if i % 100 == 0:
                conn.commit()
                print(f"    progress: {i:,}/{len(records):,} uploaded={stats['uploaded']:,} no_match={stats['no_match']:,}")
        except Exception as e:
            stats["error"] += 1
            print(f"    ⚠️  {fname}: {e}")

    conn.commit()
    conn.close()

    print(f"\n{'='*65}")
    print("สรุป:")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v:,}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
