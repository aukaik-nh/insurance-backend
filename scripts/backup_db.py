"""
backup_db.py
─────────────────────────────────────────────────────────────────────
Daily backup ของ insurance_policies → Supabase Storage bucket "insurance-backups"

- ดึงข้อมูลทั้งหมดจาก insurance_policies (paginated 1000 ต่อ batch)
- เซฟเป็น JSON Lines (.jsonl) แล้ว gzip
- อัปโหลดไป Supabase Storage path: insurance-backups/YYYY/MM/insurance_policies_YYYY-MM-DD.jsonl.gz
- ลบ backup เก่ากว่า 90 วัน

USAGE (local):
    python scripts/backup_db.py

USAGE (Railway cron):
    ตั้ง cron schedule เป็น "0 19 * * *" (รัน 02:00 ICT ทุกวัน — Railway ใช้ UTC)
    Command: python scripts/backup_db.py
"""
import os, sys, io, gzip, json
from datetime import datetime, timezone, timedelta
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

# โหลด .env เฉพาะตอนรัน local — บน Railway ใช้ env vars ตรงๆ
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client

BUCKET_NAME    = "insurance-backups"
TABLE_NAME     = "insurance_policies"
PAGE_SIZE      = 1000
RETENTION_DAYS = 90  # ลบ backup เก่ากว่าวันนี้


def get_sb():
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_KEY")
    if not (url and key):
        raise RuntimeError("ต้องมี SUPABASE_URL, SUPABASE_KEY ใน env")
    return create_client(url, key)


def dump_table_to_jsonl_gz(sb, table_name: str) -> tuple[bytes, int]:
    """ดึง table ทั้งหมด → JSON Lines + gzip → คืน (bytes, row_count)"""
    buf = io.BytesIO()
    rows_total = 0

    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        page = 0
        while True:
            r = sb.table(table_name).select("*")\
                  .range(page * PAGE_SIZE, page * PAGE_SIZE + PAGE_SIZE - 1)\
                  .execute()
            if not r.data:
                break
            for row in r.data:
                gz.write((json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            rows_total += len(r.data)
            print(f"  ดึง batch {page+1}: {len(r.data):,} rows (รวม {rows_total:,})")
            if len(r.data) < PAGE_SIZE:
                break
            page += 1

    return buf.getvalue(), rows_total


def upload_backup(sb, data: bytes, storage_path: str):
    sb.storage.from_(BUCKET_NAME).upload(
        path=storage_path,
        file=data,
        file_options={
            "content-type": "application/gzip",
            "upsert": "true",  # overwrite ถ้ามีไฟล์ชื่อซ้ำ (รันซ้ำวันเดียวกัน)
        },
    )


def cleanup_old_backups(sb, retention_days: int):
    """ลบ backup เก่ากว่า retention_days — ดู file path ที่มี date pattern"""
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    deleted = 0

    try:
        # list ไฟล์ในทุก subfolder YYYY/MM/
        files = sb.storage.from_(BUCKET_NAME).list("", {"limit": 1000})
        years = [f["name"] for f in (files or []) if f.get("name", "").isdigit()]

        for year in years:
            months = sb.storage.from_(BUCKET_NAME).list(year, {"limit": 100})
            for month_entry in (months or []):
                month = month_entry.get("name")
                if not month or not month.isdigit():
                    continue
                files_in_month = sb.storage.from_(BUCKET_NAME).list(f"{year}/{month}", {"limit": 1000})
                for f in (files_in_month or []):
                    name = f.get("name", "")
                    # parse YYYY-MM-DD จาก filename
                    try:
                        date_str = name.split("_")[-1].replace(".jsonl.gz", "").replace(".gz", "")
                        file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                        if file_date < cutoff:
                            full_path = f"{year}/{month}/{name}"
                            sb.storage.from_(BUCKET_NAME).remove([full_path])
                            print(f"  ลบ backup เก่า: {full_path}")
                            deleted += 1
                    except (ValueError, IndexError):
                        continue
    except Exception as e:
        print(f"WARNING: cleanup ล้มเหลว: {e}")

    return deleted


def main():
    print(f"\n{'='*65}")
    print(f"  backup_db.py — {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*65}\n")

    sb = get_sb()

    now       = datetime.now(timezone.utc)
    date_str  = now.strftime("%Y-%m-%d")
    year_mo   = f"{now.year:04d}/{now.month:02d}"
    file_name = f"{TABLE_NAME}_{date_str}.jsonl.gz"
    full_path = f"{year_mo}/{file_name}"

    print(f"Dump table: {TABLE_NAME}")
    data, n_rows = dump_table_to_jsonl_gz(sb, TABLE_NAME)
    size_kb = len(data) / 1024

    print(f"\n✓ ดึง {n_rows:,} rows → {size_kb:,.1f} KB (gzipped)")

    # ── Save local file (always — ปลอดภัยที่สุด) ──
    local_dir = Path(__file__).resolve().parent.parent / "backups"
    local_dir.mkdir(parents=True, exist_ok=True)
    local_path = local_dir / file_name
    local_path.write_bytes(data)
    print(f"\n✓ Local backup saved → {local_path}")

    # ── Upload to Storage (optional — ไม่ fail ถ้า bucket ไม่มี) ──
    try:
        print(f"\nUpload → {BUCKET_NAME}/{full_path}")
        upload_backup(sb, data, full_path)
        print(f"✓ Storage upload สำเร็จ")

        print(f"\nCleanup backups เก่ากว่า {RETENTION_DAYS} วัน...")
        deleted = cleanup_old_backups(sb, RETENTION_DAYS)
        print(f"✓ ลบ {deleted} ไฟล์")
    except Exception as e:
        print(f"\n⚠️ Storage upload ล้มเหลว (local backup ยังอยู่): {e}")
        print(f"   → ตรวจสอบว่า bucket '{BUCKET_NAME}' มีอยู่ใน Supabase Storage หรือไม่")

    print(f"\n{'='*65}")
    print(f"  เสร็จเรียบร้อย — backup {n_rows:,} rows, {size_kb:,.1f} KB")
    print(f"  Local: {local_path}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
