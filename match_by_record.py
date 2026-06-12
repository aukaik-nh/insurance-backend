"""
match_by_record.py — record-driven matcher (per Baby78 convention)

Logic: สำหรับแต่ละ DB record → คำนวณ filename ที่คาดไว้ → ไปหาใน BabyScan
       (ตรงข้ามกับ match_and_upload_r2.py ที่ไล่จากไฟล์)

Baby78 filename conventions (verified):
  policy_type M, P                 → "{plate-no-space} {กธ|พรบ}.{YY}.pdf"
  FIRE/ASSET/IAR/BURGLAR/MISC/...  → "{house-no}{place} กธ.{YY}.pdf"
  PA/TA/MARINE/GOLF                → "{name-no-space} กธ.{YY}.pdf"

YY = ปี พ.ศ. 2-digit จาก coverage_start year

Variants ที่ลอง:
  exact, with-space-before-doctype, no-dot, no-trailing-yy,
  partial-house-no (เลข /  - ทั้งสองแบบ), case variations
"""
import os, json, time, re, sys, argparse, uuid
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).parent))
sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv
load_dotenv()

from services.supabase_shim import create_client


PDF_FOLDER    = r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan"
PROGRESS_FILE = str(Path(__file__).parent / "match_record_progress.json")
NOFILE_FILE   = str(Path(__file__).parent / "match_record_nofile.json")
BUCKET        = os.getenv("R2_BUCKET", "insurance-pdfs")
R2_PUB        = os.getenv("R2_PUBLIC_URL", "").rstrip("/")


# Mapping policy_type → expected filename strategy
PLATE_TYPES   = {"M", "P"}
ADDR_TYPES    = {"FIRE", "ASSET", "IAR", "BURGLAR", "MISC", "3RD", "PUBLIC"}
NAME_TYPES    = {"PA", "TA", "MARINE", "GOLF"}

PRB_TYPES     = {"P"}                # ใช้ "พรบ" ในชื่อไฟล์
MAIN_TYPES    = PLATE_TYPES | ADDR_TYPES | NAME_TYPES  # ทุก type ที่ไม่ใช่ P → "กธ"
                                                       # (แต่ M ก็เป็น "กธ" ด้วย เพราะ "กธ" = กรมธรรม์)


def ad_to_be_yy(ad_year: int) -> str:
    """ค.ศ. 2024 → '67' (พ.ศ. 2 digits)"""
    return str(ad_year + 543)[-2:]


def coverage_start_year(cov_start: str) -> int | None:
    if not cov_start: return None
    try:
        y = int(str(cov_start)[:4])
        return y if y < 2500 else y - 543   # already AD assumed
    except (ValueError, TypeError):
        return None


def doc_type_label(policy_type: str) -> str:
    """Filename uses 'กธ' or 'พรบ' depending on type"""
    return "พรบ" if policy_type.upper() in PRB_TYPES else "กธ"


# ── Expected filename generators ─────────────────────────────────────
def expected_for_plate(plate: str, doc_label: str, yy: str) -> list[str]:
    """M, P: '1กท5022 กธ.65.pdf' / '1กท5022 พรบ.65.pdf'"""
    plate_clean = re.sub(r"\s+", "", plate or "")
    if not plate_clean: return []
    variants = []
    # standard: with space, dot
    variants.append(f"{plate_clean} {doc_label}.{yy}.pdf")
    # extra space:
    variants.append(f"{plate_clean}  {doc_label}.{yy}.pdf")
    # no space before doc_label:
    variants.append(f"{plate_clean}{doc_label}.{yy}.pdf")
    # no dot:
    variants.append(f"{plate_clean} {doc_label}{yy}.pdf")
    return variants


