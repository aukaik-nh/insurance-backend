"""
backup_pdfs.py
─────────────────────────────────────────────────────────────────────
Backup PDF ทั้งหมด → จัดชื่อตามประเภทประกัน → save ลงโฟลเดอร์ที่เปิดดูได้
- ดึง PDF จาก R2 (PDFs ที่ผูกกับ DB แล้ว)
- mirror โฟลเดอร์ BabyPreechar (PDFs เก่าที่ยังไม่ได้ link)
- ตั้งชื่อตามกฎ:
    M (รถยนต์)  → {ทะเบียน}_กธ{เลขกธ}_{ปีพ.ศ.}.pdf
    P (พรบ.)    → {ทะเบียน}_พรบ_{ปีพ.ศ.}.pdf
    FIRE        → {ที่อยู่ย่อ}_{ปีพ.ศ.}.pdf
    PA/TA       → {ชื่อผู้เอาประกัน}_{ปีพ.ศ.}.pdf
- แยกโฟลเดอร์ตามประเภท: motor/, prb/, fire/, pa_ta/, other/, unmatched/
- daily idempotent: ถ้ามีไฟล์ชื่อซ้ำขนาดเท่ากัน → ข้าม, ขนาดต่าง → ทับด้วยอันใหม่
- สร้าง manifest.csv → policy_id ↔ filename ↔ source

USAGE:
    python scripts/backup_pdfs.py                         # backup ทั้งหมด
    python scripts/backup_pdfs.py --skip-babypreechar    # ข้าม local mirror
    python scripts/backup_pdfs.py --limit 100             # ทดสอบ 100 records
"""
import os, sys, re, csv, shutil, argparse
from pathlib import Path
from datetime import datetime
from collections import Counter

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


BACKUP_ROOT = Path(__file__).resolve().parent.parent / "backups" / "pdfs"
BABYPREECHAR_SRC = Path(r"C:\Users\Administrator\Desktop\New folder\BabyPreechar")

# ─────────────────────────────────────────────────────────────
#   Helpers
# ─────────────────────────────────────────────────────────────
ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
MULTISPACE = re.compile(r'\s+')


def sanitize(s: str | None, maxlen: int = 80) -> str:
    if not s: return ""
    s = ILLEGAL_FS.sub("", str(s))
    s = MULTISPACE.sub(" ", s).strip()
    return s[:maxlen]


def buddhist_year_2digit(coverage_end) -> str:
    """ดึงปี พ.ศ. 2 หลัก จาก coverage_end (YYYY-MM-DD หรือ datetime)"""
    if not coverage_end: return ""
    try:
        if isinstance(coverage_end, str):
            y = int(coverage_end[:4])
        else:
            y = coverage_end.year
        return f"{(y + 543) % 100:02d}"
    except Exception:
        return ""


def policy_type_folder(pt: str | None) -> tuple[str, str]:
    """แปลง policy_type → (folder, type_label_thai)"""
    pt = (pt or "").upper().strip()
    if pt == "M":     return ("motor", "ประกันรถยนต์")
    if pt == "P":     return ("prb", "ประกันพรบ")
    if pt == "FIRE":  return ("fire", "อัคคีภัย")
    if pt in ("PA","TA","GOLF"):  return ("pa_ta", "PA/TA")
    if pt in ("IAR","ASSET"):     return ("asset", "ทรัพย์สิน")
    if pt == "MARINE":            return ("marine", "ขนส่ง")
    if pt == "โจรกรรม":            return ("theft", "โจรกรรม")
    return ("other", pt or "ไม่ระบุ")


def plate_compact(plate: str | None) -> str:
    """ลบช่องว่างออกจากทะเบียน (ตามรูปแบบ Baby78: '1กก 8803 กท' → '1กก8803กท')"""
    if not plate: return ""
    return sanitize(plate.replace(" ", ""), 40)


