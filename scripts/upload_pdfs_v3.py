"""
upload_pdfs_v3.py — STRICT match + pure Thai R2 keys (Baby78 logic 100%)
─────────────────────────────────────────────────────────────────────
หลักการ:
1. ลบ R2 ทั้งหมด + reset pdf_url ใน DB
2. สำหรับแต่ละ record:
   - Build expected filename จาก policy_type + license_plate/address/name + year
   - หาในโฟลเดอร์ BabyScan ที่ใกล้เคียง (strict — ไม่เดา):
     - Motor/PRB: plate + suffix กธ/พรบ + year
     - Fire/Asset: address (normalized) + suffix กธ + year
     - PA: address หรือ name + suffix PA + year
   - ถ้าไม่เจอ → pdf_url=NULL (ไม่ใส่ผิด)
3. Upload ด้วย pure Thai key: policies/{filename} (เติม _0001 ถ้าซ้ำ)
"""
import os, sys, re, csv, argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
from urllib.parse import quote

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


def be_year(s):
    if not s: return None
    try:
        y = int(str(s)[:4])
        return (y + 543) % 100
    except: return None


def plate_compact(p):
    if not p: return ""
    return re.sub(r'\s+', '', p).strip()


def addr_normalize(s):
    """normalize address — ลบ space, slash, dot, dash"""
    if not s: return ""
    return re.sub(r'[\s\-/.,()]+', '', str(s)).strip()


def sanitize_fs(s, maxlen=80):
    if not s: return ""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', str(s))
    s = re.sub(r'\s+', ' ', s).strip()
    return s[:maxlen]


def build_baby78_filename(d: dict, is_prb=False) -> str:
    """Baby78 naming convention เป๊ะๆ"""
    pt = (d.get("policy_type") or "").upper().strip()
    yr = be_year(d.get("coverage_end") or d.get("coverage_start"))
    yr_part = f".{yr:02d}" if yr is not None else ""

    if is_prb or pt == "P":
        plate = plate_compact(d.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} พรบ{yr_part}.pdf"
    if pt == "M":
        plate = plate_compact(d.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} กธ{yr_part}.pdf"
    if pt in ("FIRE", "IAR", "ASSET", "BURGLAR"):
        addr = sanitize_fs(d.get("address1"), 80) or "ไม่ระบุที่อยู่"
        return f"{addr} กธ{yr_part}.pdf"
    if pt in ("PA", "TA", "GOLF", "3RD", "PUBLIC"):
        label = sanitize_fs(d.get("address1") or d.get("namethai"), 80) or "ไม่ระบุ"
        return f"{label} PA{yr_part}.pdf"
    # default: ใช้ชื่อ
    label = sanitize_fs(d.get("namethai") or d.get("license_plate") or "unknown", 80)
    return f"{label} กธ{yr_part}.pdf"


