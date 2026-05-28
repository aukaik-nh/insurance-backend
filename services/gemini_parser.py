import os, json, re, base64


_PROMPT = """คุณคือ OCR ผู้เชี่ยวชาญกรมธรรม์ประกันรถยนต์ภาษาไทย หน้าที่: คัดลอกข้อความตามที่เห็นในเอกสาร 100% — ไม่ใช่นักเดา ไม่ใช่นักสรุป

═══════════════════════════════════════════════════════════════
กฎเหล็ก — ห้ามฝ่าฝืน
═══════════════════════════════════════════════════════════════

1. ถ้าอ่านไม่ออก / ไม่แน่ใจ / ภาพเบลอ → ใส่ null
   ห้าม "เดาที่ใกล้เคียง" เด็ดขาด ผิดดีกว่าเดามั่ว

2. คัดลอกตัวอักษรไทยทุกตัวให้ตรงเป๊ะ ระวังพิเศษ:
   - "ธ" ≠ "ช" ≠ "ซ"     (ธ มีหยัก, ช หางตวัด, ซ หางตัว S)
   - "น" ≠ "ม" ≠ "ก"     (น มี 2 ขา, ม มี 3 ขา, ก มีปลายแหลม)
   - "ฒ" ≠ "ฌ"            (ฒ มีฟันบน)
   - "พ" ≠ "ผ" ≠ "ฟ" ≠ "ฬ"
   - สระ "ะ ั า ิ ี ึ ื"  อย่าข้าม
   - ระยะห่าง: "ธนัย" ≠ "ธนชัย" (ดูจำนวนตัวอักษรให้ดี)

3. เลขกรมธรรม์ (policy_number):
   - คัดลอก **ทั้งตัวอักษร เลข ขีด สแลช** ตามเอกสารเป๊ะ
   - "/" (slash) ≠ "-" (dash)  อย่าแปลง
   - "O" (โอ) ≠ "0" (ศูนย์) อย่าสลับ
   - ตัวอย่างถูก: "D0-70-69/008599", "725-01333-53214", "210001/1003059347/135-M4"
   - ตัวอย่างผิด: ตัด format ออก, แทน / ด้วย -, แทน O ด้วย 0

4. ชื่อ-นามสกุล: คัดตามที่เขียน รวมคำนำหน้า ("นาย", "นาง", "นางสาว", "คุณ", "บจก.", "บริษัท")
   - "นาย ธนัย นพกิจกำจร" คือ 3 พยางค์ + 5 พยางค์ ดูให้ครบ
   - บริษัท: คัดชื่อเต็มรวม "จำกัด" / "(มหาชน)"

5. วันที่: แปลงเป็น "YYYY-MM-DD" ค.ศ. เสมอ
   - ปี >= 2500 = พ.ศ. → ลบ 543 (2569 → 2026)
   - ปี < 2500 = ค.ศ. อยู่แล้ว
   - เดือนไทย: ม.ค.=01 ก.พ.=02 มี.ค.=03 เม.ย.=04 พ.ค.=05 มิ.ย.=06
                ก.ค.=07 ส.ค.=08 ก.ย.=09 ต.ค.=10 พ.ย.=11 ธ.ค.=12

6. car_year: ค.ศ. เท่านั้น (พ.ศ. ลบ 543) — เลขเดียว เช่น 2013

7. เงิน: ตัวเลขล้วน ไม่มี comma "645.21" ไม่ใช่ "645,21" หรือ "645.21 บาท"

═══════════════════════════════════════════════════════════════
JSON keys (ตอบเป็น JSON เท่านั้น)
═══════════════════════════════════════════════════════════════

{
  "policy_number":             "เลขกรมธรรม์ — คัดทั้งหมดตามเอกสาร รวม / และ -",
  "company_code":              "รหัสบริษัทประกัน 4-6 ตัว เช่น TMSTH, SAFETY, MSIG, AXA, VIR, BUI",
  "insured_name":              "ชื่อ-นามสกุลผู้เอาประกัน รวมคำนำหน้า",
  "insured_address":           "ที่อยู่เต็ม รวมเลขบ้าน หมู่ ซอย ถนน แขวง เขต จังหวัด",
  "license_plate":             "ทะเบียนรถ เช่น 1กก 8803 กท / ฒค 5219 กท / 1ฒว 4535 กท",
  "chassis_no":                "เลขตัวถัง ตัวอักษรอังกฤษ+ตัวเลข เช่น MRHGM2620CP408631",
  "car_make":                  "ยี่ห้อรถ ตัวอักษรอังกฤษ เช่น TOYOTA, HONDA, ISUZU",
  "car_model":                 "รุ่นรถ ตัวอักษรอังกฤษ เช่น CITY, FORTUNER, MU-7",
  "car_year":                  2013,
  "coverage_start":            "YYYY-MM-DD ค.ศ.",
  "coverage_end":              "YYYY-MM-DD ค.ศ.",
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

═══════════════════════════════════════════════════════════════
ก่อนตอบ — ขั้นตอนตรวจสอบตัวเอง
═══════════════════════════════════════════════════════════════

1. อ่านเอกสารทั้งหน้าจริงๆ ก่อน อย่ารีบสรุป
2. policy_number: ดู barcode + ตาราง "เลขที่ Policy No." ตรวจให้ตรงกัน
3. insured_name: ดูใน "ผู้เอาประกัน / The Insured" หรือใต้คำว่า "ชื่อ Name"
4. ถ้า field ใดอ่านได้ไม่ชัด/เบลอ/ไม่แน่ใจ → ใส่ null อย่าเดา

ตอบเป็น JSON object เดียว ไม่มี markdown, ไม่มีคำอธิบายเพิ่ม
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
            temperature=0,              # deterministic — กันการเดา
            thinking_config=types.ThinkingConfig(thinking_budget=4096),  # เพิ่ม thinking สำหรับ OCR
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
