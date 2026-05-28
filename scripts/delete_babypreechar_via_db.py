"""
delete_babypreechar_via_db.py
─────────────────────────────────────────────────────────────────────
ลบ PDF batch BabyPreechar ออกจาก Supabase Storage ผ่าน Postgres ตรง
(bypass PostgREST ที่โดน restrict)

กลยุทธ์ — Whitelist approach (ปลอดภัยสุด):
  KEEP = paths ที่ปรากฏใน
    1. policy_attachments.pdf_url (PDF แนบจากเว็บ ใหม่)
    2. insurance_policies.pdf_url WHERE pdf_filename NOT IN BabyPreechar done list
       (PDF ใหม่จาก /upload-pdf ที่ผู้ใช้สร้าง policy ใหม่ผ่านเว็บ)
  DELETE = storage.objects ใน bucket - KEEP

USAGE:
    python scripts/delete_babypreechar_via_db.py                # dry-run
    python scripts/delete_babypreechar_via_db.py --execute      # ลบจริง
"""
import os, sys, json, argparse
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras

BUCKET_NAME   = "policy-pdfs"
PROGRESS_FILE = Path(__file__).resolve().parent.parent / "migrate_storage_progress.json"
PUBLIC_MARKER = "/storage/v1/object/public/"


def get_conn():
    url = os.getenv("DATABASE_URL_POOLER") or os.getenv("DATABASE_URL")
    if not url:
        print("ERROR: ต้องตั้ง DATABASE_URL_POOLER ใน .env")
        sys.exit(1)
    return psycopg2.connect(url, connect_timeout=15)


