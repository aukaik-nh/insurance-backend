"""
gemini_parser.py — parse กรมธรรม์ด้วย Google Gemini Vision API
แม่นยำกว่า OCR+regex มาก เพราะ Gemini เข้าใจภาษาไทย + layout เอกสารในขั้นตอนเดียว

ฟรี: 1,500 requests/วัน บน gemini-1.5-flash
API key: https://aistudio.google.com → Get API key
.env:    GEMINI_API_KEY=AIzaSy...
"""

import os
import json
import re
import io

import fitz          # PyMuPDF
from PIL import Image

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

# ──────────────────────────────────────────────────────────────
# PROMPT — บอก Gemini ว่าต้องการข้อมูลอะไร
# ──────────────────────────────────────────────────────────────
_PROMPT = """You are a data extraction specialist for Thai car insurance policy documents.

Extract ALL of the following fields from the document image(s) and return a single JSON object.
Use null for any field you cannot find. Do NOT guess.

Required JSON keys:
{
  "policy_number":             "เลขกรมธรรม์ / policy number",
  "company_code":              "ชื่อย่อบริษัทประกัน เช่น SAFETY, MSIG, AXA, VIRIYAH, TOKIO",
  "insured_name":              "ชื่อ-นามสกุลผู้เอาประกันภัย (ภาษาไทย)",
  "insured_address":           "ที่อยู่ผู้เอาประกันภัย",
  "license_plate":             "ทะเบียนรถ เช่น 1กก 1234 กรุงเทพมหานคร",
  "chassis_no":                "เลขตัวถัง / VIN / Chassis No",
  "car_make":                  "ยี่ห้อรถยนต์ เช่น TOYOTA, HONDA, ISUZU",
  "car_model":                 "รุ่นรถยนต์",
  "car_year":                  2024,
  "coverage_start":            "YYYY-MM-DD",
  "coverage_end":              "YYYY-MM-DD",
  "net_premium":               12345.67,
  "stamp_duty":                123.45,
  "vat":                       864.98,
  "total_premium":             13334.10,
  "third_party_per_person":    null,
  "third_party_per_accident":  null,
  "own_damage":                null,
  "broker_name":               "ชื่อตัวแทน/นายหน้า",
  "broker_license":            "เลขใบอนุญาตนายหน้า"
}

Critical rules:
1. Dates: convert Buddhist Era (พ.ศ.) to Christian Era (ค.ศ.) by subtracting 543.
   Example: 14 กุมภาพันธ์ 2567 → "2024-02-14"
2. car_year: integer, Christian Era (ค.ศ.)
3. Monetary values: numbers only, no commas or currency symbols
4. Return ONLY the JSON object — no explanation, no markdown fences
"""

# ──────────────────────────────────────────────────────────────
# PDF → PIL Images
# ──────────────────────────────────────────────────────────────
def _pdf_to_images(file_bytes: bytes, max_pages: int = 4, dpi: int = 180) -> list:
    """แปลง PDF เป็น list ของ PIL Image (เอาแค่หน้าแรกๆ)"""
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    images = []
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        mat = fitz.Matrix(dpi / 72, dpi / 72)
        pix = page.get_pixmap(matrix=mat)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        images.append(img)
    doc.close()
    return images


# ──────────────────────────────────────────────────────────────
# JSON extractor (รองรับ Gemini ที่บางทียังใส่ markdown)
# ──────────────────────────────────────────────────────────────
def _extract_json(text: str) -> dict:
    text = re.sub(r'```json\s*', '', text)
    text = re.sub(r'```\s*', '', text)
    text = text.strip()
    m = re.search(r'\{[\s\S]*\}', text)
    if m:
        return json.loads(m.group())
    raise ValueError(f"ไม่พบ JSON ใน Gemini response:\n{text[:300]}")


