"""
clear_storage_bucket.py
─────────────────────────────────────────────────────────────────────
ลบไฟล์ทั้งหมดใน Supabase Storage bucket folder
ใช้สำหรับ reset ก่อนรัน migration ใหม่

USAGE:
    python scripts/clear_storage_bucket.py --bucket policy-pdfs --folder policies

    # dry-run (preview only)
    python scripts/clear_storage_bucket.py --bucket policy-pdfs --folder policies --dry-run
"""
import os, sys, argparse
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from supabase import create_client


def get_sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def list_all_files(sb, bucket: str, folder: str) -> list[str]:
    """ดึง list ไฟล์ทั้งหมดใน folder — paginate 100 ต่อรอบ"""
    all_files = []
    offset = 0
    while True:
        batch = sb.storage.from_(bucket).list(folder, {
            "limit": 100,
            "offset": offset,
        })
        if not batch:
            break
        names = [f["name"] for f in batch if f.get("name")]
        all_files.extend(names)
        if len(batch) < 100:
            break
        offset += 100
    return all_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", required=True, help="ชื่อ bucket เช่น policy-pdfs")
    ap.add_argument("--folder", default="", help="folder ใน bucket เช่น policies")
    ap.add_argument("--dry-run", action="store_true", help="preview เท่านั้น ไม่ลบจริง")
    args = ap.parse_args()

    print(f"\n{'='*60}")
    print(f"  Clear Supabase Storage")
    print(f"  Bucket : {args.bucket}")
    print(f"  Folder : {args.folder or '(root)'}")
    print(f"  Mode   : {'DRY-RUN' if args.dry_run else 'DELETE'}")
    print(f"{'='*60}\n")

    sb = get_sb()
    files = list_all_files(sb, args.bucket, args.folder)

    if not files:
        print("ไม่มีไฟล์ในโฟลเดอร์นี้")
        return

    print(f"พบไฟล์ {len(files):,} ไฟล์")
    for i, name in enumerate(files[:5], 1):
        print(f"  {i}. {name}")
    if len(files) > 5:
        print(f"  ... และอีก {len(files)-5:,} ไฟล์")

    if args.dry_run:
        print("\nDRY-RUN — ไม่ลบจริง")
        return

    # ลบทีละ batch (100 paths/call)
    full_paths = [f"{args.folder}/{n}" if args.folder else n for n in files]
    BATCH = 100
    deleted = 0
    print(f"\nกำลังลบ...")
    for i in range(0, len(full_paths), BATCH):
        batch = full_paths[i:i+BATCH]
        try:
            sb.storage.from_(args.bucket).remove(batch)
            deleted += len(batch)
            print(f"  ลบแล้ว {deleted:,}/{len(full_paths):,}")
        except Exception as e:
            print(f"  ERROR batch {i}: {e}")

    print(f"\n✓ ลบเสร็จ {deleted:,} ไฟล์")


if __name__ == "__main__":
    main()