# ──────────────────────────────────────────────────────────────
#   Index ของ BabyScan
# ──────────────────────────────────────────────────────────────
def build_scan_index():
    """โครงสร้าง:
       plate_idx[plate_compact] = [(path, year, is_prb, has_special_suffix)]
       addr_idx[normalized_addr_prefix] = [(path, year, is_prb)]
       name_idx[normalized_name_prefix] = [(path, year, is_prb)]
    """
    plate_pat = re.compile(r'^((?:\d{1,2})?[ก-ฮ]{1,2}\d{1,5})')
    year_pat = re.compile(r'(?:กธ|พรบ|พรล|PA|พ\.ร\.บ|ภ\.ค)\.?\s*(\d{2})(?:\D|$)')
    alt_year_pat = re.compile(r'\s(\d{2})(?:\.|\s|$|_|\))')
    prb_pat = re.compile(r'พรบ|พรล|พ\.ร\.บ')

    plate_idx = defaultdict(list)
    addr_idx = defaultdict(list)

    for p in SCAN_DIR.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".jpg", ".jpeg"):
            continue
        stem = p.stem

        # year
        yr = None
        m = year_pat.search(stem)
        if not m: m = alt_year_pat.search(stem)
        if m:
            try: yr = int(m.group(1))
            except: pass

        is_prb = bool(prb_pat.search(stem))

        # Skip ไฟล์ที่มี indicator พิเศษ (ยกเลิก, สลักหลัง, แนบ, etc.) — เก็บไว้แต่ deprioritize
        is_special = bool(re.search(r'ยกเลิก|สลักหลัง|แนบ|บัตรเครดิต|บัตรประชาชน|ทะเบียน|ใบขับขี่|ใบเตือน|เคลม', stem))

        # plate-based
        pm = plate_pat.match(stem)
        if pm:
            plate_idx[pm.group(1)].append((p, yr, is_prb, is_special))
        else:
            # address-based: ใช้ normalized prefix (10 chars)
            # ตัด suffix " กธ.NN" / " พรบ.NN" / " NN" ออกก่อน normalize
            base = re.sub(r'\s+(?:กธ|พรบ|พรล|PA)\..*$|\s+\d{2}.*$|\s+\(.*$', '', stem).strip()
            norm = addr_normalize(base)
            if norm:
                # store multiple key lengths สำหรับ flexible match
                addr_idx[norm].append((p, yr, is_prb, is_special))
                if len(norm) > 12:
                    addr_idx[norm[:12]].append((p, yr, is_prb, is_special))
    return plate_idx, addr_idx


def find_pdf(d: dict, plate_idx, addr_idx):
    """Strict matcher — คืน path หรือ None"""
    pt = (d.get("policy_type") or "").upper().strip()
    target_yr = be_year(d.get("coverage_end") or d.get("coverage_start"))
    expect_prb = (pt == "P")

    candidates = []

    # 1. Plate-based (Motor, PRB)
    if pt in ("M", "P", "p"):
        plate = plate_compact(d.get("license_plate"))
        if plate:
            for k in plate_idx:
                # k = "1กก8803" → match กับ plate "1กก8803กท" (มี province)
                # OR plate = "ฌค8463" และ k = "ฌค8463"
                if k == plate or k in plate or (plate.startswith(k) and len(k) >= 5):
                    candidates.extend(plate_idx[k])

    # 2. Address-based (Fire/Asset/IAR/PA with address)
    if not candidates and pt in ("FIRE", "ASSET", "IAR", "BURGLAR", "PA", "TA"):
        addr1 = (d.get("address1") or "").strip()
        if addr1:
            norm = addr_normalize(addr1)
            if norm:
                # exact match
                if norm in addr_idx:
                    candidates.extend(addr_idx[norm])
                # prefix match — ลอง 15, 12 chars แรก
                if not candidates and len(norm) >= 15:
                    for k in addr_idx:
                        if k == norm[:15] or k == norm[:12]:
                            candidates.extend(addr_idx[k])
                # หรือ key ใน addr_idx ที่ขึ้นต้นด้วย norm
                if not candidates:
                    for k, files in addr_idx.items():
                        if len(k) >= 10 and (k.startswith(norm[:10]) or norm.startswith(k)):
                            candidates.extend(files)
                            break  # first hit only — ไม่เดา

    if not candidates:
        return None, expect_prb

    # filter: prefer non-special files
    non_special = [c for c in candidates if not c[3]]
    if non_special: candidates = non_special

    # filter: type match (PRB vs non-PRB)
    typed = [c for c in candidates if c[2] == expect_prb]
    if not typed: typed = candidates

    # filter: exact year
    if target_yr is not None:
        same_yr = [c for c in typed if c[1] == target_yr]
        if same_yr: return same_yr[0][0], expect_prb

    # fallback: no-year file
    no_yr = [c for c in typed if c[1] is None]
    if no_yr: return no_yr[0][0], expect_prb

    # fallback: first candidate
    return typed[0][0], expect_prb


