"""
migrate_pdfs_to_storage.py
─────────────────────────────────────────────────────────────────────
อัปโหลด PDF จาก local folder → Supabase Storage แล้ว match กับ
insurance_policies ใน Supabase (ใช้ filename matching จาก
match_and_upload.py)

USAGE:
    # dry-run 100 ไฟล์ (ไม่อัปโหลด ไม่อัปเดต DB)
    python migrate_pdfs_to_storage.py --dry-run --limit 100

    # อัปโหลดจริง batch ละ 500 ไฟล์ (skip ไฟล์ที่ match ไม่ได้)
    python migrate_pdfs_to_storage.py --limit 500

    # อัปโหลดทั้งหมด รวม unmatched
    python migrate_pdfs_to_storage.py --upload-all

    # เริ่มใหม่ ไม่ใช้ progress เดิม
    python migrate_pdfs_to_storage.py --no-resume
"""
import os, re, sys, json, time, uuid, argparse
from pathlib import Path
from dotenv import load_dotenv
load_dotenv()

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

PDF_FOLDER    = r"C:\Users\Administrator\Desktop\New folder\BabyPreechar"
PROGRESS_FILE = str(Path(__file__).parent / "migrate_storage_progress.json")
BUCKET_NAME   = "policy-pdfs"

# ── matching logic (decoupled from Drive) ───────────────────────
from services.filename_matcher import parse_filename, find_matches, load_all_policies
from supabase import create_client


def get_sb():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def safe_storage_name(filename: str) -> str:
    """Storage key ต้องเป็น ASCII (Supabase requirement)
    ดึงตัวเลข + ASCII จาก filename + UUID สั้น
    เช่น 'กก5226 พรบ66.pdf'       → '5226_66_a3f2c1.pdf'
         '104-10ถนนผดุง กธ55.PDF' → '104-10_55_b7d9e2.pdf'
         'จ8920 กธ.57.pdf'        → '8920_57_c4e1a5.pdf'
    """
    stem = os.path.splitext(filename)[0]
    ext  = os.path.splitext(filename)[1].lower() or ".pdf"
    # ดึงเฉพาะ ASCII (digits, letters, -, _)
    parts = re.findall(r'[A-Za-z0-9\-]+', stem)
    base  = '_'.join(parts)[:60] if parts else ''
    short = uuid.uuid4().hex[:6]
    return f"{base}_{short}{ext}" if base else f"{short}{ext}"


def upload_to_storage(sb, file_bytes: bytes, filename: str) -> str | None:
    """อัปโหลดไป Supabase Storage → คืน public URL"""
    try:
        unique_name  = safe_storage_name(filename)
        storage_path = f"policies/{unique_name}"

        sb.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": "application/pdf"},
        )

        url_res = sb.storage.from_(BUCKET_NAME).get_public_url(storage_path)
        if isinstance(url_res, str):
            return url_res
        return url_res.get("publicUrl") or url_res.get("publicURL") or str(url_res)

    except Exception as e:
        print(f"[storage] WARNING: upload {filename[:40]} ไม่สำเร็จ: {e}")
        return None


def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "uploaded": 0, "matched_files": 0, "matched_records": 0,
            "no_match": [], "errors": []}