def expected_for_address(address: str, doc_label: str, yy: str) -> list[str]:
    """FIRE/ASSET: extract house# + first place token → '102ซอยเฉลิมพระเกียรติ กธ.67.pdf'"""
    if not address: return []
    addr = address.strip()
    # extract house number (could be '102' or '138/462' or '184/81')
    m = re.match(r"^(\d+(?:[/\-]\d+)?(?:[/\-]\d+)?)\s*", addr)
    if not m: return []
    house = m.group(1)
    rest = addr[m.end():].strip()
    # take place name (first run of non-whitespace Thai/text up to ~25 chars)
    place_m = re.match(r"^([^\s]+(?:\s+[^\s]+){0,3})", rest)
    place = place_m.group(1) if place_m else ""
    place = place[:30].rstrip()
    if not place: return []

    house_dash  = house.replace("/", "-")
    house_slash = house.replace("-", "/")
    house_plain = re.sub(r"[/\-]", "", house)

    variants = []
    for h in [house, house_dash, house_slash, house_plain]:
        # join place without space (Baby78 style)
        place_join = re.sub(r"\s+", "", place)
        variants.append(f"{h}{place_join} {doc_label}.{yy}.pdf")
        variants.append(f"{h}{place_join} {doc_label}{yy}.pdf")
        # with space between house and place
        variants.append(f"{h} {place_join} {doc_label}.{yy}.pdf")
    # dedupe preserving order
    seen = set(); out = []
    for v in variants:
        if v not in seen:
            seen.add(v); out.append(v)
    return out


def expected_for_name(name: str, doc_label: str, yy: str) -> list[str]:
    """PA/TA: 'สุทธิดลฉัตรปรีชากุล กธ.67.pdf'"""
    if not name: return []
    name_clean = re.sub(r"\s+", "", name.strip())
    if not name_clean: return []
    name_with_sp = name.strip()
    variants = [
        f"{name_clean} {doc_label}.{yy}.pdf",
        f"{name_clean} {doc_label}{yy}.pdf",
        f"{name_with_sp} {doc_label}.{yy}.pdf",
    ]
    return variants


def compute_expected_filenames(record: dict) -> list[str]:
    """รวม filename variants ที่ Baby78 น่าจะใช้สำหรับ record นี้"""
    ptype = (record.get("policy_type") or "").upper()
    cov_y = coverage_start_year(record.get("coverage_start"))
    if not cov_y: return []
    yy = ad_to_be_yy(cov_y)
    doc_label = doc_type_label(ptype)

    if ptype in PLATE_TYPES:
        return expected_for_plate(record.get("license_plate", ""), doc_label, yy)
    if ptype in ADDR_TYPES:
        return expected_for_address(record.get("insured_address", ""), doc_label, yy)
    if ptype in NAME_TYPES:
        return expected_for_name(record.get("insured_name", ""), doc_label, yy)
    return []


# ── Folder index — 1-time scan, O(1) lookup ──────────────────────────
def build_folder_index(folder: str) -> dict[str, str]:
    """ดัชนี folder: normalized_name → actual_filename
    normalize: lowercase + strip spaces"""
    idx = {}
    for f in os.listdir(folder):
        if not f.lower().endswith(".pdf"): continue
        # store multiple normalize variants for fuzzy lookup
        norm = re.sub(r"\s+", "", f).lower()
        idx[norm] = f
    return idx


def find_file_in_folder(expected_names: list[str], folder_idx: dict[str, str]) -> str | None:
    """หา filename จริงใน folder ที่ตรงกับ expected (normalize lookup)"""
    for name in expected_names:
        norm = re.sub(r"\s+", "", name).lower()
        if norm in folder_idx:
            return folder_idx[norm]
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


# ── progress ─────────────────────────────────────────────────────────
def load_progress():
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"done_ids": [], "uploaded": 0, "no_file": [], "errors": []}


def save_progress(p):
    tmp = PROGRESS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(p, f, ensure_ascii=False, indent=2)
    if os.path.exists(PROGRESS_FILE):
        try: os.remove(PROGRESS_FILE)
        except PermissionError:
            time.sleep(0.5); os.remove(PROGRESS_FILE)
    os.rename(tmp, PROGRESS_FILE)


