"""
supabase_shim.py — drop-in replacement for supabase-py
─────────────────────────────────────────────────────────────────────
ใช้ Neon (Postgres) + Cloudflare R2 แทน Supabase
ไม่ต้องแก้ routes/ — แค่เปลี่ยน import จาก:
    from supabase import create_client
เป็น:
    from services.supabase_shim import create_client

รองรับ API ที่ codebase ใช้:
- table(...).select/insert/update/delete with chains: eq, gt, gte, lt, lte, ilike,
  is_, not_, or_, in_, order, range
- .execute() → object with .data, .count
- storage.from_(bucket).upload(path, file, file_options)
- storage.from_(bucket).get_public_url(path)
- storage.from_(bucket).remove([paths])
"""
import os
import re
import json
import threading
from typing import Any
import psycopg2
import psycopg2.extras
import boto3
from botocore.client import Config


# ────────────────────────────────────────────────────────────
#   DB layer
# ────────────────────────────────────────────────────────────
_pool_lock = threading.Lock()
_pool: list[psycopg2.extensions.connection] = []
_pool_max = 5


def _db_url() -> str:
    return os.getenv("NEON_URL") or os.getenv("DATABASE_URL_POOLER") or os.getenv("DATABASE_URL")


def _get_conn():
    """simple connection pool — keep up to _pool_max idle connections"""
    with _pool_lock:
        while _pool:
            conn = _pool.pop()
            try:
                # ping
                with conn.cursor() as c:
                    c.execute("SELECT 1")
                return conn
            except Exception:
                try: conn.close()
                except: pass
    return psycopg2.connect(_db_url(), connect_timeout=15)


def _put_conn(conn):
    with _pool_lock:
        if len(_pool) < _pool_max:
            try:
                conn.rollback()  # reset state
                _pool.append(conn)
                return
            except Exception:
                pass
    try: conn.close()
    except: pass


class _Result:
    """mimic supabase result object — has .data and .count"""
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


def _ident(name: str) -> str:
    """quote SQL identifier (column/table)"""
    if not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        raise ValueError(f"invalid identifier: {name}")
    return f'"{name}"'


