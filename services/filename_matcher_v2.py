"""
filename_matcher_v2.py — type-aware matcher (ตาม Baby78 convention)

Baby78 filename convention (verified จาก MDB + BabyScan samples):
  M, P                      → plate-based         "{plate-nospace} {กธ|พรบ}.{YY}.pdf"
  FIRE/ASSET/IAR/BURGLAR/   → address-based       "{addr-prefix} {กธ}.{YY}.pdf"
  MISC/3RD/PUBLIC                                  (address number: / → -)
  PA, TA, MARINE, GOLF      → name-based          "{name-nospace} {กธ}.{YY}.pdf"

Key implementation details:
  - address normalize: '/' และ '-' ถือว่าตัวคั่นตัวเดียวกัน → ลบทิ้ง
  - plate normalize: ลบ space ทั้งหมด
  - name normalize: ลบ space ทั้งหมด
  - year match: filename YY (พ.ศ.) ตรงกับ coverage_start year (ค.ศ. + 543)
"""
import os, re
from pathlib import Path

THAI_CONSONANTS = "กขฃคฅฆงจฉชซฌญฎฏฐฑฒณดตถทธนบปผฝพฟภมยรลวศษสหฬอฮ"

# Mapping policy_type → match strategy (plate / address / name)
TYPE_STRATEGY = {
    "M":       "plate",
    "P":       "plate",
    "FIRE":    "address",
    "ASSET":   "address",
    "IAR":     "address",
    "BURGLAR": "address",
    "MISC":    "address",
    "3RD":     "address",
    "PUBLIC":  "address",
    "PA":      "name",
    "TA":      "name",
    "MARINE":  "name",
    "GOLF":    "name",
}

# Reverse: filename kind → allowed policy_types
KIND_TO_TYPES = {
    "plate":   ["M", "P"],
    "address": ["FIRE", "ASSET", "IAR", "BURGLAR", "MISC", "3RD", "PUBLIC"],
    "name":    ["PA", "TA", "MARINE", "GOLF"],
}


# ── Filename parsing ────────────────────────────────────────────────
PLATE_PATTERNS = [
    rf"^(\d?[{THAI_CONSONANTS}]{{1,2}}\s?\d{{1,4}})(?=\s|พรบ|กธ|PA|สลักหลัง|ยกเลิก|\.pdf)",
    rf"^(\d{{6,7}})(?=\s|พรบ|กธ|\.pdf)",
]
YEAR_PATTERN = r"(?:กธ|พรบ|PA|สลักหลัง|ยกเลิก)\s*\.?\s*(\d{2})|(?<!\d)(\d{2})\s*\.\s*pdf"


def parse_filename(filename: str) -> dict:
    """แยก filename → {kind, key, year_be, raw}"""
    stem = Path(filename).stem
    cleaned = re.sub(r"_\d{4}$", "", stem)
    cleaned = re.sub(r"\s*\(.*?\)\s*", " ", cleaned).strip()

    year_be = None
    m = re.search(YEAR_PATTERN, filename)
    if m:
        y = m.group(1) or m.group(2)
        if y:
            year_be = 2500 + int(y)

    # plate
    for pat in PLATE_PATTERNS:
        m = re.match(pat, cleaned)
        if m:
            return {"kind": "plate", "key": m.group(1).replace(" ", ""),
                    "year_be": year_be, "raw": filename}

    # address — เริ่มด้วยเลข (อาจมี - หรือ /) + ตัวอักษรไทย
    m = re.match(rf"^(\d+(?:[-/]\d+)?(?:[-/]\d+)?)\s*([{THAI_CONSONANTS}][^\s]*?)(?:\s|$)", cleaned)
    if m:
        number_part = m.group(1)
        suffix_part = m.group(2)[:25]
        return {"kind": "address", "key": f"{number_part} {suffix_part}".strip(),
                "number": number_part, "suffix": suffix_part,
                "year_be": year_be, "raw": filename}

    # name — เริ่มด้วยอักษรไทย (ไม่ใช่เลข)
    m = re.match(rf"^([{THAI_CONSONANTS}].+?)\s*(?:กธ|พรบ|PA|\d{{2}}\.|\.pdf|$)", cleaned)
    if m:
        name = m.group(1).strip()
        if len(name) >= 3:
            return {"kind": "name", "key": name,
                    "year_be": year_be, "raw": filename}

    return {"kind": "unknown", "key": cleaned, "year_be": year_be, "raw": filename}