def save_progress(p):
    """atomic write + retry กัน Windows file lock"""
    tmp = PROGRESS_FILE + ".tmp"
    # retry write tmp file (อาจถูก lock จากรอบก่อน)
    for attempt in range(5):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)
            break
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.3 * (attempt + 1))

    # remove + rename — retry ถ้า lock
    for attempt in range(5):
        try:
            if os.path.exists(PROGRESS_FILE):
                os.remove(PROGRESS_FILE)
            os.rename(tmp, PROGRESS_FILE)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.5 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="ไม่อัปโหลด, ไม่อัปเดต DB — แค่ทดสอบ matching")
    ap.add_argument("--limit", type=int, default=0,
                    help="จำกัดจำนวนไฟล์ (0 = ทั้งหมด)")
    ap.add_argument("--no-resume", action="store_true",
                    help="เริ่มใหม่ ไม่ใช้ progress เดิม")
    ap.add_argument("--upload-all", action="store_true",
                    help="upload ทั้งหมดรวม unmatched (default = skip unmatched)")
    ap.add_argument("--max-matches", type=int, default=5,
                    help="ถ้า match > N records → upload แต่ไม่ update DB (default 5)")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  migrate_pdfs_to_storage.py {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"  Target: Supabase Storage bucket '{BUCKET_NAME}'")
    print(f"{'='*65}\n")

    if not os.path.isdir(PDF_FOLDER):
        print(f"ERROR: ไม่พบ folder {PDF_FOLDER}")
        sys.exit(1)

    all_files = sorted([f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")])

    progress = ({"done": [], "uploaded": 0, "matched_files": 0, "matched_records": 0,
                 "no_match": [], "errors": []}
                if args.no_resume else load_progress())
    done_set = set(progress["done"])

    pending = [f for f in all_files if f not in done_set]
    if args.limit:
        pending = pending[:args.limit]

    print(f"ไฟล์ทั้งหมด : {len(all_files):,}")
    print(f"ทำแล้ว     : {len(done_set):,}")
    print(f"จะทำรอบนี้  : {len(pending):,}\n")

    if not pending:
        print("ไม่มีไฟล์รอทำ")
        return

    sb = get_sb()
    print("โหลด policies จาก Supabase...")
    policies = load_all_policies(sb)
    print(f"โหลด policies: {len(policies):,} records\n")

    kind_count  = {"plate": 0, "address": 0, "name": 0, "unknown": 0}
    match_count = {"plate": 0, "address": 0, "name": 0, "unknown": 0}

    for i, filename in enumerate(pending, 1):
        try:
            parsed = parse_filename(filename)
            kind_count[parsed["kind"]] += 1

            matched_ids = find_matches(parsed, policies)
            if matched_ids:
                match_count[parsed["kind"]] += 1

            short_name = filename[:40].ljust(40)
            year_str   = f"y{parsed['year_be']}" if parsed['year_be'] else "y? "
            mc         = len(matched_ids)
            ambiguous  = mc > args.max_matches
            status_tag = "ambig" if ambiguous else parsed['kind']
            status     = f"{status_tag:7s} {year_str} {mc:>3} match"

            # ถ้า ambiguous → upload PDF แต่ไม่ update DB
            if ambiguous:
                matched_ids = []
                progress.setdefault("ambiguous", []).append({
                    "file": filename, "kind": parsed["kind"], "key": parsed["key"],
                    "count": mc,
                })

            if args.dry_run:
                print(f"[{i}/{len(pending)}] {short_name} → {status}")
                if not matched_ids and parsed["kind"] != "unknown":
                    progress["no_match"].append({"file": filename, "kind": parsed["kind"], "key": parsed["key"]})
                progress["done"].append(filename)
                continue

            if not matched_ids and not args.upload_all:
                progress["no_match"].append({"file": filename, "kind": parsed["kind"], "key": parsed["key"]})
                progress["done"].append(filename)
                save_progress(progress)
                print(f"[{i}/{len(pending)}] {short_name} → {status} ⊘ skip")
                continue

            filepath   = os.path.join(PDF_FOLDER, filename)
            file_bytes = Path(filepath).read_bytes()
            pdf_url    = upload_to_storage(sb, file_bytes, filename)

            if not pdf_url:
                err = f"{filename}: upload failed"
                progress["errors"].append(err)
                save_progress(progress)
                print(f"[{i}/{len(pending)}] ERROR upload {filename[:40]}")
                continue

            progress["uploaded"] += 1

            if matched_ids:
                sb.table("insurance_policies").update({
                    "pdf_url": pdf_url,
                    "pdf_filename": filename,
                    "pdf_size": len(file_bytes),
                }).in_("id", matched_ids).execute()
                progress["matched_files"] += 1
                progress["matched_records"] += len(matched_ids)

            progress["done"].append(filename)
            save_progress(progress)

            print(f"[{i}/{len(pending)}] {short_name} → {status} {'✓ uploaded' if matched_ids else '↑ uploaded (no match)'}")
            time.sleep(0.2)

        except Exception as e:
            err = f"{filename}: {str(e)[:200]}"
            progress["errors"].append(err)
            print(f"[{i}/{len(pending)}] ERROR {err}")
            save_progress(progress)
            time.sleep(1)

    if not args.dry_run:
        save_progress(progress)

    print(f"\n{'='*65}")
    print(f"  สรุปประเภทไฟล์")
    print(f"{'='*65}")
    for k, c in kind_count.items():
        mc = match_count[k]
        rate = (mc / c * 100) if c > 0 else 0
        print(f"  {k:10s}: {c:>5,} ไฟล์   match: {mc:>5,} ({rate:5.1f}%)")
    print(f"\n  รวม match files   : {sum(match_count.values()):,}/{sum(kind_count.values()):,}")
    if not args.dry_run:
        print(f"  รวม update records: {progress['matched_records']:,}")
        print(f"  upload สำเร็จ     : {progress['uploaded']:,}")
        print(f"  errors            : {len(progress['errors']):,}")
    print(f"  ambiguous (>{args.max_matches} match) : {len(progress.get('ambiguous', [])):,}")
    print(f"  no match           : {len(progress.get('no_match', [])):,}")


if __name__ == "__main__":
    main()