class _QueryBuilder:
    """chainable SELECT/INSERT/UPDATE/DELETE builder"""

    def __init__(self, table: str):
        self._table   = table
        self._op      = "SELECT"     # SELECT | INSERT | UPDATE | DELETE
        self._cols    = "*"
        self._filters: list[tuple[str, list]] = []  # (sql_fragment, params)
        self._order   = None         # (col, desc)
        self._range   = None         # (start, end)
        self._count   = False
        self._payload = None         # for INSERT/UPDATE
        self._negate_next = False

    # ── SELECT ───────────────────────────────
    def select(self, cols: str = "*", count: str | None = None):
        self._op    = "SELECT"
        self._cols  = cols
        self._count = (count == "exact")
        return self

    # ── INSERT ───────────────────────────────
    def insert(self, data):
        self._op = "INSERT"
        self._payload = data if isinstance(data, list) else [data]
        return self

    # ── UPDATE ───────────────────────────────
    def update(self, data: dict):
        self._op = "UPDATE"
        self._payload = data
        return self

    # ── DELETE ───────────────────────────────
    def delete(self):
        self._op = "DELETE"
        return self

    # ── filters ──────────────────────────────
    def _add_filter(self, frag: str, params: list):
        if self._negate_next:
            frag = f"NOT ({frag})"
            self._negate_next = False
        self._filters.append((frag, params))
        return self

    def raw_filter(self, frag: str, params: list | None = None):
        """escape hatch — เพิ่ม WHERE fragment ดิบ (ต้อง parameterize เอง ผ่าน %s)
        ใช้ตอน .or_() แสดง expression ไม่ได้ เช่น REPLACE(col,' ','') ILIKE %s"""
        return self._add_filter(frag, list(params or []))

    def eq(self, col, val):    return self._add_filter(f"{_ident(col)} = %s", [val])
    def neq(self, col, val):   return self._add_filter(f"{_ident(col)} <> %s", [val])
    def gt(self, col, val):    return self._add_filter(f"{_ident(col)} > %s", [val])
    def gte(self, col, val):   return self._add_filter(f"{_ident(col)} >= %s", [val])
    def lt(self, col, val):    return self._add_filter(f"{_ident(col)} < %s", [val])
    def lte(self, col, val):   return self._add_filter(f"{_ident(col)} <= %s", [val])
    def ilike(self, col, pat): return self._add_filter(f"{_ident(col)} ILIKE %s", [pat])
    def like(self, col, pat):  return self._add_filter(f"{_ident(col)} LIKE %s", [pat])

    def is_(self, col, val):
        if val == "null" or val is None:
            return self._add_filter(f"{_ident(col)} IS NULL", [])
        return self._add_filter(f"{_ident(col)} IS %s", [val])

    def in_(self, col, vals):
        return self._add_filter(f"{_ident(col)} = ANY(%s)", [list(vals)])

    def or_(self, expr: str):
        """parse supabase or_('a.eq.1,b.ilike.%x%') → SQL OR"""
        parts = self._split_or(expr)
        sub_frags, sub_params = [], []
        for p in parts:
            m = re.match(r'^([a-zA-Z_][a-zA-Z0-9_]*)\.([a-z]+)\.(.*)$', p, re.DOTALL)
            if not m:
                continue
            col, op, val = m.group(1), m.group(2), m.group(3)
            col_q = _ident(col)
            if op == "eq":   sub_frags.append(f"{col_q} = %s");   sub_params.append(val)
            elif op == "ilike": sub_frags.append(f"{col_q} ILIKE %s"); sub_params.append(val)
            elif op == "like":  sub_frags.append(f"{col_q} LIKE %s");  sub_params.append(val)
            elif op == "gt":    sub_frags.append(f"{col_q} > %s");     sub_params.append(val)
            elif op == "gte":   sub_frags.append(f"{col_q} >= %s");    sub_params.append(val)
            elif op == "lt":    sub_frags.append(f"{col_q} < %s");     sub_params.append(val)
            elif op == "lte":   sub_frags.append(f"{col_q} <= %s");    sub_params.append(val)
            elif op == "is":
                if val.lower() == "null": sub_frags.append(f"{col_q} IS NULL")
                else: sub_frags.append(f"{col_q} IS %s"); sub_params.append(val)
        if sub_frags:
            self._filters.append((f"({' OR '.join(sub_frags)})", sub_params))
        return self

    @staticmethod
    def _split_or(expr: str) -> list[str]:
        """split on commas not inside () [] {}"""
        out, depth, buf = [], 0, []
        for ch in expr:
            if ch in "([{": depth += 1
            elif ch in ")]}": depth -= 1
            if ch == "," and depth == 0:
                out.append(''.join(buf)); buf = []; continue
            buf.append(ch)
        if buf: out.append(''.join(buf))
        return [p.strip() for p in out if p.strip()]

    @property
    def not_(self):
        self._negate_next = True
        return self

    # ── modifiers ────────────────────────────
    def order(self, col, desc: bool = False):
        self._order = (col, desc)
        return self

    def range(self, start: int, end: int):
        # inclusive both ends → LIMIT (end-start+1) OFFSET start
        self._range = (start, end)
        return self

    # ── execute ──────────────────────────────
    def execute(self) -> _Result:
        conn = _get_conn()
        try:
            cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            try:
                if self._op == "SELECT":
                    return self._exec_select(cur)
                elif self._op == "INSERT":
                    return self._exec_insert(cur, conn)
                elif self._op == "UPDATE":
                    return self._exec_update(cur, conn)
                elif self._op == "DELETE":
                    return self._exec_delete(cur, conn)
            finally:
                cur.close()
        finally:
            _put_conn(conn)

    def _where(self) -> tuple[str, list]:
        if not self._filters: return "", []
        frags  = [f for f, _ in self._filters]
        params = [p for _, ps in self._filters for p in ps]
        return " WHERE " + " AND ".join(frags), params

    def _exec_select(self, cur) -> _Result:
        where_sql, where_params = self._where()
        sql = f"SELECT {self._cols} FROM {_ident(self._table)}{where_sql}"
        if self._order:
            col, desc = self._order
            sql += f" ORDER BY {_ident(col)} {'DESC' if desc else 'ASC'}"
        if self._range:
            start, end = self._range
            limit = end - start + 1
            sql += f" LIMIT {limit} OFFSET {start}"
        cur.execute(sql, where_params)
        rows = [dict(r) for r in cur.fetchall()]
        # count
        count = None
        if self._count:
            count_sql = f"SELECT COUNT(*) AS n FROM {_ident(self._table)}{where_sql}"
            cur.execute(count_sql, where_params)
            count = cur.fetchone()["n"]
        return _Result(rows, count)

    def _exec_insert(self, cur, conn) -> _Result:
        rows = self._payload
        if not rows: return _Result([])
        cols = list(rows[0].keys())
        cols_sql = ", ".join(_ident(c) for c in cols)
        placeholders = "(" + ", ".join(["%s"] * len(cols)) + ")"
        values_sql = ", ".join([placeholders] * len(rows))
        params = [self._adapt(r.get(c)) for r in rows for c in cols]
        sql = f"INSERT INTO {_ident(self._table)} ({cols_sql}) VALUES {values_sql} RETURNING *"
        cur.execute(sql, params)
        data = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return _Result(data)

    def _exec_update(self, cur, conn) -> _Result:
        data = self._payload
        if not data: return _Result([])
        set_cols = list(data.keys())
        set_sql = ", ".join(f"{_ident(c)} = %s" for c in set_cols)
        set_params = [self._adapt(data[c]) for c in set_cols]
        where_sql, where_params = self._where()
        sql = f"UPDATE {_ident(self._table)} SET {set_sql}{where_sql} RETURNING *"
        cur.execute(sql, set_params + where_params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return _Result(rows)

    def _exec_delete(self, cur, conn) -> _Result:
        where_sql, where_params = self._where()
        sql = f"DELETE FROM {_ident(self._table)}{where_sql} RETURNING *"
        cur.execute(sql, where_params)
        rows = [dict(r) for r in cur.fetchall()]
        conn.commit()
        return _Result(rows)

    @staticmethod
    def _adapt(v):
        if isinstance(v, (dict, list)):
            return json.dumps(v, ensure_ascii=False)
        return v


# ────────────────────────────────────────────────────────────
#   Storage layer (R2 via boto3)
# ────────────────────────────────────────────────────────────
_s3_lock = threading.Lock()
_s3_client = None


def _get_s3():
    global _s3_client
    with _s3_lock:
        if _s3_client is None:
            _s3_client = boto3.client(
                's3',
                endpoint_url=os.getenv("R2_ENDPOINT"),
                aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
                aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
                config=Config(signature_version='s3v4'),
                region_name='auto',
            )
    return _s3_client


class _R2Bucket:
    """compat object for supabase.storage.from_(bucket)"""

    # bucket name อ้างจาก env (เราใช้ bucket เดียว) — แต่รับ name มาเพื่อ compat
    def __init__(self, name: str):
        self._requested = name  # มักเป็น "policy-pdfs"
        self._bucket    = os.getenv("R2_BUCKET", name)

    def upload(self, path: str, file: bytes, file_options: dict | None = None):
        content_type = (file_options or {}).get("content-type", "application/octet-stream")
        _get_s3().put_object(
            Bucket=self._bucket,
            Key=path,
            Body=file,
            ContentType=content_type,
        )
        # supabase returns dict — return something truthy
        return {"path": path, "Key": path}

    def get_public_url(self, path: str) -> str:
        base = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
        return f"{base}/{path}"

    def remove(self, paths: list[str]):
        if not paths: return {}
        # batch delete (max 1000 per call)
        objs = [{"Key": p} for p in paths]
        BATCH = 1000
        for i in range(0, len(objs), BATCH):
            _get_s3().delete_objects(
                Bucket=self._bucket,
                Delete={"Objects": objs[i:i+BATCH], "Quiet": True},
            )
        return {"deleted": len(paths)}

    def download(self, path: str) -> bytes:
        obj = _get_s3().get_object(Bucket=self._bucket, Key=path)
        return obj["Body"].read()


class _Storage:
    def from_(self, name: str) -> _R2Bucket:
        return _R2Bucket(name)


# ────────────────────────────────────────────────────────────
#   Client
# ────────────────────────────────────────────────────────────
class _Client:
    storage = _Storage()

    def table(self, name: str) -> _QueryBuilder:
        return _QueryBuilder(name)


_singleton = _Client()


def create_client(url: str | None = None, key: str | None = None) -> _Client:
    """drop-in replacement — url/key arguments ignored (uses NEON_URL + R2_* env)"""
    return _singleton
