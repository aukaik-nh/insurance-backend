"""
batch_import_neon.py — bulk-import PDFs → Neon (DB) + R2 (storage)
Reuses same helpers as routes/upload.py so behavior matches the web UI exactly.

Usage (from D:\\insurance-backend):
    venv\\Scripts\\python.exe scripts\\batch_import_neon.py --folder "C:\\path\\to\\pdfs" --dry-run
    venv\\Scripts\\python.exe scripts\\batch_import_neon.py --folder "C:\\path\\to\\pdfs"
    venv\\Scripts\\python.exe scripts\\batch_import_neon.py --folder "C:\\path\\to\\pdfs" --resume
"""
import os, sys, io, time, json, argparse, traceback
from pathlib import Path

# reroute stdout to utf-8 (Windows cp1252 breaks on Thai)
if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

# make sure we can import backend modules
BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv
load_dotenv(BACKEND_ROOT / ".env")

# reuse the same helpers the web endpoints use → behavior matches web UI
from services.supabase_shim import create_client
from services.gemini_parser import parse_with_gemini
from routes.upload import (
    ALLOWED_COLUMNS, INT_FIELDS, FLOAT_FIELDS, DATE_FIELDS, BUCKET_NAME,
    _clean_thai_number, _normalize_date, _make_display_filename,
    _upload_pdf_to_storage,
)

LOG_FILE = BACKEND_ROOT / "scripts" / "batch_import_neon_log.json"


def load_log():
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"done": [], "failed": [], "skipped": []}


def save_log(log):
    try:
        LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
    except PermissionError:
        pass


def _prepare_row(parsed: dict) -> dict:
    """Normalize parsed dict → row that matches insurance_policies schema.
    Same logic as routes/upload.py::save_policy."""
    row = {}
    for k, v in parsed.items():
        if k == "raw_text":
            continue
        if k not in ALLOWED_COLUMNS:
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
    row["manually_edited"] = False   # flagged as batch-imported (not user-edited)
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=0.5, help="seconds between files")
    args = ap.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"folder not found: {folder}")
        sys.exit(1)

    pdfs = sorted(folder.rglob("*.pdf")) + sorted(folder.rglob("*.PDF"))
    pdfs = sorted({p.resolve() for p in pdfs}, key=lambda p: p.name)
    if args.limit:
        pdfs = pdfs[:args.limit]

    log = load_log()
    done_set = set(log["done"]) if args.resume else set()

    print(f"folder : {folder}")
    print(f"pdfs   : {len(pdfs)}")
    print(f"mode   : {'DRY-RUN' if args.dry_run else 'LIVE (Neon + R2)'}")
    print(f"resume : {args.resume}  (skipping {len(done_set)} already-done)")
    print()

    sb = None if args.dry_run else create_client(None, None)

    ok = fail = skip = 0
    t0 = time.time()

    for i, pdf in enumerate(pdfs, 1):
        key = str(pdf)
        label = f"[{i}/{len(pdfs)}] {pdf.name[:55]}"
        if args.resume and key in done_set:
            skip += 1
            print(f"{label} — SKIP (already done)")
            continue

        print(f"{label} ... ", end="", flush=True)
        try:
            file_bytes = pdf.read_bytes()

            # 1) AI parse
            parsed = parse_with_gemini(file_bytes, filename=pdf.name) or {}
            has_any = any(v not in (None, "", "null") for v in parsed.values())
            if not has_any:
                print("FAIL (AI empty)")
                log["failed"].append({"file": key, "error": "gemini returned empty"})
                save_log(log)
                fail += 1
                if args.delay: time.sleep(args.delay)
                continue

            # 2) Row for DB
            row = _prepare_row(parsed)
            row["pdf_size"] = len(file_bytes)

            # 3) Compute display filename (same rule as save-policy)
            display_name = _make_display_filename(
                plate=row.get("license_plate"),
                doc_type="main",
                coverage_end=row.get("coverage_end"),
                policy_type=row.get("policy_type"),
                address=row.get("insured_address"),
                name=row.get("insured_name"),
            )
            row["pdf_filename"] = display_name

            if args.dry_run:
                pol   = row.get("policy_number") or "?"
                plate = row.get("license_plate") or "?"
                name  = (row.get("insured_name") or "?")[:20]
                print(f"OK DRY policy={pol} plate={plate} name={name} → '{display_name}'")
                ok += 1
                if args.delay: time.sleep(args.delay)
                continue

            # 4) Upload to R2 using original PDF bytes; storage key = Thai display name
            pdf_url = _upload_pdf_to_storage(sb, file_bytes, display_name)
            if not pdf_url:
                print("FAIL (R2 upload)")
                log["failed"].append({"file": key, "error": "r2 upload failed"})
                save_log(log)
                fail += 1
                if args.delay: time.sleep(args.delay)
                continue
            row["pdf_url"] = pdf_url

            # 5) Insert into Neon via shim
            result = sb.table("insurance_policies").insert(row).execute()
            new_id = result.data[0]["id"] if result.data else None

            pol   = str(row.get("policy_number") or "?")[:18]
            plate = str(row.get("license_plate") or "?")[:10]
            print(f"OK id={new_id} policy={pol} plate={plate}")
            log["done"].append(key)
            save_log(log)
            ok += 1
        except KeyboardInterrupt:
            print("\n^C interrupted.")
            break
        except Exception as e:
            print(f"FAIL {type(e).__name__}: {str(e)[:120]}")
            log["failed"].append({"file": key, "error": f"{type(e).__name__}: {e}"})
            save_log(log)
            fail += 1
            traceback.print_exc(limit=2)

        if args.delay: time.sleep(args.delay)

    elapsed = time.time() - t0
    print()
    print(f"done in {elapsed/60:.1f} min — ok={ok}  fail={fail}  skip={skip}")
    print(f"log: {LOG_FILE}")


if __name__ == "__main__":
    main()
