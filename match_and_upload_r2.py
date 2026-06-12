"""
match_and_upload_r2.py  (v2 — type-aware matcher + batch + idempotent)

USAGE:
    python match_and_upload_r2.py --dry-run --limit 20
    python match_and_upload_r2.py --limit 500            # batch ละ 500 (default)
    python match_and_upload_r2.py                        # ทั้งหมด แต่ค่อย ๆ ทำ
    python match_and_upload_r2.py --no-resume            # เริ่มใหม่ ลบ progress

ใช้ filename_matcher_v2 → type-aware ตาม Baby78 convention
  - กธ/พรบ + plate          → M / P
  - ที่อยู่                  → FIRE / ASSET / IAR / BURGLAR / MISC / 3RD / PUBLIC
  - ชื่อ                     → PA / TA / MARINE / GOLF

ทุก batch: save progress disk ทุก 10 ไฟล์ → resume ได้ทุกเมื่อ
ตอนจบ: export no-match list → match_r2_nomatch.json (สำหรับ manual review)
"""
import os, json, time, re, sys, argparse, uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from services.supabase_shim import create_client
from services.filename_matcher_v2 import (
    parse_filename, find_matches_v2, load_policies_with_type,
)


PDF_FOLDER    = r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan"
PROGRESS_FILE = str(Path(__file__).parent / "match_r2_progress.json")
NOMATCH_FILE  = str(Path(__file__).parent / "match_r2_nomatch.json")
BUCKET        = os.getenv("R2_BUCKET", "insurance-pdfs")
R2_PUB        = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
MIN_YEAR_BE   = 2567

# ── doc_type detection ───────────────────────────────────────────────
DOC_TYPE_PATTERNS = [
    (r"พรบ|พ\.?ร\.?บ\.?|prb|compulsory", "prb"),
    (r"สลักหลัง|สลัก|endorsement",        "endorsement"),
    (r"กธ|กรมธรรม",                       "main"),
]
SKIP_PATTERNS = [
    r"ใบแจ้งหนี้", r"บัตรเครดิต", r"บัตรประชาชน",
    r"ใบขับขี่", r"ตรวจสภาพ", r"ทะเบียนรถ",
    r"หนังสือมอบ", r"หนังสือรับรอง", r"แต่งตั้งนายหน้า",
    r"คำขอ", r"ปฎิเสธ", r"เรียกร้อง", r"สินไหม",
    r"\.xls", r"\.jpg", r"\.JPG", r"\.jpeg", r"\.png",
]
OTHER_PATTERNS = [r"ยกเลิก", r"แก้ไข", r"ชำระเบี้ย", r"เอกสาร"]


def detect_doc_type(filename: str) -> str | None:
    for pat in SKIP_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE): return None
    for pat, dt in DOC_TYPE_PATTERNS:
        if re.search(pat, filename, re.IGNORECASE): return dt
    for pat in OTHER_PATTERNS:
        if re.search(pat, filename): return "other"
    return None


# ── R2 upload ────────────────────────────────────────────────────────
_s3 = None
def get_s3():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.client import Config
        _s3 = boto3.client(
            "s3",
            endpoint_url=os.getenv("R2_ENDPOINT"),
            aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
            config=Config(signature_version="s3v4"),
            region_name="auto",
        )
    return _s3


_ILLEGAL_FS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def make_storage_key(filename: str) -> str:
    """Storage key — ใช้ชื่อไทยตรง ๆ + uuid suffix กัน collision"""
    fname = _ILLEGAL_FS.sub('', filename).strip()
    if not fname: fname = "unknown.pdf"
    if not fname.lower().endswith(".pdf"):
        fname += ".pdf"
    stem, ext = fname.rsplit(".", 1)
    short = uuid.uuid4().hex[:6]
    return f"policies/{stem}_{short}.{ext}"


def upload_to_r2(file_bytes: bytes, storage_key: str) -> str:
    get_s3().put_object(
        Bucket=BUCKET, Key=storage_key, Body=file_bytes,
        ContentType="application/pdf",
    )
    return f"{R2_PUB}/{storage_key}"


# ── DB ops ───────────────────────────────────────────────────────────
def update_main_pdf(sb, policy_id, pdf_url, filename, size):
    sb.table("insurance_policies").update({
        "pdf_url": pdf_url, "pdf_filename": filename, "pdf_size": size,
    }).eq("id", policy_id).execute()


def insert_attachment(sb, policy_id, doc_type, pdf_url, filename, size, label=None):
    sb.table("policy_attachments").insert({
        "policy_id": policy_id, "doc_type": doc_type,
        "label": label or "", "note": "",
        "pdf_url": pdf_url, "pdf_filename": filename, "pdf_size": size,
    }).execute()


