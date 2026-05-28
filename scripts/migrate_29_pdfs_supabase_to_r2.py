"""
migrate_29_pdfs_supabase_to_r2.py
─────────────────────────────────────────────────────────────────────
รอ Supabase Storage unlock แล้วย้าย 29 PDFs ใหม่ → R2
+ update pdf_url ใน Neon ให้ชี้ R2 URL

USAGE:
    python scripts/migrate_29_pdfs_supabase_to_r2.py            # poll forever
    python scripts/migrate_29_pdfs_supabase_to_r2.py --once     # ลองครั้งเดียว
    python scripts/migrate_29_pdfs_supabase_to_r2.py --interval 120  # poll ทุก N วินาที
"""
import os, sys, time, argparse, urllib.request, urllib.error
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras
import boto3
from botocore.client import Config

SUPABASE_URL    = os.getenv("SUPABASE_URL")
SUPABASE_KEY    = os.getenv("SUPABASE_KEY")
NEON_URL        = os.getenv("NEON_URL")
R2_BUCKET       = os.getenv("R2_BUCKET")
R2_PUBLIC_URL   = os.getenv("R2_PUBLIC_URL", "").rstrip("/")

SUPABASE_PUBLIC_MARKER = "/storage/v1/object/public/"


def _s3():
    return boto3.client(
        's3',
        endpoint_url=os.getenv("R2_ENDPOINT"),
        aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
        aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
        config=Config(signature_version='s3v4'),
        region_name='auto',
    )


def check_supabase_unlocked() -> tuple[bool, int]:
    """test ว่า Supabase Storage API ใช้ได้หรือยัง"""
    test_url = f"{SUPABASE_URL}/rest/v1/insurance_policies?limit=1"
    try:
        req = urllib.request.Request(test_url, headers={"apikey": SUPABASE_KEY})
        r = urllib.request.urlopen(req, timeout=10)
        return (r.status == 200, r.status)
    except urllib.error.HTTPError as e:
        return (False, e.code)
    except Exception as e:
        return (False, 0)


def fetch_pending_records():
    """ดึง records ที่ยังมี pdf_url ชี้ Supabase (= 29 ไฟล์)"""
    conn = psycopg2.connect(NEON_URL, connect_timeout=15)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    items = []

    cur.execute("""
        SELECT id, pdf_url, pdf_filename
        FROM insurance_policies
        WHERE pdf_url IS NOT NULL AND pdf_url LIKE %s
    """, (f"%{SUPABASE_PUBLIC_MARKER}%",))
    for row in cur.fetchall():
        items.append({"table": "insurance_policies", **dict(row)})

    cur.execute("""
        SELECT id, pdf_url, pdf_filename
        FROM policy_attachments
        WHERE pdf_url IS NOT NULL AND pdf_url LIKE %s
    """, (f"%{SUPABASE_PUBLIC_MARKER}%",))
    for row in cur.fetchall():
        items.append({"table": "policy_attachments", **dict(row)})

    conn.close()
    return items


def supabase_path_from_url(url: str) -> tuple[str, str] | None:
    """แยก bucket + storage key จาก Supabase public URL"""
    if SUPABASE_PUBLIC_MARKER not in url:
        return None
    rest = url.split(SUPABASE_PUBLIC_MARKER, 1)[1]
    rest = rest.split("?", 1)[0]
    parts = rest.split("/", 1)
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def migrate_one(item: dict, s3, conn) -> tuple[bool, str]:
    """download Supabase → upload R2 → update Neon"""
    sb_url = item["pdf_url"]
    parsed = supabase_path_from_url(sb_url)
    if not parsed:
        return False, "parse URL fail"
    bucket, key = parsed

    # 1. download from Supabase
    try:
        r = urllib.request.urlopen(sb_url, timeout=30)
        if r.status != 200:
            return False, f"HTTP {r.status}"
        file_bytes = r.read()
    except Exception as e:
        return False, f"download: {str(e)[:80]}"

    # 2. upload to R2 (ใช้ path เดิม)
    try:
        s3.put_object(
            Bucket=R2_BUCKET,
            Key=key,
            Body=file_bytes,
            ContentType="application/pdf",
        )
    except Exception as e:
        return False, f"R2 upload: {str(e)[:80]}"

    # 3. update Neon pdf_url
    new_url = f"{R2_PUBLIC_URL}/{key}"
    try:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE {item['table']} SET pdf_url = %s WHERE id = %s",
            (new_url, item["id"]),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        conn.rollback()
        return False, f"Neon update: {str(e)[:80]}"

    return True, f"{len(file_bytes)/1024:.0f} KB → {new_url}"


def run_migration():
    items = fetch_pending_records()
    print(f"\n[migrate] พบ {len(items)} ไฟล์ที่ต้องย้าย\n")
    if not items:
        return 0, 0

    s3 = _s3()
    conn = psycopg2.connect(NEON_URL, connect_timeout=15)

    ok, fail = 0, 0
    for i, item in enumerate(items, 1):
        fn = item.get("pdf_filename") or "(no name)"
        print(f"  [{i}/{len(items)}] {item['table']} | {fn[:50]}")
        success, msg = migrate_one(item, s3, conn)
        if success:
            ok += 1
            print(f"     ✓ {msg}")
        else:
            fail += 1
            print(f"     ✗ {msg}")

    conn.close()
    print(f"\n[migrate] เสร็จ: {ok} ok, {fail} fail")
    return ok, fail


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="ลองครั้งเดียวไม่ poll")
    ap.add_argument("--interval", type=int, default=120, help="ระยะ poll (วินาที)")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  รอ Supabase unlock แล้วย้าย PDFs ใหม่ → R2")
    print(f"  poll ทุก {args.interval}s")
    print(f"{'='*65}\n")

    attempt = 0
    while True:
        attempt += 1
        unlocked, status = check_supabase_unlocked()
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] check #{attempt}: HTTP {status} → {'✓ UNLOCKED' if unlocked else '⏳ still locked'}")

        if unlocked:
            print(f"\n*** Supabase กลับมาแล้ว — เริ่มย้ายไฟล์ทันที ***")
            run_migration()
            print("\n✓ เสร็จสิ้น")
            break

        if args.once:
            print("\n[--once] ยังไม่ unlock — ออก")
            break

        time.sleep(args.interval)


if __name__ == "__main__":
    main()