# ── main ─────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit",   type=int, default=0, help="limit records to process this run (0=all)")
    ap.add_argument("--no-resume", action="store_true")
    ap.add_argument("--folder", default=PDF_FOLDER)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-upload even if record already has pdf_url")
    args = ap.parse_args()

    print(f"\n{'='*70}")
    print(f"  match_by_record.py  {'(DRY-RUN)' if args.dry_run else ''}")
    print(f"  Folder    : {args.folder}")
    print(f"  Overwrite : {args.overwrite}")
    print(f"{'='*70}\n")

    print("Building folder index...")
    folder_idx = build_folder_index(args.folder)
    print(f"  indexed {len(folder_idx):,} PDFs\n")

    sb = create_client()
    print("Loading policies from DB...")
    records = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, policy_type, license_plate, insured_address, insured_name, "
            "coverage_start, pdf_url, pdf_filename"
        ).range(page*1000, page*1000+999).execute()
        if not r.data: break
        records.extend(r.data)
        if len(r.data) < 1000: break
        page += 1
    print(f"  loaded {len(records):,} policies\n")

    progress = {"done_ids": [], "uploaded": 0, "no_file": [], "errors": []} if args.no_resume else load_progress()
    done_set = set(progress["done_ids"])

    # filter records: skip done, skip ones already with pdf_url (unless --overwrite)
    pending = []
    for r in records:
        if r["id"] in done_set: continue
        if not args.overwrite and r.get("pdf_url"): continue
        pending.append(r)

    if args.limit and args.limit > 0:
        pending = pending[:args.limit]

    print(f"Records to process this run: {len(pending):,}\n")

    if not pending:
        print("No records to process")
        return

    stats = defaultdict(int)

    for i, rec in enumerate(pending, 1):
        try:
            expected = compute_expected_filenames(rec)
            if not expected:
                stats["no_expected"] += 1
                progress["no_file"].append({
                    "id": rec["id"], "type": rec["policy_type"],
                    "reason": "no expected filename (missing type/year/key)",
                })
                progress["done_ids"].append(rec["id"])
                continue

            found = find_file_in_folder(expected, folder_idx)
            if not found:
                stats["no_file"] += 1
                progress["no_file"].append({
                    "id": rec["id"], "type": rec["policy_type"],
                    "expected": expected[:3],   # save first 3 variants
                    "plate":  rec.get("license_plate"),
                    "name":   rec.get("insured_name"),
                    "addr":   (rec.get("insured_address") or "")[:60],
                })
                progress["done_ids"].append(rec["id"])
                continue

            short_id  = rec["id"][:8]
            ptype     = rec["policy_type"]
            short_exp = expected[0][:50].ljust(50)

            if args.dry_run:
                print(f"[{i}/{len(pending)}] {ptype:6s} {short_id} → {short_exp} → {found[:30]}  (dry)")
                progress["done_ids"].append(rec["id"])
                stats[f"{ptype}_match"] += 1
                continue

            # upload
            filepath  = os.path.join(args.folder, found)
            file_bytes = Path(filepath).read_bytes()
            storage_key = make_storage_key(found)
            pdf_url = upload_to_r2(file_bytes, storage_key)

            sb.table("insurance_policies").update({
                "pdf_url": pdf_url,
                "pdf_filename": found,
                "pdf_size": len(file_bytes),
            }).eq("id", rec["id"]).execute()

            progress["uploaded"] += 1
            progress["done_ids"].append(rec["id"])
            stats[f"{ptype}_uploaded"] += 1
            print(f"[{i}/{len(pending)}] {ptype:6s} {short_id} → {found[:50]}  ✓")

            if i % 10 == 0: save_progress(progress)

        except KeyboardInterrupt:
            print("\n⛔ Interrupted")
            save_progress(progress)
            break
        except Exception as e:
            err = f"{rec.get('id', '?')[:8]}: {str(e)[:150]}"
            progress["errors"].append(err)
            stats["errors"] += 1
            print(f"[{i}/{len(pending)}] ERROR {err}")
            save_progress(progress)
            time.sleep(0.3)

    save_progress(progress)

    print(f"\n{'='*70}")
    print(f"  DONE")
    print(f"{'='*70}")
    for k, v in sorted(stats.items()):
        print(f"  {k:25s} : {v:>6,}")
    print(f"\n  total uploaded so far : {progress['uploaded']:,}")
    print(f"  total done            : {len(progress['done_ids']):,} / {len(records):,}")
    print(f"  no-file accumulated   : {len(progress['no_file']):,}")
    print(f"  errors accumulated    : {len(progress['errors']):,}")

    if progress["no_file"]:
        with open(NOFILE_FILE, "w", encoding="utf-8") as f:
            json.dump({"total": len(progress["no_file"]), "files": progress["no_file"]},
                      f, ensure_ascii=False, indent=2)
        print(f"\n  → No-file list: {NOFILE_FILE}")


if __name__ == "__main__":
    main()