# ── Normalization ──────────────────────────────────────────────────
def expand_thai_abbrev(s: str) -> str:
    if not s: return s
    s = re.sub(r'ซ\.\s*', 'ซอย', s)
    s = re.sub(r'ถ\.\s*', 'ถนน', s)
    s = re.sub(r'ม\.\s*(?=\d)', 'หมู่', s)
    s = re.sub(r'ต\.\s*', 'ตำบล', s)
    s = re.sub(r'อ\.\s*', 'อำเภอ', s)
    s = re.sub(r'จ\.\s*', 'จังหวัด', s)
    return s


def norm_plate(s: str) -> str:
    """plate: strip whitespace"""
    return re.sub(r"\s+", "", s or "").lower()


def norm_name(s: str) -> str:
    """name: strip whitespace + lowercase"""
    return re.sub(r"\s+", "", s or "").lower()


def norm_address(s: str) -> str:
    """address: expand abbrev + normalize separators (/-) + strip whitespace
    Baby78 filename uses '-' for address number while DB stores '/' (or vice versa)
    → ลบ '/' และ '-' ทิ้งทั้งหมด ทำให้ '184/81' กับ '184-81' กับ '18481' match กัน"""
    s = expand_thai_abbrev(s or "")
    s = re.sub(r"[/\-]", "", s)
    return re.sub(r"\s+", "", s).lower()


def norm_address_text_only(s: str) -> str:
    """address with all standalone numbers stripped — for fuzzy place-name match
    'หมู่ที่1หมู่บ้านสหกรณ์' → 'หมู่ที่หมู่บ้านสหกรณ์'
    'หมู่ 4 หมู่บ้านสหกรณ์'  → 'หมู่หมู่บ้านสหกรณ์'
    Both contain 'หมู่บ้านสหกรณ์' ≥ 5-char overlap → match
    หมายเหตุ: ใช้คู่กับ house_number match แยกต่างหาก"""
    s = norm_address(s)
    s = re.sub(r"\d+", "", s)   # strip ALL digits
    return s


def longest_common_substring(a: str, b: str) -> int:
    """หาความยาว LCS — quick O(n*m) impl"""
    if not a or not b: return 0
    m, n = len(a), len(b)
    if m * n > 200_000: m, n = min(m, 400), min(n, 400); a, b = a[:m], b[:n]
    dp = [0] * (n + 1)
    best = 0
    for i in range(1, m + 1):
        prev = 0
        for j in range(1, n + 1):
            tmp = dp[j]
            if a[i-1] == b[j-1]:
                dp[j] = prev + 1
                if dp[j] > best: best = dp[j]
            else:
                dp[j] = 0
            prev = tmp
    return best


# ── Year matching ──────────────────────────────────────────────────
def coverage_year_ad(p: dict) -> int | None:
    cov = p.get("coverage_start")
    if not cov: return None
    try:
        y = int(str(cov)[:4])
        if y >= 2500: y -= 543
        return y
    except (ValueError, TypeError):
        return None


def year_distance(p: dict, year_be: int | None) -> int:
    """ระยะปี filename กับ coverage_start (ปี ค.ศ.)"""
    if not year_be: return 0
    target_ad = year_be - 543
    cy = coverage_year_ad(p)
    if cy is None: return 999
    return abs(cy - target_ad)


