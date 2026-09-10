"""Atomic policy/attachment insertion for reviewed batch records."""
import hashlib
import psycopg2.extras
from services.supabase_shim import _get_conn, _put_conn, _ident


def insert_policy_pair(main, attachment=None):
    conn = _get_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            identity = str(main.get('company_code') or '') + ':' + main['policy_number']
            lock = int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], 'big', signed=True)
            cur.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
            cur.execute('SELECT id FROM insurance_policies WHERE policy_number=%s AND COALESCE(company_code,\'\')=%s LIMIT 1', (main['policy_number'], main.get('company_code') or ''))
            if cur.fetchone():
                raise ValueError('เลขกรมธรรม์และบริษัทนี้มีอยู่แล้ว กรุณาตรวจรายการเดิม')
            def insert(table, row):
                columns = list(row)
                sql = f'INSERT INTO {_ident(table)} (' + ','.join(_ident(key) for key in columns) + ') VALUES (' + ','.join(['%s'] * len(columns)) + ') RETURNING id'
                cur.execute(sql, [row[key] for key in columns])
                return cur.fetchone()['id']
            policy_id = insert('insurance_policies', main)
            if attachment:
                insert('policy_attachments', {**attachment, 'policy_id': policy_id})
        conn.commit()
        return policy_id
    except Exception:
        conn.rollback()
        raise
    finally:
        _put_conn(conn)
