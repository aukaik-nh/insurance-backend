"""
export_to_neon.py
─────────────────────────────────────────────────────────────────────
ดึง schema + data ของ insurance_policies + policy_attachments
จาก Supabase แล้ว generate SQL พร้อม restore ไปที่ Neon

USAGE:
    python scripts/export_to_neon.py
    → สร้างไฟล์ backups/neon_restore_YYYYMMDD_HHMMSS.sql
"""
import os, sys, json
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv()

import psycopg2
import psycopg2.extras

TABLES = ["insurance_policies", "policy_attachments"]
OUT_DIR = Path(__file__).resolve().parent.parent / "backups"
OUT_DIR.mkdir(exist_ok=True)


def get_schema(cur, table: str) -> str:
    """ดึง CREATE TABLE statement"""
    cur.execute("""
        SELECT
            column_name, data_type, character_maximum_length,
            is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
    """, (table,))
    cols = cur.fetchall()
    if not cols:
        return ""

    lines = []
    for name, dtype, maxlen, nullable, default in cols:
        t = dtype
        if dtype == "character varying" and maxlen:
            t = f"varchar({maxlen})"
        elif dtype == "character varying":
            t = "varchar"
        elif dtype == "timestamp with time zone":
            t = "timestamptz"
        elif dtype == "timestamp without time zone":
            t = "timestamp"
        elif dtype == "double precision":
            t = "double precision"

        line = f"  {name} {t}"
        if default:
            # skip Supabase-specific defaults
            if "uuid_generate_v4" in default or "gen_random_uuid" in default:
                line += " DEFAULT gen_random_uuid()"
            elif "now()" in default.lower():
                line += " DEFAULT now()"
        if nullable == "NO":
            line += " NOT NULL"
        lines.append(line)

    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(lines) + "\n);"


def escape_sql_value(v):
    """แปลงค่า Python → SQL literal"""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime,)):
        return f"'{v.isoformat()}'"
    if isinstance(v, dict) or isinstance(v, list):
        s = json.dumps(v, ensure_ascii=False).replace("'", "''")
        return f"'{s}'::jsonb"
    # string — escape single quote
    s = str(v).replace("'", "''")
    return f"'{s}'"


def export_data(cur, table: str) -> list[str]:
    """generate INSERT statements"""
    cur.execute(f"SELECT * FROM {table}")
    rows = cur.fetchall()
    if not rows:
        return []
    colnames = [d.name for d in cur.description]
    cols_sql = ", ".join(colnames)
    statements = []
    for row in rows:
        values = ", ".join(escape_sql_value(v) for v in row)
        statements.append(f"INSERT INTO {table} ({cols_sql}) VALUES ({values}) ON CONFLICT (id) DO NOTHING;")
    return statements


def main():
    url = os.getenv("DATABASE_URL_POOLER")
    if not url:
        print("ERROR: ตั้ง DATABASE_URL_POOLER ใน .env ก่อน")
        sys.exit(1)

    print("เชื่อมต่อ Supabase (via pooler)...")
    conn = psycopg2.connect(url, connect_timeout=15)
    cur = conn.cursor()

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = OUT_DIR / f"neon_restore_{ts}.sql"

    print(f"export → {out_path}\n")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("-- ─────────────────────────────────────────────\n")
        f.write(f"-- Export จาก Supabase → restore ใส่ Neon\n")
        f.write(f"-- Generated: {datetime.now().isoformat()}\n")
        f.write("-- ─────────────────────────────────────────────\n\n")
        f.write("BEGIN;\n\n")

        # extensions
        f.write("CREATE EXTENSION IF NOT EXISTS \"pgcrypto\";\n\n")

        for table in TABLES:
            print(f"-- table: {table}")
            schema = get_schema(cur, table)
            if not schema:
                print(f"  WARN: ไม่พบ schema ของ {table}")
                continue
            f.write(f"-- ===== {table} =====\n")
            f.write(schema + "\n\n")

            inserts = export_data(cur, table)
            print(f"  rows: {len(inserts):,}")
            if inserts:
                # batch INSERT per 100 lines for readability
                for stmt in inserts:
                    f.write(stmt + "\n")
                f.write("\n")

        f.write("COMMIT;\n")

    size = out_path.stat().st_size
    print(f"\n✓ เสร็จ — ไฟล์ขนาด {size/1024/1024:.2f} MB")
    print(f"   {out_path}")

    cur.close(); conn.close()


if __name__ == "__main__":
    main()
