"""
upload_pdfs_v2.py — รอบ 2: จัดการ address-based + name-based matching ที่ตกหล่นจาก v1
─────────────────────────────────────────────────────────────────────
สำหรับ records ที่ยังไม่มี pdf_url:
- ใช้ address1 (จาก MDB CSV) — ลบ space/normalize → fuzzy match กับ filename
- ลอง namethai เผื่อ PA ใช้ชื่อ
- จัด priority: exact-year > no-year > closest-year

ใช้ key:
- map app_number → {address1, address2, namethai}
"""
import os, sys, re, csv
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
ADDR_MAP = Path(r"D:\tmp\addr_map_67_69.csv")

# Strip from filename to get "key" — remove year markers, suffix words
SUFFIX_PAT = re.compile(
    r'\s*(?:กธ|พรบ|พรล|พ\.ร\.บ|PA|ภ\.ค|ภค|ทอพ|FF|PL|HF)\.?\s*\d{0,2}.*$'
    r'|\s*\(?(?:ยกเลิก|สลักหลัง|แนบ|ใหม่|แก้ไข)[^)]*\)?.*$'
    r'|\s+\d{2}(?:[\s.\(_].*)?$'
)


def normalize_addr(s):
    """ลบ space, แทนที่ / ด้วย -, lowercase"""
    if not s: return ""
    s = re.sub(r'[\s\-/.,()]+', '', str(s))
    return s


def filename_key(stem):
    """ดึง 'address/identifier key' จาก stem โดยตัดส่วนปี/หมวด/ noise ออก"""
    # ตัด suffix แบบ "กธ.65", "พรบ.66", "_0001", "(ยกเลิก)", etc.
    s = SUFFIX_PAT.sub('', stem).strip()
    return normalize_addr(s)


def be_year(s):
    if not s: return None
    try:
        y = int(str(s)[:4])
        return (y + 543) % 100
    except: return None


def year_in_name(stem):
    m = re.search(r'(?:กธ|พรบ|พรล|PA|พ\.ร\.บ)\.?\s*(\d{2})(?:\D|$)', stem)
    if not m: m = re.search(r'\s(\d{2})(?:\.|\s|$|_|\))', stem)
    if m:
        try: return int(m.group(1))
        except: pass
    return None


def is_prb_file(stem):
    return bool(re.search(r'พรบ|พรล|พ\.ร\.บ', stem))


def sanitize_fs(s, maxlen=80):
    if not s: return ""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(s))
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:maxlen]


def plate_compact(p):
    if not p: return ""
    return re.sub(r'\s+', '', p)


def build_filename(row: dict, is_prb=False) -> str:
    pt = (row.get("policy_type") or "").upper().strip()
    yr = be_year(row.get("coverage_end") or row.get("coverage_start"))
    yr_part = f".{yr:02d}" if yr is not None else ""

    if is_prb or pt == "P":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} พรบ{yr_part}.pdf"
    if pt == "M":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} กธ{yr_part}.pdf"
    if pt in ("FIRE", "IAR", "ASSET", "BURGLAR"):
        addr = sanitize_fs(row.get("address1"), 80) or "ไม่ระบุที่อยู่"
        return f"{addr} กธ{yr_part}.pdf"
    if pt in ("PA", "TA", "GOLF", "3RD", "PUBLIC"):
        label = sanitize_fs(row.get("address1") or row.get("namethai"), 80) or "ไม่ระบุ"
        return f"{label} PA{yr_part}.pdf"
    label = sanitize_fs(row.get("namethai") or row.get("license_plate"), 80) or "unknown"
    return f"{label} กธ{yr_part}.pdf"


