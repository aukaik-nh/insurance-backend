"""
find_missing_pdfs.py — หา PDFs ของ 199 records ที่ขาด (STRICT, ไม่เดา)
─────────────────────────────────────────────────────────────────────
สำหรับแต่ละ record ที่ pdf_url IS NULL:
1. Build "ชื่อไฟล์ที่ควรจะเป็น" ตาม Baby78 convention
2. ค้นใน BabyScan ทีละ pattern (ไม่ใช้ fuzzy):
   a. Exact: ชื่อตรง 100%
   b. Plate variants: ลองด้วย/ไม่มี space, มี/ไม่มี province
   c. Address: เปรียบเทียบ normalize prefix (อย่างน้อย 10 chars ตรง)
   d. Name: คำนำหน้า + ชื่อ + นามสกุล ตรงกัน
3. รายงาน 3 ระดับ:
   - HIGH: confident — auto-upload
   - LOW: possible match — list ให้ user ดู (ไม่ upload)
   - NONE: หาไม่เจอ — list สำหรับ user manual

USAGE:
    python scripts/find_missing_pdfs.py            # report เท่านั้น (ไม่ update DB)
    python scripts/find_missing_pdfs.py --apply    # auto-upload HIGH matches
"""
import os, sys, re, csv, argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict, Counter
from urllib.parse import quote

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import psycopg2
import psycopg2.extras
import boto3
from botocore.client import Config


SCAN_DIR = Path(r"C:\Users\Administrator\Desktop\ใหม่10062026\BabyScan")
ADDR_MAP = Path(r"D:\tmp\addr_map_67_69.csv")
REPORT_CSV = Path(r"D:\tmp\missing_pdfs_report.csv")


def be_year(s):
    if not s: return None
    try:
        y = int(str(s)[:4])
        return (y + 543) % 100
    except: return None


def addr_normalize(s):
    if not s: return ""
    return re.sub(r'[\s\-/.,()฿\d]+', '', str(s)).strip()


def addr_normalize_keep_digits(s):
    """แบบเก็บตัวเลขด้วย (เช่นเลขบ้าน)"""
    if not s: return ""
    return re.sub(r'[\s\-/.,()]+', '', str(s)).strip()


def name_normalize(s):
    """ชื่อคน: ลบคำนำหน้า + ตัดท้าย"""
    if not s: return ""
    s = re.sub(r'^(นาย|นาง|นางสาว|น\.ส\.|ด\.ช\.|ด\.ญ\.|บจก\.|บมจ\.|หจก\.|บริษัท|ห้างหุ้นส่วน)\s*', '', s)
    return re.sub(r'[\s\-/.,()]+', '', s).strip()


def plate_compact(p):
    if not p: return ""
    return re.sub(r'\s+', '', p).strip()


