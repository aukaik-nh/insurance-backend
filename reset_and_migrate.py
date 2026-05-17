"""
reset_and_migrate.py
─────────────────────────────────────────────────────────────────────
Full reset + import + PDF migration ในตัวเดียว

ขั้นตอน:
  1. Backup DB ปัจจุบัน → backups/ (local file)
  2. Re-export CSV จาก Baby78 .mdb (run VBS)
  3. TRUNCATE insurance_policies (ลบ records ทั้งหมด)
  4. Clear Supabase Storage bucket policy-pdfs/policies/
  5. Import CSV → Supabase (4,008 records)
  6. รัน PDF migration --upload-all (10,336 ไฟล์)
  7. (Optional) DELETE records ที่ไม่มี pdf_url

USAGE:
    python reset_and_migrate.py                  # รันทุกขั้น
    python reset_and_migrate.py --skip-pdf       # ข้ามขั้น 6 (PDF migration)
    python reset_and_migrate.py --keep-no-pdf    # ข้ามขั้น 7 (เก็บ records ที่ไม่มี PDF)
    python reset_and_migrate.py --pdf-limit 500  # PDF migration limit batch (default = all)
    python reset_and_migrate.py --yes            # ข้าม confirmation (ใช้ระวัง!)
"""
import os, sys, time, gzip, json, argparse, subprocess
from pathlib import Path
from datetime import datetime, timezone

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv()

from supabase import create_client

MDB_PATH      = r"C:\Users\Administrator\Desktop\New folder\Baby78\Baby78_Safety.mdb"
VBS_EXPORT    = ROOT / "read_mdb.vbs"
CSV_PATH      = ROOT / "zzapp_export.csv"
BACKUP_DIR    = ROOT / "backups"
STORAGE_BUCKET = "policy-pdfs"
STORAGE_FOLDER = "policies"
TABLE_NAME    = "insurance_policies"


# ── Helpers ───────────────────────────────────────────────────────────
def sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def banner(step: int, total: int, title: str):
    print(f"\n{'═'*68}")
    print(f"  [{step}/{total}] {title}")
    print(f"{'═'*68}\n")


def confirm(msg: str, auto_yes: bool) -> bool:
    if auto_yes:
        print(f"⚠️  {msg}  → auto-yes")
        return True
    ans = input(f"⚠️  {msg} [y/N]: ").strip().lower()
    return ans == "y"


# ── Step 1: Backup ────────────────────────────────────────────────────
def step_backup(supabase):
    BACKUP_DIR.mkdir(exist_ok=True)
    now      = datetime.now(timezone.utc)
    fname    = BACKUP_DIR / f"{TABLE_NAME}_{now:%Y-%m-%d_%H%M%S}.jsonl.gz"

    page = 0
    n = 0
    with gzip.open(fname, "wb", compresslevel=6) as gz:
        while True:
            r = supabase.table(TABLE_NAME).select("*").range(page*1000, page*1000+999).execute()
            if not r.data:
                break
            for row in r.data:
                gz.write((json.dumps(row, ensure_ascii=False, default=str) + "\n").encode())
            n += len(r.data)
            print(f"  backup batch {page+1}: {len(r.data)} rows (total {n:,})")
            if len(r.data) < 1000:
                break
            page += 1

    print(f"\n✓ Backup สำเร็จ: {fname}  ({n:,} rows, {fname.stat().st_size/1024:.1f} KB)")


