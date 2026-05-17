"""
batch_import.py — นำเข้า PDF กรมธรรม์ทั้งหมดจากโฟลเดอร์เข้า Supabase
ใช้ OCR pipeline เดียวกับ upload-pdf endpoint

วิธีใช้:
    python batch_import.py --folder "C:/path/to/pdfs"
    python batch_import.py --folder "C:/path/to/pdfs" --limit 100   # ทดสอบ 100 ไฟล์
    python batch_import.py --folder "C:/path/to/pdfs" --resume      # ข้ามไฟล์ที่บันทึกแล้ว
    python batch_import.py --folder "C:/path/to/pdfs" --dry-run     # ทดสอบโดยไม่บันทึก
"""

import os, sys, time, argparse, traceback, json, base64, re
from pathlib import Path
from datetime import datetime

from dotenv import load_dotenv
load_dotenv()

# ── Import จาก services ──────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))
from services.pdf_extractor import extract_text_from_pdf
from services.claude_parser import parse_insurance_data
from supabase import create_client

# ── Supabase ─────────────────────────────────────────────────────────
def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

BUCKET_NAME = "policy-pdfs"

ALLOWED_COLUMNS = {
    "policy_number","company_code","insured_name","insured_address",
    "license_plate","chassis_no","car_make","car_model","car_year",
    "coverage_start","coverage_end","net_premium","stamp_duty","vat",
    "total_premium","third_party_per_person","third_party_per_accident",
    "own_damage","broker_name","broker_license","manually_edited",
    "pdf_url","pdf_filename","pdf_data","pdf_size",
}
INT_FIELDS   = {"car_year","pdf_size"}
FLOAT_FIELDS = {"net_premium","stamp_duty","vat","total_premium",
                "third_party_per_person","third_party_per_accident","own_damage"}
DATE_FIELDS  = {"coverage_start","coverage_end"}


def _clean_num(val):
    if not val: return val
    thai = str.maketrans("๐๑๒๓๔๕๖๗๘๙","0123456789")
    return str(val).translate(thai).replace(",","").strip()

def _normalize_date(val):
    if not val: return None
    val = val.strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', val)
    if m:
        y = int(m.group(1))
        if y >= 2500: y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', val)
    if m:
        d,mo,y = int(m.group(1)),int(m.group(2)),int(m.group(3))
        if y < 100: y += 2000
        if y >= 2500: y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None

def _prepare_row(parsed: dict) -> dict:
    row = {}
    for k, v in parsed.items():
        if k in ("raw_text",): continue
        if k not in ALLOWED_COLUMNS: continue
        if k in INT_FIELDS:
            try:
                c = _clean_num(str(v)) if v not in (None,"") else ""
                row[k] = int(float(c)) if c not in ("","None","null") else None
            except: row[k] = None
        elif k in FLOAT_FIELDS:
            try:
                c = _clean_num(str(v)) if v not in (None,"") else ""
                row[k] = float(c) if c not in ("","None","null") else None
            except: row[k] = None
        elif k in DATE_FIELDS:
            row[k] = _normalize_date(str(v).strip() if v else "")
        else:
            if v in (None,"","null","test"): row[k] = None
            else: row[k] = str(v).strip() or None
    row["manually_edited"] = False
    return row

def _upload_storage(supabase, file_bytes: bytes, filename: str):
    try:
        import uuid
        ext   = os.path.splitext(filename)[1] or ".pdf"
        uname = f"{uuid.uuid4().hex}{ext}"
        path  = f"policies/{uname}"
        supabase.storage.from_(BUCKET_NAME).upload(
            path=path, file=file_bytes,
            file_options={"content-type":"application/pdf"},
        )
        url = supabase.storage.from_(BUCKET_NAME).get_public_url(path)
        return url if isinstance(url, str) else url.get("publicUrl") or str(url)
    except Exception as e:
        print(f"    [storage] WARNING: {e}")
        return None


# ── Progress log file ─────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "batch_import_log.json"

def load_log() -> dict:
    if LOG_FILE.exists():
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {"done": [], "failed": [], "skipped": []}