def main():
    print(f"\n{'='*65}")
    print(f"  UPLOAD PDFs v2 (address/name) — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}\n")

    # Load addr map from MDB CSV
    print("[1] Load address map จาก MDB...")
    addr_map = {}  # key: app_number → {address1, namethai, ...}
    with ADDR_MAP.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            app = (r.get("app") or "").strip()
            if app:
                addr_map[app] = r
    print(f"  loaded {len(addr_map):,} addr records")

    # Index BabyScan: file_key → [(path, year, is_prb)]
    print("\n[2] Index BabyScan ด้วย normalized key...")
    files_by_key = defaultdict(list)  # normalized_key → list
    all_keys = []  # for substring search
    n_files = 0
    for p in SCAN_DIR.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".jpg", ".jpeg"): continue
        stem = p.stem
        key = filename_key(stem)
        if not key: continue
        yr = year_in_name(stem)
        is_prb = is_prb_file(stem)
        files_by_key[key].append((p, yr, is_prb))
        all_keys.append(key)
        n_files += 1
    print(f"  indexed {n_files:,} files, {len(files_by_key):,} unique keys")

    # Query Neon for records without PDF
    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, app_number, policy_number, policy_type, license_plate,
               insured_name, insured_address, coverage_start, coverage_end
        FROM insurance_policies
        WHERE pdf_url IS NULL
    """)
    records = cur.fetchall()
    print(f"\n[3] Records ที่ยังไม่มี PDF: {len(records):,}")

    s3 = boto3.client("s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"))
    bucket = os.getenv("R2_BUCKET")
    r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    stats = Counter()
    upd_cur = conn.cursor()
    upd_sql = "UPDATE insurance_policies SET pdf_url=%s, pdf_filename=%s, pdf_size=%s WHERE id=%s"

    print(f"\n[4] Match + Upload...")
    for i, row in enumerate(records, 1):
        d = dict(row)
        app = (d.get("app_number") or "").strip()
        mdb = addr_map.get(app, {})
        addr1 = (mdb.get("address1") or "").strip()
        name = (mdb.get("namethai") or "").strip()
        d["address1"] = addr1
        d["namethai"] = name

        target_yr = be_year(d.get("coverage_end") or d.get("coverage_start"))
        pt = (d.get("policy_type") or "").upper().strip()
        is_prb_expected = (pt == "P")

        # ลำดับ key ที่จะลอง
        candidates_keys = []
        if addr1:
            candidates_keys.append(normalize_addr(addr1))
            # ลอง 20 chars แรก เผื่อ address ใน file สั้นกว่า
            n_addr = normalize_addr(addr1)
            if len(n_addr) > 20: candidates_keys.append(n_addr[:20])
            if len(n_addr) > 15: candidates_keys.append(n_addr[:15])
        if name:
            candidates_keys.append(normalize_addr(name))

        # ลองหา match
        all_candidates = []  # list of (path, year, is_prb)
        for ck in candidates_keys:
            if not ck: continue
            # exact key match
            if ck in files_by_key:
                all_candidates.extend(files_by_key[ck])
            # prefix match — key ในไฟล์ขึ้นต้นด้วย ck
            for fk in files_by_key:
                if fk.startswith(ck) and fk != ck:
                    all_candidates.extend(files_by_key[fk])
                elif ck.startswith(fk) and len(fk) >= 8:
                    # หรือ ck ขึ้นต้นด้วย fk (file key สั้นกว่า)
                    all_candidates.extend(files_by_key[fk])
            if all_candidates: break

        if not all_candidates:
            stats["no_match"] += 1
            continue

        # filter by type (PRB vs non-PRB)
        typed = [c for c in all_candidates if c[2] == is_prb_expected]
        if not typed: typed = all_candidates

        # filter by exact year, else no-year file, else first
        chosen = None
        if target_yr is not None:
            same_yr = [c for c in typed if c[1] == target_yr]
            if same_yr: chosen = same_yr[0]
        if not chosen:
            no_yr = [c for c in typed if c[1] is None]
            if no_yr: chosen = no_yr[0]
        if not chosen:
            chosen = typed[0]

        pdf_path, _, is_prb_f = chosen
        fname = build_filename(d, is_prb=(is_prb_f or is_prb_expected))
        # R2 key = pure Thai name (no UUID prefix) — ตรงกับ Baby78 logic
        # ถ้าชื่อซ้ำ → เติม _0001, _0002 (เหมือน Baby78)
        base_key = f"policies/{fname}"
        key = base_key
        suffix_n = 0
        while True:
            try:
                s3.head_object(Bucket=bucket, Key=key)
                # exists already — try next suffix
                suffix_n += 1
                stem, ext = fname.rsplit(".", 1)
                key = f"policies/{stem}_{suffix_n:04d}.{ext}"
                if suffix_n > 99: break  # safety
            except Exception:
                break  # not exists → can use this key

        try:
            data = pdf_path.read_bytes()
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
            # URL encode key for R2 public URL (handles Thai)
            from urllib.parse import quote
            pdf_url = f"{r2_pub}/{quote(key)}"
            final_fname = key.split("/", 1)[1]
            upd_cur.execute(upd_sql, (pdf_url, final_fname, len(data), d['id']))
            stats["uploaded"] += 1
            stats[f"type_{pt or 'unk'}"] += 1
            if i % 100 == 0:
                conn.commit()
                print(f"    {i:,}/{len(records):,} uploaded={stats['uploaded']:,} no_match={stats['no_match']:,}")
        except Exception as e:
            stats["error"] += 1
            print(f"    ⚠️  {fname}: {e}")

    conn.commit()
    conn.close()

    print(f"\n{'='*65}")
    print("สรุป v2:")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v:,}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
