"""
claude_parser.py — parse ข้อมูลกรมธรรม์จาก OCR text (Tesseract Thai+Eng)
รองรับทั้ง PDF ที่มี text layer ปกติ และ PDF เก่า (Safety/MSIG font encoding)
"""

import re


# ════════════════════════════════════════════════════════════
# THAI DATE PARSER
# ════════════════════════════════════════════════════════════
THAI_MONTHS = {
    'มกราคม': 1,    'ม.ค.': 1,
    'กุมภาพันธ์': 2, 'ก.พ.': 2, 'กุมภาพันธ': 2,
    'มีนาคม': 3,     'มี.ค.': 3,
    'เมษายน': 4,     'เม.ย.': 4, 'เมษา': 4,
    'พฤษภาคม': 5,   'พ.ค.': 5,
    'มิถุนายน': 6,   'มิ.ย.': 6,
    'กรกฎาคม': 7,   'ก.ค.': 7,  'กรกฏาคม': 7,
    'สิงหาคม': 8,    'ส.ค.': 8,
    'กันยายน': 9,    'ก.ย.': 9,
    'ตุลาคม': 10,    'ต.ค.': 10,
    'พฤศจิกายน': 11, 'พ.ย.': 11,
    'ธันวาคม': 12,   'ธ.ค.': 12,
}

# เดือนภาษาอังกฤษ (สำหรับ PDF ฝรั่ง)
ENG_MONTHS = {
    'jan':1,'feb':2,'mar':3,'apr':4,'may':5,'jun':6,
    'jul':7,'aug':8,'sep':9,'oct':10,'nov':11,'dec':12,
    'january':1,'february':2,'march':3,'april':4,'june':6,
    'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,
}


def _parse_thai_dates(text):
    out = []
    for month_word in sorted(THAI_MONTHS, key=len, reverse=True):
        pattern = rf'(\d{{1,2}})\s*{re.escape(month_word)}\s*(\d{{2,4}})'
        for m in re.finditer(pattern, text):
            d, y = int(m.group(1)), int(m.group(2))
            if y < 100: y += 2500
            if y >= 2500: y -= 543
            mo = THAI_MONTHS[month_word]
            if 1990 <= y <= 2035 and 1 <= d <= 31:
                out.append((m.start(), f"{y:04d}-{mo:02d}-{d:02d}"))

    # รูปแบบ DD/MM/YYYY
    for m in re.finditer(r'(\d{1,2})/(\d{1,2})/(\d{2,4})', text):
        try:
            d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100: y += 2000
            if y >= 2500: y -= 543
            if 1990 <= y <= 2035 and 1 <= d <= 31 and 1 <= mo <= 12:
                out.append((m.start(), f"{y:04d}-{mo:02d}-{d:02d}"))
        except ValueError:
            pass

    # รูปแบบ DD Month YYYY (English)
    for mw, mn in ENG_MONTHS.items():
        pat = rf'(\d{{1,2}})\s+{mw}\s+(\d{{2,4}})'
        for m in re.finditer(pat, text, re.IGNORECASE):
            d, y = int(m.group(1)), int(m.group(2))
            if y < 100: y += 2000
            if y >= 2500: y -= 543
            if 1990 <= y <= 2035 and 1 <= d <= 31:
                out.append((m.start(), f"{y:04d}-{mn:02d}-{d:02d}"))

    out.sort(key=lambda x: x[0])
    seen, result = set(), []
    for _, d in out:
        if d not in seen:
            seen.add(d)
            result.append(d)
    return result


# ════════════════════════════════════════════════════════════
# FILENAME PARSER
# พวก BabyPreechar มีข้อมูลในชื่อไฟล์ เช่น:
#   "1-8ถนนจรัญ กธ.56.pdf"    → address, year
#   "1กก5226 กธ.59.pdf"        → license, year
#   "CHADAKODWA 09.07.17.pdf"  → name, date
# ════════════════════════════════════════════════════════════