def save_log(log: dict):
    try:
        with open(LOG_FILE, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
    except PermissionError:
        # Windows อาจ lock ไฟล์ชั่วคราว — ข้ามการบันทึกครั้งนี้
        pass


# ── MAIN ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="Batch import PDFs to Supabase")
    parser.add_argument("--folder", required=True, help="โฟลเดอร์ที่มีไฟล์ PDF")
    parser.add_argument("--limit",  type=int, default=0, help="จำกัดจำนวนไฟล์ (0 = ไม่จำกัด)")
    parser.add_argument("--resume", action="store_true", help="ข้ามไฟล์ที่บันทึกแล้ว")
    parser.add_argument("--dry-run",action="store_true", help="ทดสอบโดยไม่บันทึก")
    parser.add_argument("--skip-storage", action="store_true", help="ไม่อัปโหลดไปยัง Storage")
    parser.add_argument("--no-pdf-data",  action="store_true", help="ไม่เก็บ base64 PDF ใน DB (ประหยัด space)")
    parser.add_argument("--storage-limit", type=int, default=0,
                        help="upload PDF ขึ้น Storage แค่ N ไฟล์ใหม่สุด (0=ทั้งหมด)")
    args = parser.parse_args()

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"❌ ไม่พบโฟลเดอร์: {folder}")
        sys.exit(1)

    # รวมไฟล์ PDF ทั้งหมด (recursive)
    pdfs = sorted(folder.rglob("*.pdf")) + sorted(folder.rglob("*.PDF"))
    pdfs = list({p.resolve() for p in pdfs})  # deduplicate

    # เรียงตาม mtime (เก่า→ใหม่) เพื่อให้ --storage-limit เอาไฟล์ใหม่สุด
    pdfs.sort(key=lambda p: p.stat().st_mtime)

    # ถ้ามี storage_limit ให้ mark ว่าไฟล์ใหนควร upload storage
    if args.storage_limit > 0:
        storage_set = {str(p) for p in pdfs[-args.storage_limit:]}
    else:
        storage_set = None  # None = upload ทั้งหมด

    if args.limit:
        # จำกัดจำนวนไฟล์ที่ process (เอาท้ายสุด = ใหม่สุด)
        pdfs = pdfs[-args.limit:]

    print(f"\n{'='*60}")
    print(f"  Batch PDF Import")
    print(f"  โฟลเดอร์      : {folder}")
    print(f"  ไฟล์ทั้งหมด   : {len(pdfs):,} ไฟล์")
    if args.storage_limit: print(f"  Storage limit : {args.storage_limit:,} ไฟล์ใหม่สุด")
    if args.limit:         print(f"  Process limit : {args.limit:,} ไฟล์")
    if args.dry_run:       print(f"  โหมด          : DRY RUN (ไม่บันทึก)")
    if args.resume:        print(f"  Resume        : เปิด (ข้ามไฟล์ที่ทำแล้ว)")
    print(f"{'='*60}\n")

    log = load_log()
    done_set = set(log["done"]) if args.resume else set()

    supabase = None if args.dry_run else get_supabase()

    processed = ok = failed = skipped = 0
    t0 = time.time()

    for i, pdf_path in enumerate(pdfs):
        if args.limit and processed >= args.limit:
            break

        fname = pdf_path.name
        fkey  = str(pdf_path)

        # ──── ข้ามไฟล์ที่ทำแล้ว ────
        if args.resume and fkey in done_set:
            skipped += 1
            continue

        processed += 1
        elapsed = time.time() - t0
        rate    = processed / elapsed if elapsed > 0 else 0
        remain  = (len(pdfs) - i - 1) / rate if rate > 0 else 0
        print(f"[{processed:>5}/{len(pdfs):>5}] {fname[:50]:<50} ", end="", flush=True)

        try:
            # 1. อ่านไฟล์
            file_bytes = pdf_path.read_bytes()

            # 2. OCR
            raw_text = extract_text_from_pdf(file_bytes)
            if not raw_text.strip():
                print("⚠ OCR ไม่พบข้อความ")
                log["skipped"].append(fkey)
                save_log(log)
                skipped += 1
                continue

            # 3. Parse
            parsed = parse_insurance_data(raw_text, filename=fname)

            # 4. เตรียม row
            row = _prepare_row(parsed)
            row["pdf_filename"] = fname
            row["pdf_size"]     = len(file_bytes)

            if not args.no_pdf_data:
                row["pdf_data"] = base64.b64encode(file_bytes).decode("ascii")

            if args.dry_run:
                pol  = row.get('policy_number') or '?'
                name = str(row.get('insured_name') or '?')[:20]
                print(f"OK DRY policy={pol} name={name}")
                ok += 1
                continue

            # 5. Upload storage (เฉพาะไฟล์ที่อยู่ใน storage_set)
            should_upload = (
                not args.skip_storage and
                (storage_set is None or fkey in storage_set)
            )
            if should_upload:
                row["pdf_url"] = _upload_storage(supabase, file_bytes, fname)

            # 6. Insert DB
            supabase.table("insurance_policies").insert(row).execute()
            pol  = str(row.get('policy_number') or '?')[:20]
            name = str(row.get('insured_name')  or '?')[:15]
            print(f"OK policy={pol} name={name}")
            ok += 1
            log["done"].append(fkey)
            save_log(log)

        except KeyboardInterrupt:
            print("\n⛔ หยุดโดยผู้ใช้")
            break
        except Exception as e:
            print(f"✗ ERROR: {e}")
            log["failed"].append({"file": fkey, "error": str(e)})
            save_log(log)
            failed += 1

    # ── สรุป ──
    elapsed = time.time() - t0
    print(f"\n{'='*60}")
    print(f"  สรุปผล:")
    print(f"  ✓ สำเร็จ    : {ok:,} ไฟล์")
    print(f"  ✗ ล้มเหลว   : {failed:,} ไฟล์")
    print(f"  ⏭ ข้าม      : {skipped:,} ไฟล์")
    print(f"  ⏱ เวลา      : {elapsed/60:.1f} นาที")
    if ok > 0:
        print(f"  ⚡ เฉลี่ย    : {elapsed/ok:.1f} วิ/ไฟล์")
    if failed > 0:
        print(f"\n  ❌ ไฟล์ที่ล้มเหลวอยู่ใน: batch_import_log.json")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
