import os, json, re, base64


_PROMPT = """คุณเป็นผู้เชี่ยวชาญด้านการอ่านกรมธรรม์ประกันภัยรถยนต์ภาษาไทย

อ่านเอกสารกรมธรรม์ประกันภัยในไฟล์ PDF นี้และดึงข้อมูลต่อไปนี้ออกมาเป็น JSON object เดียว
ถ้าหาข้อมูลใดไม่พบให้ใส่ null ห้ามเดาหรือสร้างข้อมูลขึ้นมาเอง

JSON keys ที่ต้องการ:
{
  "policy_number":             "เลขกรมธรรม์",
  "company_code":              "รหัสบริษัทประกัน เช่น TMSTH, SAFETY, MSIG, AXA, VIRIYAH",
  "insured_name":              "ชื่อ-นามสกุลผู้เอาประกันภัย",
  "insured_address":           "ที่อยู่ผู้เอาประกันภัย",
  "license_plate":             "ทะเบียนรถ เช่น ฒค 5219 กท",
  "chassis_no":                "เลขตัวถัง / Chassis No",
  "car_make":                  "ยี่ห้อรถ เช่น TOYOTA, ISUZU, MITSUBISHI",
  "car_model":                 "รุ่นรถ เช่น FORTUNER, MU-7, LANCER",
  "car_year":                  2008,
  "coverage_start":            "YYYY-MM-DD",
  "coverage_end":              "YYYY-MM-DD",
  "net_premium":               600.00,
  "stamp_duty":                3.00,
  "vat":                       42.21,
  "total_premium":             645.21,
  "third_party_per_person":    null,
  "third_party_per_accident":  null,
  "own_damage":                null,
  "broker_name":               "ชื่อตัวแทน/นายหน้า",
  "broker_license":            "เลขใบอนุญาตนายหน้า"
}

กฎสำคัญ:
1. วันที่: ตรวจสอบปีก่อนเสมอ
   - ถ้าปี >= 2500 = พ.ศ. → ลบ 543 เพื่อแปลงเป็น ค.ศ. เช่น 2569 → 2026, 2557 → 2014
   - ถ้าปี < 2500 = ค.ศ. อยู่แล้ว → ใช้เลยไม่ต้องแปลง เช่น 2014 → 2014
   - ผลลัพธ์ต้องอยู่ในรูป "YYYY-MM-DD" ค.ศ. เสมอ เช่น "2026-04-28", "2014-12-04"
2. car_year: ตัวเลขปี ค.ศ. เท่านั้น (ถ้าเป็น พ.ศ. ให้ลบ 543) เช่น 2008, 2012
3. ราคาเงิน: ตัวเลขเท่านั้น ไม่มี comma เช่น 645.21
4. ตอบเป็น JSON object เท่านั้น ไม่ต้องมีคำอธิบายหรือ markdown
"""


def _extract_json(text):
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text).strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        return json.loads(m.group())
    raise ValueError(f"ไม่พบ JSON:\n{text[:300]}")


def _yr(val):
    if val is None: return None
    try:
        y = int(str(val).strip().split('.')[0])
        if y >= 2500: y -= 543
        return y if 1980 <= y <= 2060 else None
    except: return None


def _dt(val):
    if not val: return None
    s = str(val).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        y = int(m.group(1))
        if y >= 2500: y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        if y >= 2500: y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _fl(val):
    if val is None or val == "": return None
    try: return float(str(val).replace(",", "").strip())
    except: return None


def is_available():
    return bool(os.getenv("GEMINI_API_KEY", ""))


def parse_with_gemini(file_bytes: bytes, filename: str = "") -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("ไม่พบ GEMINI_API_KEY")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    print(f"[gemini_parser] '{filename}' → Gemini 2.5 Flash (thinking)...")

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[
            types.Part.from_text(text=_PROMPT),
            types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"),
        ],
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_budget=2048),
        ),
    )

    raw = response.text
    print(f"[gemini_parser] {len(raw)} chars")

    data = _extract_json(raw)
    result = {
        "policy_number":            data.get("policy_number") or None,
        "company_code":             data.get("company_code") or None,
        "insured_name":             data.get("insured_name") or None,
        "insured_address":          data.get("insured_address") or None,
        "license_plate":            data.get("license_plate") or None,
        "chassis_no":               data.get("chassis_no") or None,
        "car_make":                 data.get("car_make") or None,
        "car_model":                data.get("car_model") or None,
        "car_year":                 _yr(data.get("car_year")),
        "coverage_start":           _dt(data.get("coverage_start")),
        "coverage_end":             _dt(data.get("coverage_end")),
        "net_premium":              _fl(data.get("net_premium")),
        "stamp_duty":               _fl(data.get("stamp_duty")),
        "vat":                      _fl(data.get("vat")),
        "total_premium":            _fl(data.get("total_premium")),
        "third_party_per_person":   _fl(data.get("third_party_per_person")),
        "third_party_per_accident": _fl(data.get("third_party_per_accident")),
        "own_damage":               _fl(data.get("own_damage")),
        "broker_name":              data.get("broker_name") or None,
        "broker_license":           data.get("broker_license") or None,
    }

    for k, v in result.items():
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a", "-"):
            result[k] = None

    print("[gemini_parser] parsed:")
    for k, v in result.items():
        if v not in (None, "", 0):
            print(f"  {k}: {v}")

    return result
