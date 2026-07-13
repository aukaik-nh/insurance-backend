"""
insert_prepared.py — insert a single policy with pre-parsed data.
Reuses upload → R2 + insert → Neon via the same shim as the web endpoints.

Usage:
    python insert_prepared.py <pdf_path> <json_payload>
    (json_payload = parsed policy fields, as JSON string)

Prints "OK <uuid>" on success or "FAIL <reason>" on failure.
Also appends the pdf_path to batch_import_neon_log.json's "done" list on success.
"""
import os, sys, io, json
from pathlib import Path

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv
load_dotenv(BACKEND_ROOT / ".env")

from services.supabase_shim import create_client
from routes.upload import (
    ALLOWED_COLUMNS, INT_FIELDS, FLOAT_FIELDS, DATE_FIELDS,
    _clean_thai_number, _normalize_date, _make_display_filename,
    _upload_pdf_to_storage,
)

LOG_FILE = BACKEND_ROOT / "scripts" / "batch_import_neon_log.json"


def prepare_row(parsed: dict) -> dict:
    row = {}
    for k, v in parsed.items():
        if k == "raw_text" or k not in ALLOWED_COLUMNS:
            continue
        if k in INT_FIELDS:
            try:
                c = _clean_thai_number(str(v)) if v not in (None, "") else ""
                row[k] = int(float(c)) if c not in ("", "None", "null") else None
            except (ValueError, TypeError):
                row[k] = None
        elif k in FLOAT_FIELDS:
            try:
                c = _clean_thai_number(str(v)) if v not in (None, "") else ""
                row[k] = float(c) if c not in ("", "None", "null") else None
            except (ValueError, TypeError):
                row[k] = None
        elif k in DATE_FIELDS:
            row[k] = _normalize_date(str(v).strip() if v else "")
        else:
            if v in (None, "", "null", "test"):
                row[k] = None
            else:
                row[k] = str(v).strip() or None
    row["manually_edited"] = False
    return row


def mark_done(pdf_path: str):
    try:
        log = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except Exception:
        log = {"done": [], "failed": [], "skipped": []}
    if pdf_path not in log["done"]:
        log["done"].append(pdf_path)
    # remove from failed if present
    log["failed"] = [f for f in log["failed"] if f.get("file") != pdf_path]
    LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    if len(sys.argv) < 3:
        print("usage: insert_prepared.py <pdf_path> <json_file_or_string>")
        sys.exit(2)
    pdf_path = sys.argv[1]
    arg2     = sys.argv[2]

    p = Path(pdf_path)
    if not p.is_file():
        print(f"FAIL file not found: {pdf_path}")
        sys.exit(1)

    # arg2 can be a path to a JSON file, or a raw JSON string
    payload_path = Path(arg2)
    if payload_path.is_file():
        payload = payload_path.read_text(encoding="utf-8")
    else:
        payload = arg2

    try:
        parsed = json.loads(payload)
    except Exception as e:
        print(f"FAIL invalid json: {e}")
        sys.exit(1)

    file_bytes = p.read_bytes()
    row = prepare_row(parsed)
    row["pdf_size"] = len(file_bytes)

    display_name = _make_display_filename(
        plate=row.get("license_plate"),
        doc_type="main",
        coverage_end=row.get("coverage_end"),
        policy_type=row.get("policy_type"),
        address=row.get("insured_address"),
        name=row.get("insured_name"),
    )
    row["pdf_filename"] = display_name

    sb = create_client(None, None)
    pdf_url = _upload_pdf_to_storage(sb, file_bytes, display_name)
    if not pdf_url:
        print("FAIL r2 upload")
        sys.exit(1)
    row["pdf_url"] = pdf_url

    try:
        result = sb.table("insurance_policies").insert(row).execute()
        new_id = result.data[0]["id"] if result.data else None
        mark_done(pdf_path)
        print(f"OK {new_id} filename={display_name}")
    except Exception as e:
        print(f"FAIL db: {type(e).__name__}: {str(e)[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    main()
