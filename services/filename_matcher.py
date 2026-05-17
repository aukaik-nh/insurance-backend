"""
filename_matcher.py
─────────────────────────────────────────────────────────────────────
Logic แยกชื่อไฟล์ + match กับ insurance_policies
- ไม่พึ่ง Google Drive — ใช้ได้ทั้ง Drive migration และ Storage migration
"""
import os, re
from pathlib import Path

THAI_CONSONANTS = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"

# ทะเบียนรถ format:
#   - 2-3 ตัวอักษรไทย + 1-4 ตัวเลข เช่น ฆก6755, กต8256
#   - หรือ 1 ตัวเลข + 1-2 ตัวอักษรไทย + 1-4 ตัวเลข เช่น 1ฒน1479, 1ค5163
#   - หรือ 6-7 ตัวเลขล้วน (ทะเบียนเก่า) เช่น 816814
# จับเฉพาะกรณีที่ตามด้วย space / doc-type / .pdf / end เพื่อไม่จับชื่อ
PLATE_PATTERNS = [
    rf"^(\d?[{THAI_CONSONANTS}]{{1,2}}\s?\d{{1,4}})(?=\s|พรบ|กธ|PA|สลักหลัง|ยกเลิก|\.pdf)",
    rf"^(\d{{6,7}})(?=\s|พรบ|กธ|\.pdf)",
]

# ปี: หลัง "กธ." หรือ "พรบ." หรือ "PA." หรือลำพัง 2 digits ก่อน .pdf
YEAR_PATTERN = r"(?:กธ|พรบ|PA|สลักหลัง|ยกเลิก)\s*\.?\s*(\d{2})|(?<!\d)(\d{2})\s*\.\s*pdf"


def parse_filename(filename: str) -> dict:
    """แยก filename → {kind: 'plate'|'address'|'name', key: str, year_be: int|None, raw: str}"""
    stem = Path(filename).stem
    cleaned = re.sub(r"_\d{4}$", "", stem)
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", cleaned).strip()

    # year
    year_be = None
    m = re.search(YEAR_PATTERN, filename)
    if m:
        y = m.group(1) or m.group(2)
        if y:
            year_be = 2500 + int(y)

    # ทะเบียนรถ
    for pat in PLATE_PATTERNS:
        m = re.match(pat, cleaned)
        if m:
            return {"kind": "plate", "key": m.group(1).replace(" ", ""),
                    "year_be": year_be, "raw": filename}

    # ที่อยู่ — ขึ้นต้นด้วยเลข + ตัวอักษรไทย
    m = re.match(rf"^(\d+(?:[-/]\d+)?(?:[-/]\d+)?)\s*([{THAI_CONSONANTS}][^\s]*?)(?:\s|$)", cleaned)
    if m:
        number_part = m.group(1)
        suffix_part = m.group(2)[:20]
        full_key = f"{number_part} {suffix_part}".strip()
        return {"kind": "address", "key": full_key, "number": number_part,
                "suffix": suffix_part, "year_be": year_be, "raw": filename}

    # ชื่อ
    m = re.match(rf"^([{THAI_CONSONANTS}].+?)\s*(?:กธ|พรบ|PA|\d{{2}}\.|\.pdf|$)", cleaned)
    if m:
        name = m.group(1).strip()
        if len(name) >= 3:
            return {"kind": "name", "key": name, "year_be": year_be, "raw": filename}

    return {"kind": "unknown", "key": cleaned, "year_be": year_be, "raw": filename}


def expand_thai_abbrev(s: str) -> str:
    """แปลง ซ./ถ./ม./ต./อ./จ. → คำเต็ม (กัน DB กับ filename เขียนต่างกัน)
    เช่น 'ซ.จรัญ' → 'ซอยจรัญ', 'ม.1' → 'หมู่1'"""
    if not s:
        return s
    s = re.sub(r'ซ\.\s*', 'ซอย', s)
    s = re.sub(r'ถ\.\s*', 'ถนน', s)
    s = re.sub(r'ม\.\s*(?=\d)', 'หมู่', s)  # ม.1, ม.2 → หมู่1, หมู่2
    s = re.sub(r'ต\.\s*', 'ตำบล', s)
    s = re.sub(r'อ\.\s*', 'อำเภอ', s)
    s = re.sub(r'จ\.\s*', 'จังหวัด', s)
    return s


def normalize(s: str) -> str:
    """expand ตัวย่อ + ลบ space + lowercase"""
    if not s:
        return ""
    return re.sub(r"\s+", "", expand_thai_abbrev(s)).lower()


