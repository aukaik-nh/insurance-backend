"""
restore_neon.py
─────────────────────────────────────────────────────────────────────
Restore backup จาก backup_neon.py กลับเข้า Neon (หรือ DB ใหม่)

USAGE:
    python scripts/restore_neon.py backups/neon_20260610_135035.jsonl.gz

    # ทดสอบ (dry-run) — แค่ count ไม่ insert จริง
    python scripts/restore_neon.py backups/xxx.jsonl.gz --dry-run

    # restore ไป DB อื่น (เช่นเครื่องใหม่)
    python scripts/restore_neon.py backups/xxx.jsonl.gz --url postgresql://...

⚠️  By default จะ TRUNCATE table ก่อน insert (เพื่อ restore ตรงๆ)
    ใช้ --append ถ้าต้องการ merge เข้ากับข้อมูลที่มีอยู่ (skip id ซ้ำ)
"""
import os, sys, gzip, json, argparse
from pathlib import Path
from collections import defaultdict

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


def load_backup(path: Path) -> tuple[dict, dict[str, list[dict]]]:
    """อ่าน .jsonl.gz → (meta, {table: [rows]})"""
    meta = {}
    tables: dict[str, list[dict]] = defaultdict(list)
    with gzip.open(path, "rt", encoding="utf-8") as g:
        for line in g:
            d = json.loads(line)
            if d.get("_meta"):
                meta = d
                continue
            t = d.pop("_table", None)
            if t:
                tables[t].append(d)
    return meta, tables


def restore_table(conn, table: str, rows: list[dict], append: bool, dry: bool):
    """insert rows กลับเข้า table"""
    if not rows:
        return 0
    columns = list(rows[0].keys())
    cur = conn.cursor()

    if not append and not dry:
        cur.execute(f'TRUNCATE TABLE "{table}" CASCADE')
        print(f"    TRUNCATE {table}")

    if dry:
        print(f"    [dry-run] would insert {len(rows):,} rows")
        return len(rows)

    insert_sql = (
        f'INSERT INTO "{table}" ({", ".join(f"{chr(34)}{c}{chr(34)}" for c in columns)}) '
        f"VALUES %s " + ("ON CONFLICT (id) DO NOTHING" if append else "")
    )
    values = [tuple(r.get(c) for c in columns) for r in rows]

    psycopg2.extras.execute_values(cur, insert_sql, values, page_size=500)
    n = cur.rowcount
    conn.commit()
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="backup .jsonl.gz")
    ap.add_argument("--url", help="DB url (default: NEON_URL)")
    ap.add_argument("--append", action="store_true", help="ไม่ truncate, skip id ซ้ำ")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = Path(args.file)
    if not path.exists():
        print(f"❌ ไฟล์ไม่พบ: {path}")
        sys.exit(1)

    url = args.url or os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    if not url:
        print("❌ ไม่พบ NEON_URL/DATABASE_URL")
        sys.exit(1)

    print(f"\n{'='*65}")
    print(f"  restore_neon.py {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"  File: {path}")
    print(f"  Target: {url.split('@')[-1][:50]}...")
    print(f"{'='*65}\n")

    print("Loading backup...")
    meta, tables = load_backup(path)
    print(f"  backup_at: {meta.get('backup_at')}")
    print(f"  tables: {list(tables.keys())}")
    print()

    conn = psycopg2.connect(url)
    try:
        for t, rows in tables.items():
            print(f"  Restore {t} ({len(rows):,} rows)...")
            n = restore_table(conn, t, rows, args.append, args.dry_run)
            print(f"    ✓ {n:,} rows inserted")
    finally:
        conn.close()

    print(f"\n{'='*65}")
    print(f"  {'DRY-RUN เสร็จ — ไม่ได้แตะ DB' if args.dry_run else 'Restore สำเร็จ'}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
