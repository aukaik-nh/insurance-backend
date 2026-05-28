"""
restore_to_neon.py
─────────────────────────────────────────────────────────────────────
รัน SQL file (backup จาก Supabase) เข้าไปใน Neon

USAGE:
    python scripts/restore_to_neon.py backups/neon_restore_YYYYMMDD_HHMMSS.sql
"""
import os, sys, re
from pathlib import Path

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2


def strip_sql_comments(text: str) -> str:
    """ตัด -- comment ออก (รักษา newlines)"""
    out = []
    for line in text.splitlines():
        # หา -- ที่อยู่นอก string
        in_str = False
        cut_at = None
        for i, ch in enumerate(line):
            if ch == "'":
                in_str = not in_str
            if not in_str and i + 1 < len(line) and line[i] == '-' and line[i+1] == '-':
                cut_at = i
                break
        out.append(line[:cut_at] if cut_at is not None else line)
    return '\n'.join(out)


def split_sql(content: str) -> list[str]:
    """แยก SQL statements ด้วย semicolon (ระวัง quoted strings)
    ตัด comment ออกก่อน เพื่อไม่ให้ปนกับ statement"""
    content = strip_sql_comments(content)
    statements = []
    current = []
    in_string = False
    i = 0
    while i < len(content):
        ch = content[i]
        if ch == "'" and (i == 0 or content[i-1] != '\\'):
            # toggle string mode (handle escaped '')
            if in_string and i + 1 < len(content) and content[i+1] == "'":
                current.append(ch); current.append(ch); i += 2; continue
            in_string = not in_string
        current.append(ch)
        if ch == ';' and not in_string:
            stmt = ''.join(current).strip()
            if stmt and stmt != ';':
                statements.append(stmt)
            current = []
        i += 1
    tail = ''.join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def main():
    if len(sys.argv) < 2:
        # หาไฟล์ backup ล่าสุด
        backups = sorted(Path("backups").glob("neon_restore_*.sql"))
        if not backups:
            print("ERROR: ระบุไฟล์ SQL หรือใส่ไว้ใน backups/")
            sys.exit(1)
        sql_file = backups[-1]
        print(f"ใช้ไฟล์ล่าสุด: {sql_file}\n")
    else:
        sql_file = Path(sys.argv[1])

    if not sql_file.exists():
        print(f"ERROR: ไม่พบไฟล์ {sql_file}")
        sys.exit(1)

    print(f"กำลังอ่าน {sql_file} ({sql_file.stat().st_size/1024/1024:.2f} MB)...")
    content = sql_file.read_text(encoding="utf-8")
    statements = split_sql(content)
    print(f"พบ {len(statements):,} statements\n")

    url = os.getenv("NEON_URL")
    if not url:
        print("ERROR: ต้องตั้ง NEON_URL ใน .env")
        sys.exit(1)

    print("เชื่อมต่อ Neon...")
    conn = psycopg2.connect(url, connect_timeout=15)
    conn.autocommit = True   # commit per statement (skip aborted transactions)
    cur = conn.cursor()

    print("รัน statements (autocommit per stmt)...\n")
    ok = 0
    fail = 0
    errors_sample = []
    progress_every = max(100, len(statements) // 20)
    SKIP_PREFIXES = ("BEGIN", "COMMIT", "ROLLBACK", "--")
    for i, stmt in enumerate(statements, 1):
        head = stmt.lstrip().upper()[:10]
        if any(head.startswith(p) for p in SKIP_PREFIXES):
            continue
        try:
            cur.execute(stmt)
            ok += 1
        except Exception as e:
            fail += 1
            if len(errors_sample) < 5:
                errors_sample.append((i, str(e)[:200], stmt[:200]))
        if i % progress_every == 0:
            print(f"  progress: {i:,}/{len(statements):,}  (ok={ok}, fail={fail})")

    print(f"\n✓ executed: {ok:,} ok, {fail:,} fail")
    if errors_sample:
        print("\nตัวอย่าง errors:")
        for idx, err, st in errors_sample:
            print(f"  [{idx}] {err}")
            print(f"     stmt: {st}")

    # verify counts
    print("\nตรวจสอบจำนวน rows:")
    for table in ["insurance_policies", "policy_attachments"]:
        try:
            cur.execute(f"SELECT count(*) FROM {table}")
            print(f"  {table}: {cur.fetchone()[0]:,} rows")
        except Exception as e:
            print(f"  {table}: ERROR {e}")

    cur.close(); conn.close()
    print("\n✓ restore เสร็จ")


if __name__ == "__main__":
    main()