# ──────────────────────────────────────────────────────────────
#   Main
# ──────────────────────────────────────────────────────────────
def get_r2():
    return boto3.client("s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-clear", action="store_true", help="ไม่ล้าง R2 + pdf_url ก่อน")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  UPLOAD v3 STRICT — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}\n")

    # Load addr map
    print("[1] Load address map จาก MDB...")
    addr_map = {}
    if ADDR_MAP.exists():
        with ADDR_MAP.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                app = (r.get("app") or "").strip()
                if app: addr_map[app] = r
    print(f"  loaded {len(addr_map):,} records")

    s3 = get_r2()
    bucket = os.getenv("R2_BUCKET")
    r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)

    if not args.no_clear:
        # Clear R2
        print("\n[2a] ล้าง R2 ของเก่า...")
        paginator = s3.get_paginator("list_objects_v2")
        n_del = 0
        for page in paginator.paginate(Bucket=bucket):
            objs = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if objs:
                s3.delete_objects(Bucket=bucket, Delete={"Objects": objs})
                n_del += len(objs)
        print(f"  ลบ {n_del:,} ไฟล์")

        # Clear pdf_url ใน DB
        print("[2b] reset pdf_url ใน DB...")
        c = conn.cursor()
        c.execute("UPDATE insurance_policies SET pdf_url=NULL, pdf_filename=NULL, pdf_size=NULL")
        conn.commit()
        print(f"  reset {c.rowcount:,} rows")

    print("\n[3] Index BabyScan...")
    plate_idx, addr_idx = build_scan_index()
    n_files = sum(len(v) for v in plate_idx.values()) + sum(len(v) for v in addr_idx.values())
    print(f"  plates: {len(plate_idx):,}, addr keys: {len(addr_idx):,}, total entries: {n_files:,}")

    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, app_number, policy_number, policy_type, license_plate,
               insured_name, insured_address, coverage_start, coverage_end
        FROM insurance_policies
    """)
    records = cur.fetchall()
    print(f"\n[4] Match + Upload — total {len(records):,} records")

    upd_cur = conn.cursor()
    upd_sql = "UPDATE insurance_policies SET pdf_url=%s, pdf_filename=%s, pdf_size=%s WHERE id=%s"

    stats = Counter()
    seen_keys = set()  # track collisions in this run
    for i, row in enumerate(records, 1):
        d = dict(row)
        mdb = addr_map.get((d.get("app_number") or "").strip(), {})
        d["address1"] = (mdb.get("address1") or "").strip()
        d["namethai"] = (mdb.get("namethai") or "").strip()

        pdf_path, is_prb_hint = find_pdf(d, plate_idx, addr_idx)
        if not pdf_path:
            stats["no_match"] += 1
            stats[f"miss_{(d.get('policy_type') or 'unk').upper()}"] += 1
            continue

        fname = build_baby78_filename(d, is_prb=is_prb_hint)
        # handle collision
        base_key = f"policies/{fname}"
        key = base_key
        n = 0
        while key in seen_keys:
            n += 1
            stem, ext = fname.rsplit(".", 1)
            key = f"policies/{stem}_{n:04d}.{ext}"
        seen_keys.add(key)

        try:
            data = pdf_path.read_bytes()
            s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
            pdf_url = f"{r2_pub}/{quote(key)}"
            final_fname = key.split("/", 1)[1]
            upd_cur.execute(upd_sql, (pdf_url, final_fname, len(data), d['id']))
            stats["uploaded"] += 1
            if i % 100 == 0:
                conn.commit()
                print(f"    {i:,}/{len(records):,} uploaded={stats['uploaded']:,} no_match={stats['no_match']:,}")
        except Exception as e:
            stats["error"] += 1
            print(f"    ⚠️  {fname}: {str(e)[:100]}")

    conn.commit()
    conn.close()

    print(f"\n{'='*65}")
    print("สรุป v3 strict:")
    for k, v in sorted(stats.items()):
        print(f"  {k}: {v:,}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