def be_year_from_filename(stem):
    m = re.search(r'(?:กธ|พรบ|พรล|PA|พ\.ร\.บ|ภ\.ค)\.?\s*(\d{2})(?:\D|$)', stem)
    if not m: m = re.search(r'\s(\d{2})(?:\.|\s|$|_|\))', stem)
    if m:
        try: return int(m.group(1))
        except: pass
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="upload HIGH confidence matches")
    args = ap.parse_args()

    print(f"\n{'='*65}")
    print(f"  FIND MISSING PDFs (STRICT) — {datetime.now().strftime('%H:%M:%S')}")
    print(f"{'='*65}\n")

    # ── 1. Load addr_map ───────────────────────────
    addr_map = {}
    if ADDR_MAP.exists():
        with ADDR_MAP.open(encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                app = (r.get("app") or "").strip()
                if app: addr_map[app] = r
    print(f"[1] addr_map: {len(addr_map):,} records")

    # ── 2. Get records without PDF ──────────────────
    url = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    conn = psycopg2.connect(url)
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("""
        SELECT id, app_number, policy_number, policy_type, license_plate,
               insured_name, insured_address, coverage_start, coverage_end
        FROM insurance_policies
        WHERE pdf_url IS NULL
        ORDER BY policy_type, coverage_end DESC
    """)
    records = [dict(r) for r in cur.fetchall()]
    print(f"[2] missing records: {len(records):,}")

    # ── 3. Index BabyScan: หลายแบบ ───────────────────
    print(f"[3] index BabyScan...")
    addr_idx_15 = defaultdict(list)  # normalized address 15 char prefix
    addr_idx_10 = defaultdict(list)  # 10 char prefix
    name_idx = defaultdict(list)
    plate_idx = defaultdict(list)
    all_files = []

    plate_pat = re.compile(r'^((?:\d{1,2})?[ก-ฮ]{1,2}\d{1,5})')
    prb_pat = re.compile(r'พรบ|พรล|พ\.ร\.บ')

    for p in SCAN_DIR.iterdir():
        if not p.is_file() or p.suffix.lower() not in (".pdf", ".jpg", ".jpeg"): continue
        stem = p.stem
        yr = be_year_from_filename(stem)
        is_prb = bool(prb_pat.search(stem))
        all_files.append((p, yr, is_prb))

        pm = plate_pat.match(stem)
        if pm:
            plate_idx[pm.group(1)].append((p, yr, is_prb))
            continue

        # non-plate → assume address or name
        # strip year/category suffix
        clean = re.sub(r'\s*(?:กธ|พรบ|พรล|PA|พ\.ร\.บ|ภ\.ค)\..*$|\s*\(.*$|\s+\d{2}.*$', '', stem).strip()
        # try as address (keep digits)
        norm_addr = addr_normalize_keep_digits(clean)
        if norm_addr:
            if len(norm_addr) >= 15: addr_idx_15[norm_addr[:15]].append((p, yr, is_prb, clean))
            if len(norm_addr) >= 10: addr_idx_10[norm_addr[:10]].append((p, yr, is_prb, clean))
        # try as name (no digits)
        norm_name = name_normalize(clean)
        if norm_name and len(norm_name) >= 6:
            name_idx[norm_name[:15]].append((p, yr, is_prb, clean))
    print(f"    addr keys: {len(addr_idx_10):,} (10-char), {len(addr_idx_15):,} (15-char)")
    print(f"    name keys: {len(name_idx):,}")
    print(f"    plates: {len(plate_idx):,}")

    # ── 4. Match each missing record ─────────────────
    print(f"\n[4] match...")
    results = []  # (record, confidence, candidates)

    for d in records:
        mdb = addr_map.get((d.get("app_number") or "").strip(), {})
        addr1 = (mdb.get("address1") or "").strip()
        name = (mdb.get("namethai") or "").strip()
        target_yr = be_year(d.get("coverage_end") or d.get("coverage_start"))
        pt = (d.get("policy_type") or "").upper().strip()
        plate = plate_compact(d.get("license_plate"))
        expect_prb = (pt == "P")

        candidates = []  # list of (path, year, is_prb, confidence_score, reason)

        # A. plate exact (อาจ plate = "OTHER" หรือว่างไม่ work)
        if plate and plate not in ("", "OTHER"):
            for k in plate_idx:
                if k == plate:
                    for f in plate_idx[k]:
                        candidates.append((*f, 100, "plate-exact"))
                elif plate.startswith(k) and len(k) >= 5:
                    for f in plate_idx[k]:
                        candidates.append((*f, 80, "plate-prefix"))

        # B. address exact 15-char prefix
        if not candidates and addr1:
            norm = addr_normalize_keep_digits(addr1)
            if len(norm) >= 15:
                k = norm[:15]
                for f in addr_idx_15.get(k, []):
                    candidates.append((f[0], f[1], f[2], 90, f"addr-15:{k[:8]}…"))

        # C. address 10-char prefix
        if not candidates and addr1:
            norm = addr_normalize_keep_digits(addr1)
            if len(norm) >= 10:
                k = norm[:10]
                for f in addr_idx_10.get(k, []):
                    candidates.append((f[0], f[1], f[2], 70, f"addr-10:{k[:8]}…"))

        # D. name match
        if not candidates and name:
            norm = name_normalize(name)
            if norm and len(norm) >= 6:
                for k in name_idx:
                    # ทั้ง key หรือ name ขึ้นต้นด้วยอีกอัน
                    if norm.startswith(k) or k.startswith(norm[:8]):
                        for f in name_idx[k]:
                            candidates.append((f[0], f[1], f[2], 60, f"name:{k[:8]}…"))

        # Filter: ตรง type
        if candidates:
            typed = [c for c in candidates if c[2] == expect_prb]
            if typed: candidates = typed

        # Filter: ตรงปี (boost +20)
        if target_yr is not None and candidates:
            adjusted = []
            for c in candidates:
                p, yr, is_prb, score, reason = c
                if yr == target_yr:
                    adjusted.append((p, yr, is_prb, min(100, score + 20), reason + "+year"))
                else:
                    adjusted.append(c)
            candidates = adjusted

        if not candidates:
            confidence = "NONE"
            best = None
        else:
            candidates.sort(key=lambda c: -c[3])
            best = candidates[0]
            top_score = best[3]
            # ระดับความมั่นใจ
            if top_score >= 100:
                confidence = "HIGH"
            elif top_score >= 80:
                confidence = "MED"
            else:
                confidence = "LOW"

        results.append((d, confidence, best, candidates[:3]))

    # ── 5. รายงาน ──────────────────────────────────
    by_conf = Counter(r[1] for r in results)
    print(f"\n[5] รายงาน:")
    for c, n in by_conf.most_common():
        print(f"  {c}: {n}")

    # CSV report
    with REPORT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["confidence", "policy_type", "license_plate", "insured_name",
                    "address1", "coverage_end", "candidate_file", "reason",
                    "policy_number", "id"])
        for d, conf, best, _ in results:
            mdb = addr_map.get((d.get("app_number") or "").strip(), {})
            w.writerow([
                conf,
                d.get("policy_type"),
                d.get("license_plate"),
                d.get("insured_name"),
                (mdb.get("address1") or "")[:80],
                d.get("coverage_end"),
                best[0].name if best else "",
                best[4] if best else "",
                d.get("policy_number"),
                str(d.get("id")),
            ])
    print(f"\n  รายงาน CSV: {REPORT_CSV}")

    # ── 6. Apply HIGH matches (if --apply) ─────────
    if args.apply:
        high = [r for r in results if r[1] == "HIGH"]
        print(f"\n[6] Upload HIGH matches: {len(high)} records")
        s3 = boto3.client("s3",
            endpoint_url=os.getenv("R2_ENDPOINT"),
            aws_access_key_id=os.getenv("R2_ACCESS_KEY"),
            aws_secret_access_key=os.getenv("R2_SECRET_KEY"),
            config=Config(signature_version="s3v4"))
        bucket = os.getenv("R2_BUCKET")
        r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
        upd = conn.cursor()
        n_done = 0
        for d, conf, best, _ in high:
            pdf_path = best[0]
            # build filename ตาม Baby78 — ใช้ logic เดียวกับ upload_pdfs_v3.py
            pt = (d.get("policy_type") or "").upper()
            yr = be_year(d.get("coverage_end") or d.get("coverage_start"))
            yr_part = f".{yr:02d}" if yr is not None else ""
            if pt == "P":
                fname = f"{plate_compact(d.get('license_plate')) or 'unknown'} พรบ{yr_part}.pdf"
            elif pt == "M":
                fname = f"{plate_compact(d.get('license_plate')) or 'unknown'} กธ{yr_part}.pdf"
            else:
                mdb = addr_map.get((d.get("app_number") or "").strip(), {})
                base = (mdb.get("address1") or d.get("insured_name") or "unknown")[:80]
                base = re.sub(r'[<>:"/\\|?*]', '', base).strip()
                fname = f"{base} กธ{yr_part}.pdf"

            key = f"policies/{fname}"
            # ตรวจ duplicate
            n = 0
            while n < 100:
                try:
                    s3.head_object(Bucket=bucket, Key=key)
                    n += 1
                    stem, ext = fname.rsplit(".", 1)
                    key = f"policies/{stem}_{n:04d}.{ext}"
                except:
                    break
            try:
                data = pdf_path.read_bytes()
                s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType="application/pdf")
                pdf_url = f"{r2_pub}/{quote(key)}"
                final_fname = key.split("/", 1)[1]
                upd.execute("UPDATE insurance_policies SET pdf_url=%s, pdf_filename=%s, pdf_size=%s WHERE id=%s",
                            (pdf_url, final_fname, len(data), d['id']))
                n_done += 1
            except Exception as e:
                print(f"    error {fname}: {e}")
        conn.commit()
        print(f"  uploaded {n_done}")
    conn.close()


if __name__ == "__main__":
    main()