def _parse_filename(filename: str) -> dict:
    """ดึงข้อมูลจากชื่อไฟล์"""
    info = {}
    fn = re.sub(r'\.(pdf|PDF)$', '', filename).strip()

    # ── ทะเบียนรถ ──
    # รูปแบบ: "1กข1234", "กข 1234", "ฎน63" (2 หลัก), "กธ 5226"
    m = re.search(r'(?:^|\s)(\d?[ก-ฮ]{1,2})\s*(\d{2,4})(?:\s|$)', fn)
    if m:
        info['license_plate'] = f"{m.group(1)} {m.group(2)}"
    else:
        # fallback สำหรับ pattern ไม่มีเลขนำหน้า
        m = re.search(r'\b([ก-ฮ]{1,2})\s*(\d{2,4})\b', fn)
        if m:
            # อย่า match ปีกรมธรรม์ (กธ.60 หรือ พรบ.60)
            before = fn[:m.start()]
            if not re.search(r'(?:กธ|พรบ|ก\.ธ|พ\.ร\.บ)\s*$', before):
                info['license_plate'] = f"{m.group(1)} {m.group(2)}"

    # ── ปีกรมธรรม์ "กธ.59" หรือ "พรบ.60" หรือ "กธ 59" ──
    m = re.search(r'(?:กธ|พรบ|ก\.ธ|พ\.ร\.บ)[\s\.]*(\d{2})\b', fn)
    if m:
        yr2 = int(m.group(1))
        yr = yr2 + 2500  # พ.ศ. สองหลัก → สี่หลัก
        yr_ad = yr - 543
        if 2000 <= yr_ad <= 2030:
            # กำหนด coverage_end ตามปี (สิ้นปีนั้น)
            info['_year_be'] = yr
            info['coverage_end'] = f"{yr_ad:04d}-12-31"

    # ── วันที่รูปแบบ DD.MM.YY (เช่น 09.07.17) ──
    m = re.search(r'(\d{1,2})\.(\d{1,2})\.(\d{2,4})(?:\s|$|\.pdf)', fn, re.IGNORECASE)
    if m:
        d, mo, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if yr < 100: yr += 2000
        if yr >= 2500: yr -= 543
        if 1990 <= yr <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
            info['coverage_end'] = f"{yr:04d}-{mo:02d}-{d:02d}"

    # ── ชื่อ/ที่อยู่จากชื่อไฟล์ ──
    # ลบส่วนที่เป็น metadata ออก (ปี, ทะเบียน, หมายเหตุ)
    clean = fn
    clean = re.sub(r'(?:กธ|พรบ|ก\.ธ|พ\.ร\.บ)[\s\.]*\d{2}\b.*', '', clean)
    clean = re.sub(r'\d{1,2}\.\d{1,2}\.\d{2,4}.*', '', clean)
    clean = re.sub(r'\(.*?\)', '', clean)               # วงเล็บ
    clean = re.sub(r'_\d{4}$', '', clean)               # suffix _0001
    clean = re.sub(r'[\s_]+', ' ', clean).strip()
    # ลบทะเบียนรถออก (ทั้งแบบ "1กข1234" และ "กข 1234")
    clean = re.sub(r'\d*[ก-ฮ]{1,2}\s*\d{2,4}', '', clean).strip()
    # ลบตัวเลขนำหน้า
    clean = re.sub(r'^[\d\-/\.]+\s*', '', clean).strip()

    # ถ้ามีภาษาไทย และมีข้อความมากกว่าแค่ทะเบียน → ที่อยู่
    thai_text = ''.join(c for c in clean if 'ก' <= c <= '๿')
    if re.search(r'[ก-๛]', clean) and len(thai_text) >= 4 and len(clean) >= 6:
        # แปลงตัวคั่น - เป็น / สำหรับบ้านเลขที่
        addr = re.sub(r'^(\d+)-(\d+)', r'\1/\2', fn)
        addr = re.sub(r'(?:กธ|พรบ)[\s\.]*\d{2}.*', '', addr)
        addr = re.sub(r'\(.*?\)', '', addr)
        addr = re.sub(r'_\d{4}', '', addr)
        addr = re.sub(r'[\s_]+', ' ', addr).strip()
        # อย่าเก็บถ้าเหลือแค่ทะเบียน
        addr_no_plate = re.sub(r'\d*[ก-ฮ]{1,2}\s*\d{2,4}', '', addr).strip()
        if len(addr_no_plate) >= 5:
            info['insured_address'] = addr[:200]

    # ถ้าเป็นอักษรอังกฤษล้วน (ชื่อคน) → insured_name
    elif re.match(r'^[A-Z][A-Z\s]+$', clean) and len(clean) >= 4:
        info['insured_name'] = clean.title()

    return info


