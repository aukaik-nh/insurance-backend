from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from services.pdf_extractor import extract_text_from_pdf
from services.claude_parser import parse_insurance_data
from services.gemini_parser import parse_with_gemini, is_available as gemini_available
from supabase import create_client
import os
import traceback
import uuid
import base64
import asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

router = APIRouter()

def get_supabase():
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_KEY")
    )

import re as _re

# columns ที่มีใน Supabase table จริงๆ
ALLOWED_COLUMNS = {
    "policy_number", "company_code", "insured_name", "insured_address",
    "phone", "license_plate", "chassis_no", "car_make", "car_model", "car_year",
    "coverage_start", "coverage_end", "net_premium", "stamp_duty", "vat",
    "total_premium", "third_party_per_person", "third_party_per_accident",
    "own_damage", "broker_name", "broker_license", "manually_edited",
    "pdf_url", "pdf_filename", "pdf_data", "pdf_size", "notes",
}

INT_FIELDS   = {"car_year", "pdf_size"}
FLOAT_FIELDS = {"net_premium", "stamp_duty", "vat", "total_premium",
                "third_party_per_person", "third_party_per_accident", "own_damage"}
DATE_FIELDS  = {"coverage_start", "coverage_end"}

BUCKET_NAME = "policy-pdfs"


def _clean_thai_number(val: str) -> str:
    """แปลงตัวเลขไทย → อารบิก และลบ comma"""
    if not val:
        return val
    thai_digits = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
    return str(val).translate(thai_digits).replace(",", "").strip()


def _normalize_date(val: str) -> str | None:
    """แปลง DD/MM/YYYY (พ.ศ.) → YYYY-MM-DD (ค.ศ.) สำหรับ Supabase"""
    if not val:
        return None
    val = val.strip()

    m = _re.match(r'^(\d{4})-(\d{2})-(\d{2})$', val)
    if m:
        y = int(m.group(1))
        if y >= 2500:
            y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"

    m = _re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', val)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        if y >= 2500:
            y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"

    return None


def _upload_pdf_to_storage(supabase, file_bytes: bytes, filename: str) -> str | None:
    """
    อัปโหลด PDF ไปยัง Supabase Storage bucket 'policy-pdfs'
    คืนค่า public URL หรือ None ถ้าล้มเหลว
    """
    try:
        ext          = os.path.splitext(filename)[1] or ".pdf"
        unique_name  = f"{uuid.uuid4().hex}{ext}"
        storage_path = f"policies/{unique_name}"

        supabase.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": "application/pdf"},
        )

        # supabase-py v1 คืน string, v2 คืน object
        url_res = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
        if isinstance(url_res, str):
            return url_res
        return url_res.get("publicUrl") or url_res.get("publicURL") or str(url_res)

    except Exception as e:
        print(f"[upload-storage] WARNING: ไม่สามารถอัปโหลด PDF ได้: {e}")
        return None


@router.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")

    file_bytes = await file.read()
    loop = asyncio.get_event_loop()

    # ── OCR + Storage upload พร้อมกัน ──────────────────────────
    supabase = get_supabase()

    used_ai   = False
    ai_error  = None

    async def run_ocr():
        nonlocal used_ai, ai_error
        if gemini_available():
            print("[upload] ใช้ Gemini Vision parser")
            try:
                result = await loop.run_in_executor(_executor, lambda: parse_with_gemini(file_bytes, filename=file.filename))
                used_ai = True
                return "[Gemini Vision]", result
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "quota" in err_str.lower() or "rate" in err_str.lower() or "exhausted" in err_str.lower():
                    ai_error = "rate_limit"
                    print(f"[upload] Gemini rate limit → fallback OCR")
                else:
                    ai_error = "error"
                    print(f"[upload] Gemini ล้มเหลว ({e}) → fallback OCR")
        print("[upload] ใช้ OCR + regex parser")
        raw = await loop.run_in_executor(_executor, lambda: extract_text_from_pdf(file_bytes))
        if not raw.strip():
            raise HTTPException(status_code=400, detail="ไม่สามารถอ่านข้อความจาก PDF ได้")
        result = await loop.run_in_executor(_executor, lambda: parse_insurance_data(raw, filename=file.filename))
        return raw, result

    async def run_storage():
        return await loop.run_in_executor(
            _executor, lambda: _upload_pdf_to_storage(supabase, file_bytes, file.filename)
        )

    # รัน OCR และ upload พร้อมกัน
    (raw_text, parsed), pdf_url = await asyncio.gather(run_ocr(), run_storage())

    pdf_b64 = base64.b64encode(file_bytes).decode("ascii")

    parsed["raw_text"]     = raw_text
    parsed["pdf_filename"] = file.filename
    parsed["pdf_url"]      = pdf_url
    parsed["pdf_data"]     = pdf_b64
    parsed["pdf_size"]     = len(file_bytes)

    return JSONResponse(content={
        "success":  True,
        "parsed":   parsed,
        "used_ai":  used_ai,
        "ai_error": ai_error,   # "rate_limit" | "error" | None
    })


@router.post("/save-policy")
async def save_policy(data: dict):
    supabase = get_supabase()

    save_data = {}
    skipped   = {}

    for k, v in data.items():
        if k == "raw_text":          # ไม่เก็บ raw text ลง DB
            continue

        if k not in ALLOWED_COLUMNS:
            skipped[k] = v
            continue

        if k in INT_FIELDS:
            try:
                clean = _clean_thai_number(str(v)) if v not in (None, "") else ""
                save_data[k] = int(float(clean)) if clean not in ("", "None", "null") else None
            except (ValueError, TypeError):
                save_data[k] = None

        elif k in FLOAT_FIELDS:
            try:
                clean = _clean_thai_number(str(v)) if v not in (None, "") else ""
                save_data[k] = float(clean) if clean not in ("", "None", "null") else None
            except (ValueError, TypeError):
                save_data[k] = None

        elif k in DATE_FIELDS:
            val = str(v).strip() if v else ""
            save_data[k] = _normalize_date(val)

        else:
            # string fields รวมถึง pdf_url, pdf_filename
            if v in (None, "", "null", "test"):
                save_data[k] = None
            else:
                save_data[k] = str(v).strip() or None

    save_data["manually_edited"] = True

    print("[save-policy] saving:", save_data)
    if skipped:
        print("[save-policy] skipped:", list(skipped.keys()))

    try:
        result = supabase.table("insurance_policies").insert(save_data).execute()
        return {"success": True, "id": result.data[0]["id"]}
    except Exception as e:
        print("[save-policy] ERROR:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")