# ── Step 2: Re-export CSV from .mdb ──────────────────────────────────
def step_export_csv():
    if not Path(MDB_PATH).exists():
        print(f"❌ ไม่พบ .mdb: {MDB_PATH}")
        sys.exit(1)

    print(f"  Source : {MDB_PATH}")
    print(f"  Target : {CSV_PATH}")
    cscript_32 = r"C:\Windows\SysWOW64\cscript.exe"
    result = subprocess.run(
        [cscript_32, "//nologo", str(VBS_EXPORT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    print(result.stdout)
    if result.returncode != 0:
        print(f"❌ VBS error: {result.stderr}")
        sys.exit(1)

    if not CSV_PATH.exists():
        print(f"❌ ไม่พบ CSV หลัง export: {CSV_PATH}")
        sys.exit(1)
    print(f"✓ CSV export สำเร็จ: {CSV_PATH} ({CSV_PATH.stat().st_size/1024:.1f} KB)")


# ── Step 3: TRUNCATE ──────────────────────────────────────────────────
def step_truncate(supabase):
    # Supabase client ไม่มี TRUNCATE — ใช้ DELETE ทั้งหมด
    # ใช้ neq กับ id ที่ไม่มีอยู่ → ลบทุก row
    result = supabase.table(TABLE_NAME).delete().neq(
        "id", "00000000-0000-0000-0000-000000000000"
    ).execute()
    deleted = len(result.data) if result.data else 0
    print(f"✓ ลบ {deleted:,} records จาก {TABLE_NAME}")

    # ตรวจสอบว่าว่างแล้ว
    check = supabase.table(TABLE_NAME).select("id", count="exact").limit(1).execute()
    remaining = check.count or 0
    if remaining > 0:
        print(f"⚠️  ยังเหลือ {remaining} records — Supabase อาจ throttle, ลบรอบ 2...")
        supabase.table(TABLE_NAME).delete().neq(
            "id", "00000000-0000-0000-0000-000000000000"
        ).execute()
        check2 = supabase.table(TABLE_NAME).select("id", count="exact").limit(1).execute()
        print(f"  เหลือ: {check2.count or 0} records")


# ── Step 4: Clear Storage ─────────────────────────────────────────────
def step_clear_storage(supabase):
    # list ไฟล์ทั้งหมดใน policies/
    all_files = []
    offset = 0
    while True:
        batch = supabase.storage.from_(STORAGE_BUCKET).list(
            STORAGE_FOLDER, {"limit": 100, "offset": offset}
        )
        if not batch:
            break
        names = [f["name"] for f in batch if f.get("name")]
        all_files.extend(names)
        if len(batch) < 100:
            break
        offset += 100

    if not all_files:
        print("  ไม่มีไฟล์ใน Storage")
        return

    print(f"  พบ {len(all_files):,} ไฟล์ใน {STORAGE_BUCKET}/{STORAGE_FOLDER}/")
    full_paths = [f"{STORAGE_FOLDER}/{n}" for n in all_files]
    BATCH = 100
    for i in range(0, len(full_paths), BATCH):
        chunk = full_paths[i:i+BATCH]
        try:
            supabase.storage.from_(STORAGE_BUCKET).remove(chunk)
            print(f"  ลบแล้ว {min(i+BATCH, len(full_paths)):,}/{len(full_paths):,}")
        except Exception as e:
            print(f"  ⚠️ batch {i} error: {e}")
    print(f"✓ ลบเสร็จ {len(all_files):,} ไฟล์")

    # Reset migration progress JSON
    progress_file = ROOT / "migrate_storage_progress.json"
    if progress_file.exists():
        progress_file.unlink()
        print(f"✓ ลบ {progress_file.name}")


# ── Step 5: Import CSV ────────────────────────────────────────────────
def step_import_csv():
    result = subprocess.run(
        [sys.executable, "import_csv.py"],
        cwd=ROOT, capture_output=False, text=True
    )
    if result.returncode != 0:
        print(f"❌ import_csv.py ล้มเหลว (exit {result.returncode})")
        sys.exit(1)


# ── Step 6: PDF Migration ─────────────────────────────────────────────
def step_pdf_migration(pdf_limit):
    cmd = [sys.executable, "migrate_pdfs_to_storage.py", "--upload-all"]
    if pdf_limit:
        cmd += ["--limit", str(pdf_limit)]

    print(f"  Command: {' '.join(cmd)}")
    print(f"  (อาจใช้เวลานานมาก ~50 นาที สำหรับ 10,336 ไฟล์)\n")
    result = subprocess.run(cmd, cwd=ROOT, capture_output=False, text=True)
    if result.returncode != 0:
        print(f"⚠️  migrate_pdfs_to_storage.py exit code {result.returncode} (อาจมี error แต่ resume ได้)")


# ── Step 7: Delete records without PDF ───────────────────────────────
def step_delete_no_pdf(supabase):
    # นับก่อน
    has_pdf  = supabase.table(TABLE_NAME).select("id", count="exact")\
                   .not_.is_("pdf_url", "null").limit(1).execute()
    no_pdf   = supabase.table(TABLE_NAME).select("id", count="exact")\
                   .is_("pdf_url", "null").limit(1).execute()
    print(f"  มี PDF    : {has_pdf.count or 0:,}")
    print(f"  ไม่มี PDF : {no_pdf.count or 0:,}")

    supabase.table(TABLE_NAME).delete().is_("pdf_url", "null").execute()

    check = supabase.table(TABLE_NAME).select("id", count="exact").limit(1).execute()
    print(f"✓ ลบเสร็จ — เหลือ {check.count or 0:,} records (เฉพาะที่มี PDF)")


# ── Main ──────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-pdf",     action="store_true", help="ข้าม PDF migration")
    ap.add_argument("--keep-no-pdf",  action="store_true", help="เก็บ records ที่ไม่มี PDF (ข้าม step 7)")
    ap.add_argument("--pdf-limit",    type=int, default=0, help="limit PDF migration batch")
    ap.add_argument("--yes",          action="store_true", help="auto-confirm ทุก destructive ops")
    ap.add_argument("--skip-backup",  action="store_true", help="ข้าม backup (ใช้ระวัง!)")
    ap.add_argument("--skip-export",  action="store_true", help="ข้าม VBS export ใช้ CSV เดิม")
    args = ap.parse_args()

    print(f"\n{'█'*68}")
    print(f"  reset_and_migrate.py — {datetime.now():%Y-%m-%d %H:%M:%S}")
    print(f"{'█'*68}")

    supabase = sb()

    # ── นับ records + PDFs ก่อนเริ่ม ──
    cur = supabase.table(TABLE_NAME).select("id", count="exact").limit(1).execute()
    print(f"\n📊 สถานะปัจจุบัน:")
    print(f"  Records ใน DB    : {cur.count or 0:,}")
    print(f"  CSV path         : {CSV_PATH} {'(มีอยู่)' if CSV_PATH.exists() else '(ยังไม่มี)'}")
    print(f"  PDF folder       : C:\\Users\\Administrator\\Desktop\\New folder\\BabyPreechar")

    print(f"\n🎯 จะทำ:")
    steps = [
        (True,             "1. Backup DB ปัจจุบัน → local file"),
        (not args.skip_export, "2. Re-export CSV จาก .mdb"),
        (True,             "3. TRUNCATE insurance_policies"),
        (True,             "4. Clear Storage policy-pdfs/policies/"),
        (True,             "5. Import CSV → Supabase"),
        (not args.skip_pdf,    "6. PDF migration --upload-all"),
        (not args.keep_no_pdf, "7. DELETE records ที่ไม่มี PDF"),
    ]
    for active, desc in steps:
        print(f"  {'✅' if active else '⏭️ '} {desc}")

    if not confirm("\nDestructive operation — ยืนยันไหม?", args.yes):
        print("ยกเลิก")
        return

    # ── เริ่ม ──
    t0 = time.time()
    total = sum(1 for active, _ in steps if active)
    step_n = 0

    if not args.skip_backup:
        step_n += 1; banner(step_n, total, "Backup DB ปัจจุบัน")
        step_backup(supabase)

    if not args.skip_export:
        step_n += 1; banner(step_n, total, "Re-export CSV จาก .mdb")
        step_export_csv()

    step_n += 1; banner(step_n, total, "TRUNCATE insurance_policies")
    step_truncate(supabase)

    step_n += 1; banner(step_n, total, "Clear Storage bucket")
    step_clear_storage(supabase)

    step_n += 1; banner(step_n, total, "Import CSV → Supabase")
    step_import_csv()

    if not args.skip_pdf:
        step_n += 1; banner(step_n, total, "PDF migration")
        step_pdf_migration(args.pdf_limit)

    if not args.keep_no_pdf:
        step_n += 1; banner(step_n, total, "DELETE records ที่ไม่มี PDF")
        if confirm("ลบ records ที่ไม่มี PDF จริงหรือ?", args.yes):
            step_delete_no_pdf(supabase)
        else:
            print("ข้าม — เก็บทุก records ไว้")

    elapsed = time.time() - t0
    print(f"\n{'█'*68}")
    print(f"  เสร็จเรียบร้อย — ใช้เวลา {elapsed/60:.1f} นาที")
    final = supabase.table(TABLE_NAME).select("id", count="exact").limit(1).execute()
    print(f"  Records ใน DB ตอนนี้: {final.count or 0:,}")
    print(f"{'█'*68}\n")


if __name__ == "__main__":
    main()