def attachment_exists(sb, policy_id, filename) -> bool:
    """กัน duplicate ตอน resume — เช็คชื่อไฟล์ว่าเคย insert แล้วยัง"""
    res = sb.table("policy_attachments").select("id")\
        .eq("policy_id", policy_id).eq("pdf_filename", filename).execute()
    return bool(res.data)


# ── progress ─────────────────────────────────────────────────────────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done": [], "uploaded": 0, "matched_files": 0,
            "no_match": [], "errors": [], "skipped": []}


def save_progress(p):
    """Atomic save with retry — Windows can briefly lock the file (AV scanner etc.)"""
    tmp = PROGRESS_FILE + ".tmp"
    for attempt in range(5):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(p, f, ensure_ascii=False, indent=2)
            if os.path.exists(PROGRESS_FILE):
                for _ in range(5):
                    try:
                        os.remove(PROGRESS_FILE); break
                    except PermissionError:
                        time.sleep(0.3)
            os.rename(tmp, PROGRESS_FILE)
            return
        except PermissionError:
            time.sleep(0.5 + attempt * 0.3)
        except Exception:
            time.sleep(0.3)
    # last resort: best-effort direct write
    try:
        with open(PROGRESS_FILE, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [warn] save_progress failed: {e}")


# ── helpers ──────────────────────────────────────────────────────────
def split_by_type(matched_ids, by_id, want_type):
    in_t  = [i for i in matched_ids if (by_id.get(i, {}).get("policy_type") or "").upper() == want_type]
    out_t = [i for i in matched_ids if i not in in_t]
    return in_t, out_t


# ── main ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run",   action="store_true")
    ap.add_argument("--limit",     type=int, default=500, help="batch size (default 500, 0=unlimited)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--folder",    default=PDF_FOLDER)
    ap.add_argument("--include-all-years", action="store_true")
    args = ap.parse_args()

    print(f"\n{'='*70}")
    print(f"  match_and_upload_r2.py v2  {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"  PDF folder : {args.folder}")
    print(f"  Min year   : {'none' if args.include_all_years else f'พ.ศ. {MIN_YEAR_BE}'}")
    print(f"  Batch size : {args.limit if args.limit else 'unlimited'}")
    print(f"{'='*70}\n")

    all_files = sorted([f for f in os.listdir(args.folder) if f.lower().endswith(".pdf")])
    print(f"Total PDFs in folder : {len(all_files):,}")

    progress = {"done": [], "uploaded": 0, "matched_files": 0,
                "no_match": [], "errors": [], "skipped": []} if args.no_resume else load_progress()
    done_set = set(progress["done"])
    print(f"Already processed    : {len(done_set):,}")

    pending = [f for f in all_files if f not in done_set]
    if args.limit and args.limit > 0:
        pending = pending[:args.limit]
    print(f"Pending this batch   : {len(pending):,}\n")

    if not pending:
        print("ไม่มีไฟล์รอทำในรอบนี้")
        export_nomatch(progress)
        return

    sb = create_client()
    print("Loading policies from DB...")
    policies = load_policies_with_type(sb)
    by_id = {p["id"]: p for p in policies}
    print(f"Loaded {len(policies):,} policies\n")

    stats = {
        "main_ok": 0, "prb_ok": 0, "endorsement_ok": 0, "other_ok": 0,
        "skip_filetype": 0, "skip_year": 0, "no_match": 0, "errors": 0,
        "dup_attachment_skipped": 0,
    }

    for i, filename in enumerate(pending, 1):
        try:
            doc_type = detect_doc_type(filename)
            parsed = parse_filename(filename)
            if doc_type is None:
                if parsed["kind"] in ("plate", "address", "name"):
                    doc_type = "main"
                else:
                    stats["skip_filetype"] += 1
                    progress["skipped"].append({"file": filename, "reason": "filetype"})
                    progress["done"].append(filename)
                    continue

            year_be = parsed.get("year_be")
            if not args.include_all_years and year_be and year_be < MIN_YEAR_BE:
                stats["skip_year"] += 1
                progress["skipped"].append({"file": filename, "reason": f"year={year_be}"})
                progress["done"].append(filename)
                continue

            result = find_matches_v2(parsed, policies, strict_type=True, best_only=True)
            matched = result["matched_ids"]
            # idempotency check: ถ้าเป็น main + ทุก matched record มี pdf_url แล้ว → skip
            #                    ถ้าเป็น attachment + ทุก matched มี attachment ชื่อนี้แล้ว → skip
            if matched and doc_type == "main":
                all_linked = all(by_id[pid].get("pdf_url") for pid in matched if pid in by_id)
                if all_linked:
                    stats["skip_already_linked"] = stats.get("skip_already_linked", 0) + 1
                    progress["done"].append(filename)
                    continue
            if matched and doc_type in ("prb", "endorsement", "other"):
                # quick check ก่อนอัป R2 — ถ้า attachment ชื่อนี้มีอยู่แล้วทุก policy → skip
                if all(attachment_exists(sb, pid, filename) for pid in matched):
                    stats["skip_already_attached"] = stats.get("skip_already_attached", 0) + 1
                    progress["done"].append(filename)
                    continue
            if not matched:
                stats["no_match"] += 1
                progress["no_match"].append({
                    "file": filename, "kind": parsed["kind"],
                    "key": parsed["key"], "year": year_be,
                    "doc_type": doc_type,
                    "candidates_total": result.get("candidates_total", 0),
                })
                progress["done"].append(filename)
                continue

            short = filename[:45].ljust(45)
            if args.dry_run:
                print(f"[{i}/{len(pending)}] {short} → {doc_type:5s} y{year_be or '?'} {len(matched)}m  (dry)")
                progress["done"].append(filename)
                stats[f"{doc_type}_ok"] += 1
                continue

            # ── upload ──
            filepath   = os.path.join(args.folder, filename)
            file_bytes = Path(filepath).read_bytes()
            storage_key = make_storage_key(filename)
            pdf_url = upload_to_r2(file_bytes, storage_key)
            progress["uploaded"] += 1

            if doc_type == "main":
                for pid in matched:
                    update_main_pdf(sb, pid, pdf_url, filename, len(file_bytes))
                stats["main_ok"] += 1

            elif doc_type == "prb":
                in_p, _   = split_by_type(matched, by_id, "P")
                in_m, _   = split_by_type(matched, by_id, "M")
                # main of P
                for pid in in_p:
                    update_main_pdf(sb, pid, pdf_url, filename, len(file_bytes))
                # attachment on M (skip if dup)
                for pid in in_m:
                    if not attachment_exists(sb, pid, filename):
                        insert_attachment(sb, pid, "prb", pdf_url, filename, len(file_bytes),
                                          label=f"พ.ร.บ. {year_be or ''}".strip())
                    else:
                        stats["dup_attachment_skipped"] += 1
                if not in_p and not in_m and matched:
                    if not attachment_exists(sb, matched[0], filename):
                        insert_attachment(sb, matched[0], "prb", pdf_url, filename, len(file_bytes),
                                          label=f"พ.ร.บ. {year_be or ''}".strip())
                stats["prb_ok"] += 1

            elif doc_type == "endorsement":
                if not attachment_exists(sb, matched[0], filename):
                    insert_attachment(sb, matched[0], "endorsement", pdf_url, filename, len(file_bytes),
                                      label=f"สลักหลัง {year_be or ''}".strip())
                else:
                    stats["dup_attachment_skipped"] += 1
                stats["endorsement_ok"] += 1

            else:   # other
                if not attachment_exists(sb, matched[0], filename):
                    insert_attachment(sb, matched[0], "other", pdf_url, filename, len(file_bytes),
                                      label=filename[:50])
                else:
                    stats["dup_attachment_skipped"] += 1
                stats["other_ok"] += 1

            progress["matched_files"] += 1
            progress["done"].append(filename)
            print(f"[{i}/{len(pending)}] {short} → {doc_type:5s} y{year_be or '?'} {len(matched)}m  ✓")

            if i % 10 == 0:
                save_progress(progress)

        except KeyboardInterrupt:
            print("\n⛔ Interrupted by user — saving progress")
            save_progress(progress)
            break
        except Exception as e:
            err = f"{filename}: {str(e)[:200]}"
            progress["errors"].append(err)
            stats["errors"] += 1
            print(f"[{i}/{len(pending)}] ERROR {err}")
            save_progress(progress)
            time.sleep(0.3)

    save_progress(progress)

    print(f"\n{'='*70}")
    print(f"  BATCH DONE")
    print(f"{'='*70}")
    for k, v in stats.items():
        print(f"  {k:25s} : {v:>6,}")
    print(f"  total uploaded so far : {progress['uploaded']:,}")
    print(f"  total matched so far  : {progress['matched_files']:,}")
    print(f"  total done so far     : {len(progress['done']):,} / {len(all_files):,}")
    print(f"  no-match accumulated  : {len(progress['no_match']):,}")
    print(f"  errors accumulated    : {len(progress['errors']):,}")

    remaining = len(all_files) - len(progress['done'])
    print(f"\n  Remaining files       : {remaining:,}")
    if remaining > 0:
        print(f"  → Run again to continue: python match_and_upload_r2.py --limit {args.limit}")

    export_nomatch(progress)


def export_nomatch(progress):
    """Export no-match list สำหรับ manual review"""
    if not progress.get("no_match"): return
    with open(NOMATCH_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "total": len(progress["no_match"]),
            "files": progress["no_match"],
        }, f, ensure_ascii=False, indent=2)
    print(f"\n  → No-match list saved: {NOMATCH_FILE}  ({len(progress['no_match'])} files)")


if __name__ == "__main__":
    main()
