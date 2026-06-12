"""Audit ด้วย matcher v2 (type-aware) — read-only"""
import os, sys, json, re
from pathlib import Path
from collections import Counter

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from services.supabase_shim import create_client
from services.filename_matcher_v2 import (
    parse_filename, find_matches_v2, load_policies_with_type
)

PDF_FOLDER  = r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan"
MIN_YEAR_BE = 2567


# doc_type detection (mirror of match_and_upload_r2.py)
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


print("="*70)
print("  PDF Audit v2  (type-aware matcher — Baby78 convention)")
print("="*70 + "\n")

sb = create_client()
policies = load_policies_with_type(sb)
print(f"DB policies: {len(policies):,}\n")

files = sorted([f for f in os.listdir(PDF_FOLDER) if f.lower().endswith(".pdf")])
print(f"PDF files: {len(files):,}\n")

stats = Counter()
strategy_count = Counter()
upload_size = 0
no_match_samples = []
fallback_samples = []
upload_by_type = Counter()

for fname in files:
    fpath = os.path.join(PDF_FOLDER, fname)
    try: fsize = os.path.getsize(fpath)
    except OSError: fsize = 0

    doc_type = detect_doc_type(fname)
    if doc_type is None:
        parsed_chk = parse_filename(fname)
        if parsed_chk["kind"] in ("plate", "address", "name"):
            doc_type = "main"
        else:
            stats["skip_filetype"] += 1
            continue

    parsed = parse_filename(fname)
    year_be = parsed.get("year_be")

    # year filter
    if year_be and year_be < MIN_YEAR_BE:
        stats["skip_year"] += 1
        continue

    # type-aware match — strict_type=True, fallback ปิด
    result = find_matches_v2(parsed, policies, strict_type=True, best_only=True)

    stats[f"kind_{parsed['kind']}"] += 1
    strategy_count[result["strategy"]] += 1

    if not result["matched_ids"]:
        stats[f"{doc_type}_no_match"] += 1
        if len(no_match_samples) < 30:
            no_match_samples.append({
                "file": fname,
                "kind": parsed["kind"],
                "key": parsed["key"],
                "year": year_be,
                "candidates_total": result["candidates_total"],
                "candidates_type_matched": result["candidates_type_matched"],
            })
        continue

    stats[doc_type] += 1
    upload_size += fsize
    upload_by_type[doc_type] += 1

print("— Summary —")
print(f"{'main':25s} : {stats['main']:>6,}")
print(f"{'prb':25s} : {stats['prb']:>6,}")
print(f"{'endorsement':25s} : {stats['endorsement']:>6,}")
print(f"{'other':25s} : {stats['other']:>6,}")
print(f"{'main_no_match':25s} : {stats['main_no_match']:>6,}")
print(f"{'prb_no_match':25s} : {stats['prb_no_match']:>6,}")
print(f"{'endorsement_no_match':25s} : {stats['endorsement_no_match']:>6,}")
print(f"{'other_no_match':25s} : {stats['other_no_match']:>6,}")
print(f"{'skip_year':25s} : {stats['skip_year']:>6,}")
print(f"{'skip_filetype':25s} : {stats['skip_filetype']:>6,}")

print(f"\n— By detected kind —")
for k in ("kind_plate", "kind_address", "kind_name", "kind_unknown"):
    print(f"  {k:25s} : {stats[k]:>6,}")

print(f"\n— Matching strategy —")
for s, c in strategy_count.items():
    print(f"  {s:25s} : {c:>6,}")

print(f"\n— Upload prediction —")
total_match = sum(upload_by_type.values())
print(f"  files to upload   : {total_match:,}")
print(f"  size              : {upload_size/(1024**2):.1f} MB ({upload_size/(1024**3):.2f} GB)")

print(f"\n— No-match samples (first 20) —")
for s in no_match_samples[:20]:
    print(f"  [{s['kind']:8s} y{s['year'] or '?'}] key='{s['key']}' "
          f"cands={s['candidates_total']} (type-match={s['candidates_type_matched']}) "
          f"file={s['file'][:50]}")

# write report
report = {
    "stats": dict(stats),
    "strategy": dict(strategy_count),
    "upload_count": total_match,
    "upload_size_bytes": upload_size,
    "no_match_samples": no_match_samples,
}
out = Path(__file__).parent / "audit_v2_report.json"
with open(out, "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
print(f"\nFull report: {out}")
