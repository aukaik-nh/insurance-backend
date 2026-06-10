"""
backup_neon.py
─────────────────────────────────────────────────────────────────────
Daily backup ทุกตารางใน Neon → local file + Google Drive

ขั้นตอน:
  1. ดึงทุก table ใน public schema → JSON Lines + gzip
  2. เซฟไฟล์ลง local folder backups/ (ไม่หายเด็ดขาด)
  3. อัปขึ้น Google Drive folder (ตั้งใน GOOGLE_DRIVE_FOLDER_ID)
  4. ลบ backup local เก่ากว่า 60 วัน (Drive เก็บไว้นานกว่า)

USAGE:
    python scripts/backup_neon.py             # backup ครั้งเดียว
    python scripts/backup_neon.py --skip-drive  # ไม่อัป Drive
    python scripts/backup_neon.py --no-cleanup   # ไม่ลบไฟล์เก่า

SCHEDULE บน Windows (Task Scheduler):
    Program: D:\\insurance-backend\\venv\\Scripts\\python.exe
    Args:    D:\\insurance-backend\\scripts\\backup_neon.py
    Trigger: Daily 02:00

SCHEDULE บน Render/Railway (cron):
    0 19 * * *   (= 02:00 ICT)
    python scripts/backup_neon.py
"""
import os, sys, io, gzip, json, argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta

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
import httpx


LOCAL_RETENTION_DAYS = 60
BACKUP_DIR = Path(__file__).resolve().parent.parent / "backups"


# ─────────────────────────────────────────────────────────────
#   1. Dump DB → JSONL.gz
# ─────────────────────────────────────────────────────────────
def dump_all_tables() -> tuple[bytes, dict[str, int]]:
    """ดึงทุกตาราง public schema → bytes ของ .jsonl.gz file
    คืน (bytes, {table_name: row_count})"""
    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    if not url:
        raise RuntimeError("ไม่พบ NEON_URL หรือ DATABASE_URL ใน env")

    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    cur.execute("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema='public' AND table_type='BASE TABLE'
        ORDER BY table_name
    """)
    tables = [r["table_name"] for r in cur.fetchall()]

    buf = io.BytesIO()
    counts: dict[str, int] = {}

    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        # header line — metadata
        meta = {
            "_meta": True,
            "backup_at": datetime.now(timezone.utc).isoformat(),
            "tables": tables,
            "format_version": 1,
        }
        gz.write((json.dumps(meta, ensure_ascii=False, default=str) + "\n").encode("utf-8"))

        for t in tables:
            cur.execute(f'SELECT * FROM "{t}"')
            rows = cur.fetchall()
            counts[t] = len(rows)
            for row in rows:
                rec = {"_table": t, **dict(row)}
                gz.write((json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
            print(f"  ✓ {t}: {len(rows):,} rows")

    conn.close()
    return buf.getvalue(), counts


# ─────────────────────────────────────────────────────────────
#   2. Google Drive upload (REST API + refresh token)
# ─────────────────────────────────────────────────────────────
def _get_drive_access_token() -> str | None:
    """แลก refresh_token → access_token (อายุ 1 ชม.)"""
    cid = os.getenv("GOOGLE_CLIENT_ID")
    csec = os.getenv("GOOGLE_CLIENT_SECRET")
    rtok = os.getenv("GOOGLE_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        return None
    r = httpx.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": rtok,
            "grant_type": "refresh_token",
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def upload_to_drive(data: bytes, filename: str) -> str | None:
    """อัพไฟล์ → Drive folder ที่ตั้งใน GOOGLE_DRIVE_FOLDER_ID
    คืน file_id ถ้าสำเร็จ, None ถ้าไม่มี config"""
    folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID")
    token = _get_drive_access_token()
    if not (folder_id and token):
        return None

    metadata = {
        "name": filename,
        "parents": [folder_id],
    }
    # multipart upload (metadata + bytes)
    boundary = "----neonbackupboundary"
    body = (
        f"--{boundary}\r\n"
        "Content-Type: application/json; charset=UTF-8\r\n\r\n"
        + json.dumps(metadata) + "\r\n"
        f"--{boundary}\r\n"
        "Content-Type: application/gzip\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--".encode("utf-8")

    r = httpx.post(
        "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": f"multipart/related; boundary={boundary}",
        },
        content=body,
        timeout=120,
    )
    r.raise_for_status()
    return r.json().get("id")


# ─────────────────────────────────────────────────────────────
#   3. Cleanup local
# ─────────────────────────────────────────────────────────────
def cleanup_local(days: int) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    deleted = 0
    for f in BACKUP_DIR.glob("neon_*.jsonl.gz"):
        try:
            mtime = datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
            if mtime < cutoff:
                f.unlink()
                print(f"  ลบเก่า: {f.name}")
                deleted += 1
        except Exception as e:
            print(f"  WARN: {f.name}: {e}")
    return deleted


# ─────────────────────────────────────────────────────────────
#   Main
# ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-drive", action="store_true")
    ap.add_argument("--no-cleanup", action="store_true")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  backup_neon.py — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}\n")

    # 1. Dump
    print("Dump tables จาก Neon:")
    data, counts = dump_all_tables()
    size_kb = len(data) / 1024
    total_rows = sum(counts.values())
    print(f"\n✓ รวม {total_rows:,} rows จาก {len(counts)} tables → {size_kb:,.1f} KB (gzipped)")

    # 2. Save local
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"neon_{ts}.jsonl.gz"
    fpath = BACKUP_DIR / fname
    fpath.write_bytes(data)
    print(f"\n✓ Local: {fpath}")

    # 3. Upload Drive
    if not args.skip_drive:
        try:
            print(f"\nUpload → Google Drive...")
            file_id = upload_to_drive(data, fname)
            if file_id:
                print(f"✓ Drive file_id: {file_id}")
                print(f"  https://drive.google.com/file/d/{file_id}/view")
            else:
                print("⚠️  ข้าม (ไม่มี GOOGLE_DRIVE_FOLDER_ID หรือ OAuth token)")
        except Exception as e:
            print(f"⚠️  Drive upload ล้มเหลว (local ยังอยู่): {e}")

    # 4. Cleanup
    if not args.no_cleanup:
        print(f"\nCleanup local backups เก่ากว่า {LOCAL_RETENTION_DAYS} วัน...")
        deleted = cleanup_local(LOCAL_RETENTION_DAYS)
        print(f"✓ ลบ {deleted} ไฟล์")

    print(f"\n{'='*65}")
    print(f"  เสร็จเรียบร้อย")
    for t, n in counts.items():
        print(f"    {t}: {n:,} rows")
    print(f"  ไฟล์: {fpath.name} ({size_kb:,.1f} KB)")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