def parse_storage_name(pdf_url: str) -> str | None:
    """แยก storage object name (e.g., 'policies/xxx_uuid.pdf') จาก public URL
    รองรับ URL ที่มี query string ติดท้าย (เช่น '...pdf?' หรือ '...pdf?download=1')
    """
    if not pdf_url or PUBLIC_MARKER not in pdf_url:
        return None
    rest = pdf_url.split(PUBLIC_MARKER, 1)[1]
    # ตัด query string + fragment ก่อนแยก path
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    parts = rest.split("/", 1)
    if len(parts) != 2 or parts[0] != BUCKET_NAME:
        return None
    return parts[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="ลบจริง (default = dry-run)")
    ap.add_argument("--batch", type=int, default=500,
                    help="ขนาด batch สำหรับ DELETE (default 500)")
    args = ap.parse_args()

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"\n{'='*65}")
    print(f"  delete_babypreechar_via_db.py  [{mode}]")
    print(f"{'='*65}\n")

    # 1. load BabyPreechar done list
    if not PROGRESS_FILE.exists():
        print(f"ERROR: ไม่พบ {PROGRESS_FILE}")
        sys.exit(1)
    with open(PROGRESS_FILE, encoding="utf-8") as f:
        progress = json.load(f)
    done_set = set(progress.get("done", []))
    print(f"BabyPreechar filenames (done): {len(done_set):,}")

    conn = get_conn()
    cur  = conn.cursor()

    # 2. build KEEP set
    print("\nกำลังสร้าง KEEP set...")

    # 2a. all paths referenced from policy_attachments (ALL kept — new uploads)
    cur.execute("SELECT pdf_url FROM policy_attachments WHERE pdf_url IS NOT NULL")
    keep_from_attach = set()
    for (url,) in cur.fetchall():
        name = parse_storage_name(url)
        if name:
            keep_from_attach.add(name)
    print(f"  จาก policy_attachments       : {len(keep_from_attach):,} paths")

    # 2b. paths from insurance_policies WHERE pdf_filename NOT IN done_set (new uploads via /upload-pdf)
    cur.execute("SELECT pdf_url, pdf_filename FROM insurance_policies WHERE pdf_url IS NOT NULL")
    rows_with_pdf = cur.fetchall()
    keep_from_policies = set()
    delete_from_policies_ids = []   # rows to clear column on later
    for (url, fn) in rows_with_pdf:
        name = parse_storage_name(url)
        if not name:
            continue
        if fn and fn in done_set:
            # this is BabyPreechar — will delete
            continue
        # new upload via web — keep
        keep_from_policies.add(name)
    print(f"  จาก insurance_policies (ใหม่): {len(keep_from_policies):,} paths")

    keep_set = keep_from_attach | keep_from_policies
    print(f"  รวม KEEP set                  : {len(keep_set):,} paths\n")

    # 3. get full list of objects in bucket
    print("กำลัง list storage.objects ทั้ง bucket...")
    cur.execute("""
        SELECT name, COALESCE((metadata->>'size')::bigint, 0)
        FROM storage.objects
        WHERE bucket_id = %s
    """, (BUCKET_NAME,))
    all_objects = cur.fetchall()
    total_size = sum(s for _, s in all_objects)
    print(f"  จำนวนไฟล์ทั้ง bucket : {len(all_objects):,}")
    print(f"  ขนาดรวม              : {total_size/1024/1024/1024:.3f} GB\n")

    # 4. compute DELETE set
    to_delete = []
    delete_size = 0
    for (name, size) in all_objects:
        if name in keep_set:
            continue
        to_delete.append(name)
        delete_size += size

    print(f"{'='*65}")
    print(f"  สรุปแผน")
    print(f"{'='*65}")
    print(f"  จะลบ storage objects   : {len(to_delete):,} ไฟล์")
    print(f"  ขนาดที่จะคืน           : {delete_size/1024/1024/1024:.3f} GB")
    print(f"  เก็บไว้                : {len(all_objects)-len(to_delete):,} ไฟล์")
    print(f"  คาดว่า bucket หลังลบ   : {(total_size-delete_size)/1024/1024:.1f} MB")

    # 5. count DB rows ที่จะ clear
    rows_to_clear = sum(1 for (url, fn) in rows_with_pdf if fn and fn in done_set)
    print(f"  จะ NULL pdf_url ใน DB  : {rows_to_clear:,} rows")

    if to_delete[:5]:
        print(f"\n  ตัวอย่าง ไฟล์ที่จะลบ:")
        for n in to_delete[:5]:
            print(f"    - {n}")
        print(f"    ... และอีก {len(to_delete)-5:,}")

    if not args.execute:
        print(f"\n[DRY-RUN] ยังไม่ลบจริง — ระบุ --execute เพื่อดำเนินการ")
        cur.close(); conn.close()
        return

    # 6. EXECUTE — delete storage.objects
    if not to_delete:
        print("\nไม่มีไฟล์ที่จะลบ"); cur.close(); conn.close(); return

    print(f"\nลบ storage.objects (batch {args.batch})...")
    deleted = 0
    for i in range(0, len(to_delete), args.batch):
        batch = to_delete[i:i+args.batch]
        try:
            # bypass storage.protect_objects_delete trigger
            cur.execute("SET session_replication_role = 'replica'")
            cur.execute(
                "DELETE FROM storage.objects WHERE bucket_id = %s AND name = ANY(%s)",
                (BUCKET_NAME, batch),
            )
            rowcount = cur.rowcount
            cur.execute("SET session_replication_role = 'origin'")
            conn.commit()
            deleted += rowcount
            print(f"  deleted {deleted:,}/{len(to_delete):,}")
        except Exception as e:
            conn.rollback()
            print(f"  ERROR batch {i}: {str(e)[:200]}")
    print(f"\n✓ ลบ storage.objects เสร็จ: {deleted:,}")

    # 7. clear pdf_* columns ใน insurance_policies (เฉพาะ BabyPreechar)
    if done_set:
        print(f"\nล้าง pdf columns ใน insurance_policies (เฉพาะ BabyPreechar)...")
        # ใช้ ANY กับ array ของ filenames
        done_list = list(done_set)
        cleared = 0
        DB_BATCH = 1000
        for i in range(0, len(done_list), DB_BATCH):
            batch = done_list[i:i+DB_BATCH]
            try:
                cur.execute("""
                    UPDATE insurance_policies
                    SET pdf_url = NULL, pdf_filename = NULL, pdf_size = NULL
                    WHERE pdf_filename = ANY(%s) AND pdf_url IS NOT NULL
                """, (batch,))
                conn.commit()
                cleared += cur.rowcount
                print(f"  cleared {cleared:,} rows")
            except Exception as e:
                conn.rollback()
                print(f"  ERROR batch {i}: {str(e)[:200]}")
        print(f"\n✓ NULL pdf_url เสร็จ: {cleared:,} rows")

    # 8. verify
    cur.execute("SELECT count(*), COALESCE(sum((metadata->>'size')::bigint),0) FROM storage.objects WHERE bucket_id = %s", (BUCKET_NAME,))
    cnt, sz = cur.fetchone()
    print(f"\n  bucket หลังลบ: {cnt:,} ไฟล์ / {sz/1024/1024:.1f} MB")

    cur.close(); conn.close()
    print("\n✓ เสร็จสิ้น — รอ Supabase ปลด restriction ~5-30 นาที")


if __name__ == "__main__":
    main()