# ──────────────────────────────────────────────────────────────
# Normalize helpers
# ──────────────────────────────────────────────────────────────
def _normalize_year(val) -> int | None:
    if val is None:
        return None
    try:
        y = int(str(val).strip().split('.')[0])
        if y >= 2500:
            y -= 543
        if 1980 <= y <= 2060:
            return y
    except (ValueError, TypeError):
        pass
    return None


def _normalize_date(val) -> str | None:
    if not val:
        return None
    s = str(val).strip()
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        y = int(m.group(1))
        if y >= 2500:
            y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    # DD/MM/YYYY
    m = re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if y >= 2500:
            y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _normalize_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        return float(str(val).replace(",", "").strip())
    except (ValueError, TypeError):
        return None


# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────
def is_available() -> bool:
    """ตรวจว่าตั้ง GEMINI_API_KEY แล้วหรือยัง"""
    return bool(GEMINI_API_KEY)


def parse_with_gemini(file_bytes: bytes, filename: str = "") -> dict:
    """
    ส่ง PDF ไปให้ Gemini Vision อ่าน แล้วคืน dict เหมือน claude_parser
    Raises RuntimeError ถ้าไม่มี API key
    Raises Exception ถ้า Gemini ล้มเหลว
    """
    if not GEMINI_API_KEY:
        raise RuntimeError(
            "ไม่พบ GEMINI_API_KEY\n"
            "วิธีตั้ง:\n"
            "  1. ไปที่ https://aistudio.google.com → Get API key\n"
            "  2. เพิ่มบรรทัดนี้ใน D:\\insurance-backend\\.env\n"
            "     GEMINI_API_KEY=AIzaSy...\n"
            "  3. รีสตาร์ท backend"
        )

    import google.generativeai as genai
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-1.5-flash")

    # แปลง PDF → รูป
    images = _pdf_to_images(file_bytes, max_pages=4, dpi=180)
    if not images:
        raise ValueError("แปลง PDF เป็นรูปภาพไม่ได้")

    print(f"[gemini_parser] '{filename}' → ส่ง {len(images)} หน้า → Gemini...")

    # ส่งรูปทุกหน้าพร้อมกัน (Gemini รับได้หลายรูปใน 1 request)
    response = model.generate_content([_PROMPT] + images)
    raw_text = response.text

    print(f"[gemini_parser] response {len(raw_text)} chars")
    if len(raw_text) < 200:
        print(f"[gemini_parser] raw: {raw_text}")

    # parse JSON
    data = _extract_json(raw_text)

    # normalize ทุก field
    result = {
        "policy_number":            data.get("policy_number") or None,
        "company_code":             data.get("company_code") or None,
        "insured_name":             data.get("insured_name") or None,
        "insured_address":          data.get("insured_address") or None,
        "license_plate":            data.get("license_plate") or None,
        "chassis_no":               data.get("chassis_no") or None,
        "car_make":                 data.get("car_make") or None,
        "car_model":                data.get("car_model") or None,
        "car_year":                 _normalize_year(data.get("car_year")),
        "coverage_start":           _normalize_date(data.get("coverage_start")),
        "coverage_end":             _normalize_date(data.get("coverage_end")),
        "net_premium":              _normalize_float(data.get("net_premium")),
        "stamp_duty":               _normalize_float(data.get("stamp_duty")),
        "vat":                      _normalize_float(data.get("vat")),
        "total_premium":            _normalize_float(data.get("total_premium")),
        "third_party_per_person":   _normalize_float(data.get("third_party_per_person")),
        "third_party_per_accident": _normalize_float(data.get("third_party_per_accident")),
        "own_damage":               _normalize_float(data.get("own_damage")),
        "broker_name":              data.get("broker_name") or None,
        "broker_license":           data.get("broker_license") or None,
    }

    # ล้าง string ว่างและ "null" literal
    for k, v in result.items():
        if isinstance(v, str) and v.strip().lower() in ("", "null", "none", "n/a", "-"):
            result[k] = None

    print("[gemini_parser] parsed result:")
    for k, v in result.items():
        if v not in (None, "", 0):
            print(f"  {k}: {v}")

    return result