# ════════════════════════════════════════════════════════════
# FIX GARBLED NUMBER (font encoding เก่า: o→0, '→,)
# ════════════════════════════════════════════════════════════
def _fix_garbled_number(s: str) -> str:
    """แก้ตัวเลขที่ถูก encode ผิด: 1'5oo.oo → 1500.00"""
    s = s.replace("'", "")          # ลบ apostrophe (thousands sep แบบเก่า)
    s = re.sub(r'(?<=\d)o(?=\d|$)', '0', s)  # o ระหว่างตัวเลข → 0
    s = re.sub(r'(?<=\d)O(?=\d|$)', '0', s)
    # ลบ space ระหว่างตัวเลข "1 500" → "1500" ถ้าเป็น 3 หลักแน่
    s = re.sub(r'(\d)\s(\d{2})\b', r'\1\2', s)
    return s.strip()


def _parse_money(text: str) -> float | None:
    """แปลง string → float รองรับ format เก่า"""
    if not text:
        return None
    text = _fix_garbled_number(text)
    text = text.replace(',', '').strip()
    try:
        return float(text) if text else None
    except ValueError:
        return None


# ════════════════════════════════════════════════════════════
# MAIN PARSER
# ════════════════════════════════════════════════════════════
def parse_insurance_data(raw_text: str, filename: str = "") -> dict:
    t = raw_text
    tu = t.upper()

    # ── ดึงจากชื่อไฟล์ก่อน (ข้อมูลที่เชื่อถือได้กว่า) ──────
    fn_info = _parse_filename(filename) if filename else {}

    # ── 1. COMPANY CODE ──────────────────────────────────────
    company_code = ""
    m = re.search(r'(?:รหัสบริษัท|company\s*code)\s*[:\s]*([A-Z]{3,6})', t, re.IGNORECASE)
    if m:
        company_code = m.group(1).upper()
    else:
        for pat, code in [
            # บริษัทที่พบบ่อยใน BabyPreechar (Safety, MSIG/HsIG)
            (r'safety\s+insurance',          'STI'),
            (r'\bmsig\b|HsIG|MsIG',          'MSIG'),
            (r'tmsth|tokio.*mar',             'TKM'),
            (r'viriyah|วิริยะ',               'VRI'),
            (r'\baxa\b',                      'AXA'),
            (r'allianz',                      'ALZ'),
            (r'dhipaya|ทิพย',                'DHP'),
            (r'muang.*thai|เมืองไทย',         'MTI'),
            (r'navakij',                      'NVK'),
            (r'ergo',                         'ERGO'),
            (r'krungthai.*panich|ktp',        'KTP'),
            (r'indara|อินทร',                 'IND'),
            (r'falcon',                       'FAL'),
            (r'syn\s*mun\s*kong|สินมั่นคง',   'SMK'),
            (r'asia\s+insurance|เอเชียประ',   'AIS'),
            (r'bkk.*insurance|กรุงเทพประ',    'BKI'),
            (r'lmg',                          'LMG'),
        ]:
            if re.search(pat, t, re.IGNORECASE):
                company_code = code
                break

    # ── 2. POLICY NUMBER ─────────────────────────────────────
    policy_number = ""

    for pat in [
        r'(?:policy\s*num(?:ber|hcr|bcr)|กรมธรรม(?:ประกันภัย)?เลขที่)\s*[:\s]*([A-Z0-9][A-Z0-9\/\-]{5,25})',
        # รูปแบบ D0-72-69/006797 หรือ Do-21-s6/000032 (พร้อม char subst)
        r'\b(D[O0o]-?\d{2}-[0-9sc]{2}[\/r][0-9co]{4,8})\b',
        r'\b([A-Z]{0,2}\d{1,2}-\d{2}-\d{2,4}[\/r]\d{4,8})\b',
        # รูปแบบ Policy No ตาม label
        r'Policy\s*(?:No|Number)[.\s:]*([A-Z0-9][A-Z0-9\/\-\.]{5,25})',
        # รูปแบบ lf-' Do-21 (มี prefix garbage)
        r'\bD[O0o]-\d{2}-\d{2}\/\d{4,8}\b',
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            raw_pol = m.group(0) if m.lastindex == 0 else m.group(1)
            # clean char substitutions
            raw_pol = re.sub(r'\s+', '', raw_pol).upper()
            raw_pol = raw_pol.replace('O', '0').replace('R', '/')
            # ต้องเป็น format ที่สมเหตุสมผล
            if re.search(r'[A-Z0-9]-\d{2}-\d{2}\/\d{4,}', raw_pol):
                policy_number = raw_pol
                break
            elif len(raw_pol) >= 8:
                policy_number = raw_pol
                break

    # ── 3. INSURED NAME ──────────────────────────────────────
    insured_name = fn_info.get('insured_name', '')
    if not insured_name:
        for pat in [
            r'([ก-๙][ก-๙\s\.]{3,50}?)\s{2,}insured\s+name',
            r'(?:ผู้[ก-๙]{1,4}ประกัน[ก-๙]{0,6}(?:ชื่อ)?|insured\s*name)\s*[:\s]*([ก-๙][ก-๙\s\.]{3,60})',
            r'((?:คุณ|นาย|นาง(?:สาว)?|น\.ส\.|บริษัท)\s*[ก-๙][ก-๙\s\.\/]{2,60}(?:จำกัด|จํากัด)?)',
        ]:
            for m in re.finditer(pat, t, re.IGNORECASE):
                s = m.group(1).strip()
                s = re.split(r'\s{2,}|\n|(?:insured|address|ที่อยู|อาชีพ|occupation|driver)', s, flags=re.IGNORECASE)[0]
                s = re.sub(r'\s+', ' ', s).strip()
                thai_chars = [c for c in s if 'ก' <= c <= '๿']
                if len(thai_chars) >= 2 and 4 <= len(s) <= 80:
                    insured_name = s
                    break
            if insured_name:
                break

    # ── 4. ADDRESS ───────────────────────────────────────────
    insured_address = fn_info.get('insured_address', '')
    if not insured_address:
        for pat in [
            r'(?:ที่(?:กยู|อยู่?|อยู|กยู่))\s*(\d+[^\n\r]{10,200}?\d{5})',
            r'address\s+([ก-๙\d][^\n\r]{10,200}?\d{5})',
            r'(\d+[^\n\r]*(?:ซอย|ถนน|แขวง|ซ\.|ถ\.)[^\n\r]*\d{5})',
        ]:
            m = re.search(pat, t, re.IGNORECASE)
            if m:
                addr = m.group(1).strip()
                addr = re.split(r'\s+(?:อาชีพ|occupation|driver|ผู้ขับ|lthe)', addr, flags=re.IGNORECASE)[0]
                addr = re.sub(r'\baddress\b', '', addr, flags=re.IGNORECASE)
                addr = re.sub(r'\s+', ' ', addr).strip()
                if len(addr) >= 10:
                    insured_address = addr[:250]
                    break

    # ── 5. LICENSE PLATE ─────────────────────────────────────
    license_plate = fn_info.get('license_plate', '')
    if not license_plate:
        for pat in [
            r'\b([ก-ฮ]{1,2})\s*(\d{3,4})\s*(กท|กร|กบ|กข|กค|กง|กจ|กช|นบ|นค|นฉ|นม|บจ|บร|บษ|ปข|ปน|ผข|พก|พน|พร|พล|มก|มด|มน|รก|รน|ลจ|วก|วน|สก|สข|สง|สน|สย|สร|สว|สส|อก|อด|อน)\b',
            r'\b([ก-ฮ]{1,2})\s*(\d{3,4})\b',
            r'\b(\d[ก-ฮ]{2})\s*(\d{3,4})\b',
        ]:
            m = re.search(pat, t)
            if m:
                if m.lastindex >= 3 and m.group(3):
                    license_plate = f"{m.group(1)} {m.group(2)} {m.group(3)}"
                else:
                    license_plate = f"{m.group(1)} {m.group(2)}"
                break

    # ── 6. CHASSIS NO ────────────────────────────────────────
    chassis_no = ""
    for pat in [
        r'(?:chassis\s*no|เลขตัวถัง)\s*[:\s]*([A-Za-z0-9\$\#\@]{10,20})',
        r'[ก-ฮ]{1,2}\s*\d{3,4}\s*(?:[ก-ฮ]{2,3})?\s+([A-Za-z0-9\$\#]{10,20})\s+\d{4}',
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            cleaned = re.sub(r'[^A-Za-z0-9]', '', m.group(1)).upper()
            if len(cleaned) >= 10:
                chassis_no = cleaned
                break

    # ── 7. CAR MAKE / MODEL ──────────────────────────────────
    MAKES = ['TOYOTA','HONDA','ISUZU','NISSAN','MITSUBISHI','SUZUKI','FORD',
             'CHEVROLET','BMW','MERCEDES','BENZ','MAZDA','HYUNDAI','KIA',
             'SUBARU','VOLVO','MG','LEXUS','MINI','PORSCHE','AUDI','TESLA',
             'HINO','FUSO','JAC','FOTON','BYD','HAVAL','GWM']
    KNOWN_MODELS = {
        'MITSUBISHI': ['LANCER','PAJERO SPORT','PAJERO','TRITON','OUTLANDER','ATTRAGE','MIRAGE','ECLIPSE CROSS'],
        'TOYOTA':     ['CAMRY','COROLLA CROSS','COROLLA','FORTUNER','HILUX REVO','HILUX','VIOS','YARIS ATIV','YARIS','CHR','RAV4','ALPHARD','VELLFIRE','INNOVA'],
        'HONDA':      ['CIVIC','ACCORD','CR-V','HR-V','CITY HATCHBACK','CITY','JAZZ','BR-V','WR-V'],
        'ISUZU':      ['MU-7','MU-X','D-MAX','DMAX','TROOPER'],
        'NISSAN':     ['NAVARA','ALMERA','TEANA','X-TRAIL','MARCH','NOTE','KICKS','TERRA'],
        'FORD':       ['RANGER RAPTOR','RANGER','EVEREST','TERRITORY','MUSTANG'],
        'MAZDA':      ['CX-30','CX-3','CX-5','CX-8','MAZDA2','MAZDA3'],
        'MG':         ['ZS EV','ZS','HS','EP','VS','MG3','MG5'],
        'SUZUKI':     ['SWIFT','CIAZ','VITARA','ERTIGA','JIMNY','CARRY'],
    }

    car_make = ""
    car_model = ""
    for mk in MAKES:
        if mk in tu:
            car_make = mk
            break

    if car_make:
        for mod in KNOWN_MODELS.get(car_make, []):
            if mod in tu:
                car_model = mod
                break
        if not car_model:
            m = re.search(rf'{car_make}\s+([A-Z][A-Z0-9\-]{{2,15}})', tu)
            if m:
                cand = m.group(1).strip()
                if not re.match(r'^[ก-ฮ]', cand) and len(cand) >= 2:
                    car_model = cand

    # ── 8. CAR YEAR ──────────────────────────────────────────
    car_year = None
    for pat in [
        r'[A-Z0-9]{10,17}\s+(\d{4})\s+(?:sedan|wagon|suv|pickup|hatchback|van|saloon|truck)',
        r'(?:model\s*yr|ปีที่ผลิต|ปีรถ|year\s*of)\s*[:\s]*(\d{4})',
        r'\b((?:19[9]\d|200\d|201\d|202\d))\b',
    ]:
        m = re.search(pat, t, re.IGNORECASE)
        if m:
            yr = int(m.group(1))
            if 2500 <= yr <= 2580: yr -= 543
            if 1990 <= yr <= 2030:
                car_year = yr
                break

    # ── 9. DATES ─────────────────────────────────────────────
    dates = _parse_thai_dates(t)
    coverage_start = dates[0] if len(dates) > 0 else ""
    coverage_end   = dates[1] if len(dates) > 1 else ""

    # ถ้าได้แค่วันเดียว ให้ตรวจสอบว่า start หรือ end
    if len(dates) == 1 and not coverage_start:
        coverage_start = dates[0]

    # ถ้าชื่อไฟล์บอกปี ใช้เป็น coverage_end fallback
    if not coverage_end and fn_info.get('coverage_end'):
        coverage_end = fn_info['coverage_end']

    # ── 10. PREMIUMS ─────────────────────────────────────────
    # รองรับทั้ง format ปกติ และ format เก่า (1'5oo oo = 1500.00)
    def _find_money(pats_and_labels) -> float | None:
        for p in pats_and_labels:
            m = re.search(p, t, re.IGNORECASE)
            if m:
                raw = m.group(1).strip()
                val = _parse_money(raw)
                if val is not None and val > 0:
                    return val
        return None

    net_premium = _find_money([
        r'(?:net\s*pre[mr][ei]ium|เบี้ยประกันภัยสุทธิ|เบี้ยสุทธิ)\s*[:\s(Baht)]*\s*([\d\',o O]+(?:\.\d{1,2})?)',
        r'Net\s+P[a-z]*\s*\(Baht\)\s*([\d\',o O]+)',
    ])
    stamp_duty = _find_money([
        r'(?:stamp\s*du(?:ty|e)|อากรแสตมป์)\s*[:\s(Baht)]*\s*([\d\',o O]+(?:\.\d{1,2})?)',
    ])
    vat = _find_money([
        r'(?:vat|ภาษีมูลค่าเพิ่ม|ภาษี\s*7%)\s*[:\s(Baht)]*\s*([\d\',o O]+(?:\.\d{1,2})?)',
    ])
    total_premium = _find_money([
        r'(?:total|รวม(?:เบี้ย)?(?:ประกันภัย)?(?:ทั้งสิ้น)?|grand\s*total|Total\s*\(Bah?[lt]\))\s*[:\s]*\s*([\d\',o O]+(?:\.\d{1,2})?)',
    ])

    # fallback: ค้นหาตัวเลขทศนิยม 2 ตำแหน่งทั้งหมด
    if not net_premium or not total_premium:
        # หา pattern แบบ "label Amount" ในบรรทัดเดียวกัน
        for line in t.split('\n'):
            lu = line.upper()
            nums = re.findall(r'([\d\',]+\.\d{2})', line)
            nums_clean = []
            for n in nums:
                v = _parse_money(n)
                if v and 100 < v < 500000:
                    nums_clean.append(v)
            if not nums_clean:
                continue
            if 'NET' in lu or 'NETT' in lu:
                if not net_premium: net_premium = nums_clean[0]
            if 'TOTAL' in lu or 'GRAND' in lu:
                if not total_premium: total_premium = nums_clean[-1]
            if 'STAMP' in lu:
                if not stamp_duty: stamp_duty = nums_clean[0]
            if 'VAT' in lu:
                if not vat: vat = nums_clean[0]

    # ── 11. BROKER ───────────────────────────────────────────
    broker_name = ""
    m = re.search(
        r'(?:นายหน้า|ตัวแทน|broker|agent)\s*(?:ชื่อ)?\s*[:\s]*([ก-๙A-Za-z][ก-๙A-Za-z\s\.]{3,50})',
        t, re.IGNORECASE
    )
    if m:
        name = re.split(r'\s{2,}|\n', m.group(1).strip())[0].strip()
        if not re.search(r'ประกันวินาศภัย|insurance|บริษัท|จำกัด|pcl', name, re.IGNORECASE):
            broker_name = name

    broker_license = ""
    m = re.search(r'(?:ใบอนุญาต|license\s*no)\s*[:\s]*([A-Z]?\d{7,12})', t, re.IGNORECASE)
    if m:
        broker_license = m.group(1).strip()

    # ── สรุปผล ───────────────────────────────────────────────
    result = {
        "policy_number":            policy_number,
        "company_code":             company_code,
        "insured_name":             insured_name,
        "insured_address":          insured_address,
        "license_plate":            license_plate,
        "chassis_no":               chassis_no,
        "car_make":                 car_make,
        "car_model":                car_model,
        "car_year":                 car_year,
        "coverage_start":           coverage_start,
        "coverage_end":             coverage_end,
        "net_premium":              net_premium,
        "stamp_duty":               stamp_duty,
        "vat":                      vat,
        "total_premium":            total_premium,
        "third_party_per_person":   None,
        "third_party_per_accident": None,
        "own_damage":               None,
        "broker_name":              broker_name,
        "broker_license":           broker_license,
    }

    print("[claude_parser] parsed:")
    for k, v in result.items():
        if v not in (None, "", 0):
            print(f"  {k}: {v}")

    return result
