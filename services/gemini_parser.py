import os, json, re


def _log(message: str) -> None:
    """Log ได้แม้ Windows console ตั้ง code page ที่ไม่รองรับภาษาไทย."""
    try:
        print(message, flush=True)
    except UnicodeEncodeError:
        print(str(message).encode("ascii", "backslashreplace").decode("ascii"), flush=True)


_PROMPT = """คุณคือ OCR ผู้เชี่ยวชาญกรมธรรม์ประกันรถยนต์ภาษาไทย หน้าที่: คัดลอกข้อความตามที่เห็นในเอกสาร 100% — ไม่ใช่นักเดา ไม่ใช่นักสรุป

เอกสารที่แนบมาเป็นภาพที่เรนเดอร์จาก PDF ต้นฉบับ (อาจมีมากกว่าหนึ่งหน้า) และอาจมี
ข้อความจาก text layer ประกอบ ให้ยึดข้อความที่เห็นบนภาพเป็นหลักเสมอ โดยใช้ text layer
เพื่อช่วยตรวจตัวอักษรเล็ก ๆ เท่านั้น

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

3. เลขกรมธรรม์ (policy_number): ⚠️ จุดผิดบ่อยที่สุด — ทำตามขั้นตอนต่อไปนี้เคร่งครัด

   ━━━ STEP A: หาตำแหน่งที่ถูกต้อง ━━━
   หา **ข้อความที่เป็นตัวพิมพ์** ใต้/ข้างคำเหล่านี้:
     "กรมธรรม์ประกันภัยเลขที่"  หรือ  "เลขที่"  หรือ  "Policy No."  หรือ  "Policy Number"

   ━━━ STEP B: ห้ามอ่าน BARCODE bars เด็ดขาด ━━━
   ⛔ บนเอกสารมักจะมีรหัสแท่ง (barcode) คู่กับเลขกรมธรรม์
   ⛔ **อย่า** พยายามอ่านแท่งสีดำหรือนับจำนวนแท่ง
   ⛔ ให้อ่านเฉพาะ **ตัวเลขที่พิมพ์ด้วยตัวอักษร** (printed text) เหนือ/ใต้แท่ง barcode
   ⛔ ถ้าเห็นตัวเลขยาวผิดปกติ (>20 ตัว) มักเป็นการเข้าใจ barcode ผิด → กลับไปอ่านจาก text หัวตาราง

   ━━━ STEP C: คัดทุกอักขระตามเป๊ะ ━━━
   - คัดลอก **ทั้งตัวอักษรพิมพ์ใหญ่ เลข ขีด สแลช ช่องว่าง** ตามเอกสารเป๊ะ
   - "/" (slash) ≠ "-" (dash) ≠ " " (space)  อย่าแปลง อย่าเอาออก
   - **ตัวอักษรอังกฤษพิมพ์ใหญ่ที่นำหน้า ห้ามอ่านเป็นตัวเลข** (เรื่องนี้ผิดบ่อยที่สุด):
       - "D" ≠ "0" ≠ "8"  (D = มีเส้นตรงด้านซ้าย + โค้งครึ่งวงกลมขวา, ไม่ใช่ทรงไข่ปิดทึบ)
       - "B" ≠ "8" ≠ "3"  (B = เส้นตรงด้านซ้าย + 2 ห่วงโค้ง)
       - "O" (โอ) ≠ "0" (ศูนย์)  (O ตัวหนาสม่ำเสมอ, 0 มักผอมกว่าและมีขีดทแยงในบางฟอนต์)
       - "I" ≠ "1" ≠ "l"  (I มีเส้นบน-ล่าง, 1 มีหางเฉียง, l ไม่มีอะไรประดับ)
       - "S" ≠ "5"        (S โค้งล้วน, 5 มีเส้นตรงบน)
       - "Z" ≠ "2"        (Z มี 3 เส้นตรง, 2 มีโค้งล่าง)
       - "G" ≠ "6"        (G มีลายบนกลางห่วง, 6 ม้วนต่อเนื่อง)

   ━━━ STEP D: ตรวจรูปแบบ + ความยาวให้สมเหตุสมผล ━━━
   เลขกรมธรรม์รถยนต์ในไทยมีรูปแบบมาตรฐาน — ความยาวรวม **8-22 ตัวอักษร** เท่านั้น
   ตัวอย่างรูปแบบที่พบ:
     - "D0-70-69/011672"   (15 ตัว — Tokio Marine: D + ขีด + slash)
     - "M 1690097208"      (12 ตัว — Muangthai/Mitsui)
     - "725-01333-53214"   (15 ตัว — Bangkok Insurance)
     - "210001/1003059347/135-M4"  (24 ตัว — แบบยาว)
   - ถ้าได้ผลลัพธ์ < 8 ตัว หรือ > 25 ตัว → กลับไปอ่านใหม่
   - ถ้ามีแต่ตัวเลขล้วน 13+ หลัก ไม่มีขีด/slash → น่าจะอ่าน barcode ผิด ให้กลับไปดู text

   ━━━ STEP E: cross-check 2 ที่ในเอกสาร ━━━
   เลขเดียวกันมักปรากฏ **2 ตำแหน่ง**:
     1) หัวเอกสาร "กรมธรรม์ประกันภัยเลขที่"
     2) ตารางด้านในที่ระบุ "Policy No." / "เลขที่กรมธรรม์"
   ต้องตรงกัน 100% ถ้าไม่ตรง → เลือกที่ชัดกว่า แล้วใส่ null ถ้าทั้งคู่ไม่ชัด

   ตัวอย่างถูก: "D0-70-69/011672", "M 1690097208", "210001/1003059347/135-M4"
   ตัวอย่างผิด ที่เคยเจอจริง:
     - "80-70-88811873"   (อ่าน D เป็น 8 + อ่าน barcode bars แทน text — ยาวเกินจริง 18 ตัวล้วนเลข ผิดรูปแบบ)
     - "20-70-69/008599"  (อ่าน D เป็น 2)
     - "8 1690097208"     (อ่าน B เป็น 8)
     - "01234567890123"   (อ่าน barcode เป็นเลขล้วนยาว ไม่มีตัวอักษร/ขีด → strong signal ของ barcode reading)

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
  "doc_type":                  "motor_main | motor_prb | endorsement | credit_note | fire | sme_property | unknown",
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
ก่อนตอบ — ขั้นตอนตรวจสอบตัวเอง (ทำทุก field)
═══════════════════════════════════════════════════════════════

1. **อ่านเอกสารทั้งหน้าก่อน** — สแกนดูทั้งเอกสาร อย่ารีบจับ field แรกที่เจอ

2. **policy_number — ตรวจ 4 จุดก่อนตอบ**
   ✓ ตำแหน่งถูก (ใต้ "กรมธรรม์ประกันภัยเลขที่" / "Policy No.")?
   ✓ ไม่ได้อ่าน barcode bars (ไม่มีเลขล้วนยาว 14+ หลัก ติดกัน)?
   ✓ ความยาว 8-22 ตัวอักษร?
   ✓ ตัวอักษรพิมพ์ใหญ่ที่ตอบยังเป็นตัวอักษร ไม่ได้แปลงเป็นเลข (D, B, S, O, I)?

3. **insured_name** — ดูใน "ผู้เอาประกัน / The Insured" หรือใต้คำว่า "ชื่อ Name"
   ✓ คำนำหน้าครบ (นาย/นาง/นางสาว/คุณ/บจก./บริษัท)?

4. **license_plate** — รูปแบบ "เลข ตัวอักษรไทย เลข ตัวอักษรไทย" เช่น "1กก 1234 กท"
   ✓ ไม่ใช่ "0ก0 0000" (อ่าน "ทะเบียนรถ" หัวข้อแทน)?

5. **dates** — แปลงเป็น YYYY-MM-DD ค.ศ. (ไม่ใช่ พ.ศ.)
   ✓ ปีดูสมเหตุสมผลกับ context (กรมธรรม์ออกเร็วๆ นี้)?

6. **car_year** — ค.ศ. 4 หลัก (เช่น 2013) — ปี พ.ศ. ลบ 543 ก่อน

7. ถ้า field ใดอ่านได้ไม่ชัด/เบลอ/ไม่แน่ใจ → ใส่ null อย่าเดา **ผิดดีกว่าเดามั่ว**

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


def _validate_policy_number(s):
    """กรอง policy_number ที่ดูเหมือนอ่าน barcode bars แทน text
       - ตัวเลขล้วน 14+ หลัก โดยไม่มีตัวอักษร/ขีด/slash → น่าจะอ่าน barcode ผิด
       - ความยาวรวมเกิน 30 ตัว = ผิดรูปแบบกรมธรรม์ไทย
    """
    if not s: return None
    s = str(s).strip()
    if not s: return None
    # มีแต่ digit ล้วน + ยาว >= 14 → reject (น่าจะอ่าน barcode)
    digits_only = s.replace(" ", "")
    if digits_only.isdigit() and len(digits_only) >= 14:
        _log(f"[gemini_parser] policy_number rejected (suspect barcode): {s}")
        return None
    if len(s) > 30:
        _log(f"[gemini_parser] policy_number rejected (too long: {len(s)}): {s}")
        return None
    return s


def _doc_type(val):
    """รับเฉพาะชนิดเอกสารที่ doc_pairing รู้จัก; ค่าอื่นให้ fallback ไปจำแนกจากข้อมูล."""
    raw = str(val or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "main": "motor_main", "motor": "motor_main", "motor_policy": "motor_main",
        "prb": "motor_prb", "พรบ": "motor_prb", "p_r_b": "motor_prb",
        "fire_insurance": "fire", "sme": "sme_property", "property": "sme_property",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in {
        "motor_main", "motor_prb", "endorsement", "credit_note", "fire", "sme_property", "unknown"
    } else None


def is_available():
    return bool(os.getenv("GEMINI_API_KEY", ""))


def _ai_page_count() -> int:
    """จำนวนหน้าที่ส่งให้ AI ต่อไฟล์; จำกัด RAM ของ Render Free."""
    try:
        return max(1, min(int(os.getenv("AI_PDF_MAX_PAGES", "1")), 2))
    except ValueError:
        return 1


def _ai_render_dpi() -> int:
    """144 DPI อ่านตารางไทยชัด แต่ใช้ RAM น้อยกว่า 200 DPI มาก."""
    try:
        return max(120, min(int(os.getenv("AI_PDF_RENDER_DPI", "144")), 170))
    except ValueError:
        return 144


def _vision_mode() -> str:
    """native_pdf is the free-tier-safe default; Gemini handles PDF server-side."""
    return os.getenv("AI_VISION_MODE", "native_pdf").strip().lower()


def _render_pdf_for_vision(file_bytes: bytes) -> tuple[list[bytes], str]:
    """เรนเดอร์หน้าแรกของ PDF เป็น JPEG ชั่วคราวสำหรับ Gemini Vision.

    PDF ต้นฉบับไม่ถูกแก้ไขและไม่ถูกแปลงเพื่อเก็บข้อมูล; รูปเหล่านี้อยู่ในหน่วยความจำ
    เฉพาะระหว่างการอ่าน แล้ว PDF เดิมจะถูกอัปขึ้น Cloudflare R2 ตอนบันทึก.
    """
    # Lazy import: PyMuPDF ไม่อยู่ใน working set ของ web service จนกว่าจะมีไฟล์จริง
    import fitz

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        pages = []
        scale = _ai_render_dpi() / 72
        for page_no in range(min(doc.page_count, _ai_page_count())):
            pix = doc[page_no].get_pixmap(
                matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False
            )
            try:
                # JPEG เก็บไว้ส่ง Gemini เท่านั้น แล้วปล่อย bitmap ดิบของ PyMuPDF ทันที
                pages.append(pix.tobytes("jpeg", jpg_quality=82))
            finally:
                pix = None

        text = "\n".join(doc[i].get_text("text") for i in range(min(doc.page_count, 2))).strip()
        # กัน text layer ที่ยาว/เพี้ยนจนแย่ง context ของภาพ
        return pages, text[:12000]
    finally:
        doc.close()


def parse_with_gemini(file_bytes: bytes, filename: str = "") -> dict:
    api_key = os.getenv("GEMINI_API_KEY", "")
    if not api_key:
        raise RuntimeError("ไม่พบ GEMINI_API_KEY")

    from google import genai
    from google.genai import types

    client = genai.Client(api_key=api_key)

    _log(f"[gemini_parser] '{filename}' -> Gemini Flash Vision (rendered PDF pages)")

    image_pages, embedded_text = [], ""
    # Render ภาพใช้ RAM สูงมากบน Render Free (512MB). ค่าเริ่มต้นจึงส่ง PDF
    # ให้ Gemini อ่านโดยตรง; Google แปลง/อ่านเอกสารบนฝั่งบริการของ Gemini เอง.
    if _vision_mode() == "rendered":
        try:
            image_pages, embedded_text = _render_pdf_for_vision(file_bytes)
        except Exception as render_error:
            _log(f"[gemini_parser] render failed, using native PDF: {str(render_error)[:120]}")
    contents = [types.Part.from_text(text=_PROMPT)]
    if embedded_text:
        contents.append(types.Part.from_text(text=(
            "ข้อความที่ดึงได้จาก PDF (ใช้เพื่อตรวจทานภาพ ไม่ใช่ให้เดา):\n" + embedded_text
        )))
    for image in image_pages:
        contents.append(types.Part.from_bytes(data=image, mime_type="image/jpeg"))

    # Native PDF is also the default free-tier path; no bitmap is held in Render RAM.
    if not image_pages:
        contents.append(types.Part.from_bytes(data=file_bytes, mime_type="application/pdf"))

    response = client.models.generate_content(
        model=os.getenv("GEMINI_MODEL", "gemini-2.5-flash"),
        contents=contents,
        config=types.GenerateContentConfig(
            temperature=0,              # deterministic — กันการเดา
            thinking_config=types.ThinkingConfig(thinking_budget=4096),  # เพิ่ม thinking สำหรับ OCR
            response_mime_type="application/json",
        ),
    )

    raw = response.text
    _log(f"[gemini_parser] {len(raw)} chars")

    data = _extract_json(raw)
    result = {
        "doc_type":                 _doc_type(data.get("doc_type")),
        "policy_number":            _validate_policy_number(data.get("policy_number")),
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

    _log("[gemini_parser] parsed fields:")
    for k, v in result.items():
        if v not in (None, "", 0):
            _log(f"  {k}: {v}")

    return result