# ── Matching ───────────────────────────────────────────────────────
def _key_matches(parsed: dict, p: dict) -> bool:
    """ตรวจว่า key จาก filename match policy record หรือไม่
    Match strategy ขึ้นกับ kind, ไม่สนใจ policy_type ในขั้นนี้"""
    kind = parsed["kind"]
    key  = parsed["key"]

    if kind == "plate":
        plate_norm = norm_plate(p.get("license_plate"))
        # ละ placeholder plate
        if plate_norm in ("other", "null", "fire", "pa", "ta", "asset", "misc"):
            return False
        key_norm = norm_plate(key)
        return key_norm == plate_norm or key_norm in plate_norm or plate_norm in key_norm

    if kind == "address":
        addr_full = norm_address(p.get("insured_address"))
        if not addr_full: return False
        number = parsed.get("number", "")
        suffix = parsed.get("suffix", "")
        num_norm = re.sub(r"[/\-]", "", number)  # 184-81 → 18481, 184/81 → 18481
        # house number must appear in DB addr (token-style — not part of larger number)
        if not num_norm or num_norm not in addr_full:
            return False
        # ตรวจ token boundary: เลขห้ามต่อกับเลขอื่น (กัน "138" ไป match "1380")
        idx = addr_full.find(num_norm)
        end = idx + len(num_norm)
        if end < len(addr_full) and addr_full[end].isdigit():
            return False  # "138" followed by more digits in addr → not token match
        if idx > 0 and addr_full[idx-1].isdigit():
            return False
        # ผ่าน house# → ตรวจ place name (LCS ≥ 3 ของ text-only normalized)
        # ลด threshold เพราะ Baby78 user มักเขียน suffix สั้น (เช่น "หมู่ที่3")
        addr_text = norm_address_text_only(p.get("insured_address"))
        suf_text  = norm_address_text_only(suffix)
        if not suf_text or len(suf_text) < 2:
            return True   # filename has no place name → match by house# alone
        lcs = longest_common_substring(suf_text, addr_text)
        return lcs >= 3

    if kind == "name":
        name = norm_name(p.get("insured_name"))
        if not name: return False
        key_norm = norm_name(key)
        return key_norm in name or name in key_norm

    return False


def find_matches_v2(parsed: dict, policies: list, *,
                    strict_type: bool = True,
                    strict_year: bool = False,
                    best_only: bool = True) -> dict:
    """type-aware matching

    Args:
        strict_type: True → จำกัด candidate ต้องเป็น policy_type ที่ allowed สำหรับ kind นั้น
                      False → ลอง type-aware ก่อน ถ้าไม่เจอ fallback ทุก type
        strict_year: True → ต้อง match year_be == coverage_start year (พ.ศ.) เป๊ะ
                      False → เลือกตัวที่ปีใกล้สุด
        best_only: True → คืน id ของ candidate ที่ดีที่สุด (ปีใกล้สุด)

    Returns: {
        "matched_ids": [id, ...],
        "strategy":    "type-match" | "fallback" | "no-match",
        "candidates_total": N,
        "candidates_type_matched": N,
    }
    """
    kind = parsed["kind"]
    if not parsed.get("key") or kind == "unknown":
        return {"matched_ids": [], "strategy": "no-match",
                "candidates_total": 0, "candidates_type_matched": 0}

    allowed_types = set(KIND_TO_TYPES.get(kind, []))

    # หา candidates ที่ key match
    all_cands = [p for p in policies if _key_matches(parsed, p)]
    typed_cands = [p for p in all_cands
                   if (p.get("policy_type") or "").upper() in allowed_types]

    use_cands  = typed_cands
    strategy   = "type-match"
    if not use_cands:
        if strict_type:
            return {"matched_ids": [], "strategy": "no-match",
                    "candidates_total": len(all_cands),
                    "candidates_type_matched": 0}
        # fallback ไม่บังคับ type
        use_cands = all_cands
        strategy  = "fallback"

    if not use_cands:
        return {"matched_ids": [], "strategy": "no-match",
                "candidates_total": 0, "candidates_type_matched": 0}

    year = parsed.get("year_be")
    if strict_year and year:
        use_cands = [p for p in use_cands if year_distance(p, year) == 0]
        if not use_cands:
            return {"matched_ids": [], "strategy": "no-match-year",
                    "candidates_total": len(all_cands),
                    "candidates_type_matched": len(typed_cands)}

    if year:
        use_cands.sort(key=lambda p: year_distance(p, year))
        if best_only:
            min_dist = year_distance(use_cands[0], year)
            use_cands = [p for p in use_cands if year_distance(p, year) == min_dist]

    matched_ids = [p["id"] for p in use_cands] if not best_only else [use_cands[0]["id"]]
    return {
        "matched_ids": matched_ids,
        "strategy":    strategy,
        "candidates_total": len(all_cands),
        "candidates_type_matched": len(typed_cands),
    }


def load_policies_with_type(sb):
    """load policies + policy_type สำหรับ type-aware matcher"""
    rows = []
    page = 0
    while True:
        r = sb.table("insurance_policies").select(
            "id, license_plate, insured_address, insured_name, "
            "coverage_start, coverage_end, policy_type, pdf_url"
        ).range(page*1000, page*1000+999).execute()
        if not r.data: break
        rows.extend(r.data)
        if len(r.data) < 1000: break
        page += 1
    return rows