def filename_for_policy(row: dict, is_prb: bool = False) -> str:
    """ตั้งชื่อไฟล์ตาม Baby78 convention:
        Motor:  {plate} กธ.{yy}.pdf
        PRB:    {plate} พรบ.{yy}.pdf
        Fire:   {address} กธ.{yy}.pdf
        PA/TA:  {name หรือ address} PA.{yy}.pdf
    หมายเหตุ: is_prb=True override → ใช้ format พรบ. (สำหรับ attachments doc_type=prb)
    """
    pt = (row.get("policy_type") or "").upper().strip()
    yr = buddhist_year_2digit(row.get("coverage_end") or row.get("coverage_start"))
    yr_part = f".{yr}" if yr else ""

    # PRB: attachments ที่เป็น พ.ร.บ. หรือ policy_type=P
    if is_prb or pt == "P":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} พรบ{yr_part}.pdf"

    if pt == "M":
        plate = plate_compact(row.get("license_plate")) or "ไม่ระบุทะเบียน"
        return f"{plate} กธ{yr_part}.pdf"

    if pt in ("FIRE", "IAR", "ASSET"):
        addr = sanitize(row.get("insured_address"), 80) or "ไม่ระบุที่อยู่"
        return f"{addr} กธ{yr_part}.pdf"

    if pt in ("PA", "TA", "GOLF"):
        # PA/TA: ใช้ที่อยู่ก่อนถ้ามี ไม่งั้นใช้ชื่อ (ตามตัวอย่าง '1047ถนนสีลม PA.56.pdf')
        label = sanitize(row.get("insured_address") or row.get("insured_name"), 80) or "ไม่ระบุ"
        return f"{label} PA{yr_part}.pdf"

    # default — ใช้ชื่อ + กธ.
    label = sanitize(row.get("insured_name") or row.get("license_plate") or row.get("policy_number"), 80) or "unknown"
    return f"{label} กธ{yr_part}.pdf"


def safe_write(dest: Path, data: bytes) -> str:
    """เขียนไฟล์ idempotent — ถ้ามีอยู่ขนาดเท่ากัน=ข้าม, ขนาดต่าง=ทับ"""
    if dest.exists() and dest.stat().st_size == len(data):
        return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return "write"


def safe_copy(src: Path, dest: Path) -> str:
    """copy ไฟล์ idempotent ตาม mtime + size"""
    if dest.exists():
        s_stat, d_stat = src.stat(), dest.stat()
        if s_stat.st_size == d_stat.st_size and abs(s_stat.st_mtime - d_stat.st_mtime) < 2:
            return "skip"
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return "copy"


def resolve_path(folder: Path, fname: str, data_size: int) -> tuple[Path, str]:
    """หา path ที่จะเขียน:
    - ถ้าชื่อหลักว่าง → ใช้ชื่อหลัก, action=write
    - ถ้าชื่อหลักมีไฟล์ size เท่ากัน → ใช้ชื่อหลัก, action=skip
    - ถ้าชื่อหลักมีไฟล์ size ต่าง → ทับ (user สั่ง "ซ้ำเอาอันใหม่"), action=overwrite
    """
    base = folder / fname
    if not base.exists():
        return base, "write"
    if base.stat().st_size == data_size:
        return base, "skip"
    return base, "overwrite"  # user ต้องการให้ทับ daily


# ─────────────────────────────────────────────────────────────
#   1. Download from R2 → rename per policy type
# ─────────────────────────────────────────────────────────────
def get_r2_client():
    return boto3.client(
        "s3",
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version="s3v4"),
    )


def backup_from_r2(limit: int | None) -> tuple[Counter, list[dict]]:
    """ดึง PDF ที่ผูกกับ DB → save ในโฟลเดอร์ตาม type
    คืน (stats, manifest_rows)"""
    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    sql = """
        SELECT id, policy_number, policy_type, license_plate,
               insured_name, insured_address, coverage_start, coverage_end,
               pdf_url, pdf_filename, company_code
        FROM insurance_policies
        WHERE pdf_url IS NOT NULL
        ORDER BY coverage_end DESC NULLS LAST
    """
    if limit: sql += f" LIMIT {int(limit)}"
    cur.execute(sql)
    rows = cur.fetchall()

    # attachments — มี policy_id, doc_type
    cur.execute("""
        SELECT a.id, a.policy_id, a.doc_type, a.pdf_url, a.pdf_filename,
               p.policy_type, p.license_plate, p.insured_name, p.insured_address,
               p.coverage_start, p.coverage_end, p.policy_number, p.company_code
        FROM policy_attachments a JOIN insurance_policies p ON p.id = a.policy_id
        WHERE a.pdf_url IS NOT NULL
    """)
    att_rows = cur.fetchall()
    conn.close()

    bucket = os.getenv("R2_BUCKET")
    r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
    s3 = get_r2_client()

    stats = Counter()
    manifest = []

    def process(row, is_attachment=False):
        url = row["pdf_url"]
        # หา key ใน R2
        if r2_pub and url.startswith(r2_pub):
            key = url[len(r2_pub):].lstrip("/")
        elif "/r2.dev/" in url:
            key = url.split("/r2.dev/", 1)[1].split("/", 1)[1]
        else:
            stats["non_r2_skip"] += 1
            return

        # ถ้า attachment เป็น พ.ร.บ. → ใช้ format พรบ
        is_prb_attachment = bool(is_attachment and row.get("doc_type") == "prb")
        policy_view = row

        folder, label = policy_type_folder("P" if is_prb_attachment else policy_view.get("policy_type"))
        fname = filename_for_policy(policy_view, is_prb=is_prb_attachment)
        dest_folder = BACKUP_ROOT / folder

        try:
            obj = s3.get_object(Bucket=bucket, Key=key)
            data = obj["Body"].read()
            dest, action = resolve_path(dest_folder, fname, len(data))
            if action != "skip":
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(data)
            stats[f"r2_{action}"] += 1
            stats[f"type_{folder}"] += 1
            manifest.append({
                "source": "r2",
                "table": "attachments" if is_attachment else "policies",
                "policy_id": str(row.get("policy_id") or row.get("id")),
                "policy_number": row.get("policy_number"),
                "policy_type": policy_view.get("policy_type"),
                "type_folder": folder,
                "filename": dest.name,
                "filepath": str(dest.relative_to(BACKUP_ROOT)),
                "original_filename": row.get("pdf_filename"),
                "r2_key": key,
                "size": len(data),
            })
        except Exception as e:
            print(f"  ⚠️  {key}: {e}")
            stats["r2_error"] += 1

    print(f"\n[1] Download PDFs จาก R2 ({len(rows) + len(att_rows)} records)...")
    for r in rows:
        process(r, is_attachment=False)
    for r in att_rows:
        process(r, is_attachment=True)
    return stats, manifest