def number_token_in(number_norm: str, addr_norm: str) -> bool:
    """เช็คว่า number_norm เป็น token เต็มใน addr_norm
    (ไม่ใช่ส่วนหนึ่งของเลขใหญ่กว่า เช่น '12' ไม่ match '120')"""
    if not number_norm or not addr_norm:
        return False
    idx = addr_norm.find(number_norm)
    while idx >= 0:
        before_ok = (idx == 0) or not addr_norm[idx-1].isdigit()
        end_idx = idx + len(number_norm)
        after_ok = (end_idx == len(addr_norm)) or not addr_norm[end_idx].isdigit()
        if before_ok and after_ok:
            return True
        idx = addr_norm.find(number_norm, idx + 1)
    return False


def year_matches(coverage_start: str, year_be: int) -> bool:
    """coverage_start (YYYY-MM-DD) ตรงปี BE ±1 ปีไหม"""
    if not coverage_start or not year_be:
        return True
    try:
        cov_year = int(coverage_start[:4])
        target_ad = year_be - 543
        return target_ad - 1 <= cov_year <= target_ad + 1
    except (ValueError, TypeError):
        return False


def _check_match(p: dict, kind: str, key_norm: str, parsed: dict) -> bool:
    """เช็คว่า record p ตรงกับ key หรือไม่ — ไม่เช็คปี"""
    if kind == "plate":
        plate = normalize(p.get("license_plate") or "")
        if plate in ("other", "null", ""):
            return False
        return key_norm in plate or plate in key_norm

    if kind == "address":
        addr = normalize(p.get("insured_address") or "")
        number = parsed.get("number", "")
        suffix = parsed.get("suffix", "")
        num_variants = {
            normalize(number),
            normalize(number).replace("-", "/"),
            normalize(number).replace("/", "-"),
        }
        suf_norm = normalize(suffix)
        # ใช้ token match กัน "12" ไป match "120"
        num_in = any(number_token_in(v, addr) for v in num_variants if v)
        suf_in = (suf_norm[:5] in addr) if len(suf_norm) >= 3 else True
        return num_in and suf_in

    if kind == "name":
        name = normalize(p.get("insured_name") or "")
        return key_norm in name

    return False


def find_matches(parsed: dict, policies: list, *,
                 best_only: bool = True,
                 strict_year: bool = False) -> list:
    """หา records ที่ match — คืน list ของ ids

    Logic:
    1. หา records ที่ match customer key (plate/address/name)
    2. ถ้ามีปีใน filename → เลือก record ที่ปีใกล้สุด (เสมอ — ไม่จำกัดระยะ)
       UI แสดง related PDFs ของลูกค้าคนเดียวกันอยู่แล้ว
    3. ถ้าไม่มีปี → คืนตัวแรก (หรือทั้งหมดถ้า best_only=False)

    Args:
        best_only: True = คืนแค่ records ที่ปีใกล้สุด
        strict_year: True = ต้องเจอ year exactly — ไม่เจอคืน []
    """
    kind = parsed["kind"]
    key  = parsed["key"]
    year = parsed["year_be"]
    if not key or kind == "unknown":
        return []

    key_norm = normalize(key)
    candidates = [p for p in policies if _check_match(p, kind, key_norm, parsed)]
    if not candidates:
        return []

    # ไม่มีปีใน filename — คืน candidate แรก
    if not year:
        return [candidates[0]["id"]] if best_only else [p["id"] for p in candidates]

    target_ad = year - 543

    def year_distance(p):
        cov = p.get("coverage_start")
        if not cov:
            return 999
        try:
            cov_year = int(cov[:4])
            if cov_year >= 2500:
                cov_year -= 543
            return abs(cov_year - target_ad)
        except (ValueError, TypeError):
            return 999

    if strict_year:
        return [p["id"] for p in candidates if year_distance(p) == 0]

    # default: เลือกตัวที่ปีใกล้สุด (ไม่จำกัดระยะ — UI ดู related ของลูกค้าได้)
    scored = sorted(candidates, key=year_distance)
    min_dist = year_distance(scored[0])
    if best_only:
        return [p["id"] for p in scored if year_distance(p) == min_dist]
    return [p["id"] for p in scored]


def load_all_policies(sb):
    """โหลด policies ทั้งหมดเข้า memory ครั้งเดียว"""
    rows = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, license_plate, insured_address, insured_name, coverage_start, pdf_url"
        ).range(page * 1000, page * 1000 + 999).execute()
        if not r.data:
            break
        rows.extend(r.data)
        if len(r.data) < 1000:
            break
        page += 1
    return rows
