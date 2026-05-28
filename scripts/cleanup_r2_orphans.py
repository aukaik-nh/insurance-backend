"""
cleanup_r2_orphans.py
─────────────────────────────────────────────────────────────────────
ลบไฟล์ใน R2 ที่ไม่มี pdf_url อ้างใน Neon DB
(ไฟล์ orphan = upload แล้วแต่ user ไม่กดบันทึก)

USAGE:
    python scripts/cleanup_r2_orphans.py           # dry-run
    python scripts/cleanup_r2_orphans.py --execute # ลบจริง
    python scripts/cleanup_r2_orphans.py --execute --min-age-hours 1
        # ลบเฉพาะไฟล์ที่ค้างเกิน 1 ชม. (กันชนกับ upload ที่กำลังทำ)
"""
import os, sys, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import boto3
from botocore.client import Config


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--min-age-hours", type=float, default=0,
                    help="ลบเฉพาะไฟล์ที่อายุเกิน N ชม. (กันชน — แนะนำ 1)")
    args = ap.parse_args()

    bucket = os.getenv("R2_BUCKET")
    pub    = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

    s3 = boto3.client(
        's3',
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )

    print(f"\n{'='*60}")
    print(f"  R2 orphan cleanup — {'EXECUTE' if args.execute else 'DRY-RUN'}")
    print(f"  bucket: {bucket}")
    print(f"  min age: {args.min_age_hours}h")
    print(f"{'='*60}\n")

    # 1) list ทุกไฟล์ใน R2 bucket
    print("กำลัง list ไฟล์ใน R2...")
    all_objects = []
    paginator = s3.get_paginator('list_objects_v2')
    for page in paginator.paginate(Bucket=bucket):
        for obj in page.get('Contents', []):
            all_objects.append({
                "key":  obj["Key"],
                "size": obj["Size"],
                "modified": obj["LastModified"],
            })
    print(f"  ไฟล์ใน R2: {len(all_objects):,}\n")

    # 2) ดึง pdf_url ที่ใช้งานใน DB → set ของ keys ที่ห้ามลบ
    print("กำลังอ่าน DB...")
    conn = psycopg2.connect(os.getenv("NEON_URL"), connect_timeout=15)
    cur = conn.cursor()

    referenced_keys = set()
    for table in ["insurance_policies", "policy_attachments"]:
        cur.execute(f"SELECT pdf_url FROM {table} WHERE pdf_url IS NOT NULL")
        for (url,) in cur.fetchall():
            if pub and url.startswith(pub):
                key = url.replace(pub + "/", "", 1).split("?", 1)[0]
                referenced_keys.add(key)
    print(f"  ไฟล์ที่ DB อ้างใช้: {len(referenced_keys):,}\n")
    conn.close()

    # 3) หา orphans
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=args.min_age_hours)
    orphans = []
    for obj in all_objects:
        if obj["key"] in referenced_keys:
            continue
        if obj["modified"] > cutoff:
            continue  # อายุยังไม่ถึง — skip กันชนกับ upload กำลังทำ
        orphans.append(obj)

    print(f"{'='*60}")
    print(f"  สรุป")
    print(f"{'='*60}")
    print(f"  ไฟล์ทั้งหมด        : {len(all_objects):,}")
    print(f"  อ้างใน DB           : {len(referenced_keys):,}")
    print(f"  orphan (จะลบ)       : {len(orphans):,}")
    total_size = sum(o["size"] for o in orphans)
    print(f"  ขนาด orphan รวม     : {total_size/1024/1024:.2f} MB")

    if orphans[:10]:
        print(f"\n  ตัวอย่าง orphan:")
        for o in orphans[:10]:
            age = (now - o["modified"]).total_seconds() / 3600
            print(f"    - {o['key']:50}  {o['size']/1024:>6.0f} KB  age={age:.1f}h")
        if len(orphans) > 10:
            print(f"    ... และอีก {len(orphans)-10:,}")

    if not args.execute:
        print(f"\n[DRY-RUN] ระบุ --execute เพื่อลบจริง")
        return

    if not orphans:
        print("\nไม่มี orphan ต้องลบ")
        return

    print(f"\nกำลังลบ...")
    keys = [{"Key": o["key"]} for o in orphans]
    BATCH = 1000
    deleted = 0
    for i in range(0, len(keys), BATCH):
        chunk = keys[i:i+BATCH]
        try:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": chunk, "Quiet": True})
            deleted += len(chunk)
            print(f"  ลบแล้ว {deleted:,}/{len(orphans):,}")
        except Exception as e:
            print(f"  ERROR: {str(e)[:200]}")

    print(f"\n✓ ลบเสร็จ: {deleted:,} ไฟล์ — คืน {total_size/1024/1024:.2f} MB")


if __name__ == "__main__":
    main()