# ─────────────────────────────────────────────────────────────
#   2. Mirror BabyPreechar folder (เก็บไว้สำหรับ match ภายหลัง)
# ─────────────────────────────────────────────────────────────
def mirror_babypreechar(limit: int | None) -> tuple[Counter, list[dict]]:
    """copy ไฟล์จาก BabyPreechar → backups/pdfs/unmatched/ (preserve original Thai name)"""
    stats = Counter()
    manifest = []
    dest_folder = BACKUP_ROOT / "unmatched"
    dest_folder.mkdir(parents=True, exist_ok=True)

    if not BABYPREECHAR_SRC.exists():
        print(f"\n[2] BabyPreechar ไม่พบ — ข้าม")
        return stats, manifest

    files = [p for p in BABYPREECHAR_SRC.iterdir() if p.is_file() and p.suffix.lower() == ".pdf"]
    if limit: files = files[:limit]
    print(f"\n[2] Mirror BabyPreechar ({len(files):,} files)...")

    for i, src in enumerate(files, 1):
        dest = dest_folder / src.name
        try:
            action = safe_copy(src, dest)
            stats[f"local_{action}"] += 1
            if action == "copy":
                manifest.append({
                    "source": "babypreechar",
                    "table": "",
                    "policy_id": "",
                    "policy_number": "",
                    "policy_type": "",
                    "type_folder": "unmatched",
                    "filename": dest.name,
                    "filepath": str(dest.relative_to(BACKUP_ROOT)),
                    "original_filename": src.name,
                    "r2_key": "",
                    "size": dest.stat().st_size,
                })
            if i % 500 == 0:
                print(f"    progress: {i:,}/{len(files):,} ({stats['local_copy']} copy, {stats['local_skip']} skip)")
        except Exception as e:
            print(f"  ⚠️  {src.name}: {e}")
            stats["local_error"] += 1
    return stats, manifest


# ─────────────────────────────────────────────────────────────
#   3. Write manifest
# ─────────────────────────────────────────────────────────────
def write_manifest(rows: list[dict]):
    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    path = BACKUP_ROOT / "manifest.csv"
    if not rows:
        return path
    fields = ["source","table","policy_id","policy_number","policy_type","type_folder",
              "filename","filepath","original_filename","r2_key","size"]
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)
    return path


# ─────────────────────────────────────────────────────────────
#   Main
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-babypreechar", action="store_true")
    ap.add_argument("--skip-r2", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  backup_pdfs.py — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  Backup root: {BACKUP_ROOT}")
    print(f"{'='*65}")

    BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
    manifest: list[dict] = []
    total_stats = Counter()

    if not args.skip_r2:
        stats, m = backup_from_r2(args.limit)
        manifest.extend(m)
        total_stats.update(stats)

    if not args.skip_babypreechar:
        stats, m = mirror_babypreechar(args.limit)
        manifest.extend(m)
        total_stats.update(stats)

    mpath = write_manifest(manifest)

    print(f"\n{'='*65}")
    print("สรุป:")
    for k, v in sorted(total_stats.items()):
        print(f"  {k}: {v:,}")
    print(f"\n  Manifest: {mpath} ({len(manifest):,} entries)")
    print(f"  Folder: {BACKUP_ROOT}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
