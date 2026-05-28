from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from services.pdf_extractor import extract_text_from_pdf
from services.claude_parser import parse_insurance_data
from services.gemini_parser import parse_with_gemini, is_available as gemini_available
from services.supabase_shim import create_client
from functools import lru_cache
import os, json, traceback, uuid, asyncio
from concurrent.futures import ThreadPoolExecutor

_executor = ThreadPoolExecutor(max_workers=4)

router = APIRouter()

# cache client at module level — reuse connection pool (ดู comment ใน routes/policies.py)
@lru_cache(maxsize=1)
def get_supabase():
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_KEY")
    )

import re as _re

# columns ที่มีใน Supabase table จริงๆ
ALLOWED_COLUMNS = {
    "policy_number", "company_code", "app_number", "policy_type",
    "new_renew", "agent_code",
    "insured_name", "insured_address", "phone",
    "license_plate", "license_province", "chassis_no",
    "car_make", "car_model", "car_year", "sum_insured",
    "coverage_start", "coverage_end",
    "date_notify", "date_cancel", "date_policy_receive",
    "net_premium", "stamp_duty", "vat", "total_premium",
    "third_party_per_person", "third_party_per_accident", "own_damage",
    "broker_name", "broker_license", "manually_edited",
    "pdf_url", "pdf_filename", "pdf_data", "pdf_size", "notes",
    # commission / หัก ณ ที่จ่าย / ปัดเศษ / เรียกเก็บ
    "prepaid_tax_1pct", "commission_pct", "commission_baht",
    "wht_10pct", "rounding", "collected_amount",
}

INT_FIELDS   = {"car_year", "pdf_size"}
FLOAT_FIELDS = {"net_premium", "stamp_duty", "vat", "total_premium",
                "third_party_per_person", "third_party_per_accident",
                "own_damage", "sum_insured",
                "prepaid_tax_1pct", "commission_pct", "commission_baht",
                "wht_10pct", "rounding", "collected_amount"}
DATE_FIELDS  = {"coverage_start", "coverage_end",
                "date_notify", "date_cancel", "date_policy_receive"}

BUCKET_NAME = "policy-pdfs"  # Supabase Storage bucket (primary)


def _make_display_filename(plate: str | None, doc_type: str, coverage_end: str | None) -> str:
    """ชื่อไฟล์สำหรับ display + download (ภาษาไทย OK)
       รูปแบบ: '{ทะเบียน} {ประเภท}.{ปี พ.ศ. 2 หลัก}.pdf'
       เช่น   '1ฒว4535กท พรบ.69.pdf'
              '1กก8803 กธ.70.pdf'
    """
    type_thai = {
        "prb": "พรบ",
        "endorsement": "สลักหลัง",
        "main": "กธ",
    }.get(doc_type, "เอกสาร")

    plate_clean = _re.sub(r'\s+', '', (plate or '').strip())
    if not plate_clean:
        plate_clean = "ไม่ทราบ"

    yy = ""
    if coverage_end:
        m = _re.search(r'(\d{4})', str(coverage_end))
        if m:
            y = int(m.group(1))
            if y < 2500:
                y += 543
            yy = str(y)[-2:]

    if yy:
        return f"{plate_clean} {type_thai}.{yy}.pdf"
    return f"{plate_clean} {type_thai}.pdf"


def _safe_storage_name(filename: str) -> str:
    """Storage key ต้องเป็น ASCII (Supabase requirement)
    ดึงตัวเลข + ASCII จาก filename + UUID สั้น
    เช่น 'กก5226 พรบ66.pdf' → '5226_66_a3f2c1.pdf'"""
    stem = os.path.splitext(filename)[0]
    ext  = os.path.splitext(filename)[1].lower() or ".pdf"
    parts = _re.findall(r'[A-Za-z0-9\-]+', stem)
    base  = '_'.join(parts)[:60] if parts else ''
    short = uuid.uuid4().hex[:6]
    return f"{base}_{short}{ext}" if base else f"{short}{ext}"


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
    Storage path: policies/{safe_filename}_{short_id}.pdf
    คืนค่า public URL หรือ None ถ้าล้มเหลว
    """
    try:
        unique_name  = _safe_storage_name(filename)
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


@router.post("/preview-pdf")
async def preview_pdf(file: UploadFile = File(...)):
    """Extract เลขเบี้ย/วันที่จาก PDF ผ่าน Gemini — ไม่ upload, ไม่ save DB
    ใช้สำหรับ pre-fill ตอนเลือกไฟล์ พ.ร.บ. บนหน้า /upload"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    file_bytes = await file.read()
    loop = asyncio.get_event_loop()
    try:
        parsed = await loop.run_in_executor(
            _executor, lambda: parse_with_gemini(file_bytes, filename=file.filename) or {}
        )
    except Exception as e:
        print(f"[preview-pdf] gemini error: {str(e)[:200]}")
        parsed = {}
    return {
        "success": True,
        "parsed": parsed,
        "pdf_filename": file.filename,
        "pdf_size": len(file_bytes),
    }


@router.post("/upload-pdf-only")
async def upload_pdf_only(file: UploadFile = File(...)):
    """อัปโหลด PDF ไป Supabase Storage อย่างเดียว — ไม่ใช้ Gemini, ไม่ insert DB
    ใช้สำหรับ batch migration — caller ต้อง match + update DB เอง"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    file_bytes = await file.read()
    loop = asyncio.get_event_loop()
    supabase = get_supabase()
    pdf_url = await loop.run_in_executor(
        _executor, lambda: _upload_pdf_to_storage(supabase, file_bytes, file.filename)
    )
    if not pdf_url:
        raise HTTPException(status_code=500, detail="upload to storage failed")
    return {"success": True, "pdf_url": pdf_url, "pdf_filename": file.filename, "pdf_size": len(file_bytes)}


@router.post("/upload-pdf")
async def upload_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")

    file_bytes = await file.read()
    loop = asyncio.get_event_loop()

    # ── OCR + Storage upload พร้อมกัน ──────────────────────────
    supabase = get_supabase()

    async def run_ocr():
        # ── Gemini Vision (หลัก) ──────────────────────────────
        if gemini_available():
            print("[upload] ใช้ Gemini 1.5 Flash Vision parser")
            try:
                result = await loop.run_in_executor(
                    _executor, lambda: parse_with_gemini(file_bytes, filename=file.filename)
                )
                return "[Gemini Vision]", result, True, None
            except Exception as e:
                print(f"[upload] Gemini ERROR: {str(e)[:200]}")
                return "", {}, False, "rate_limit"

        # ── ไม่มี API key ──────────────────────────────────────
        print("[upload] ไม่มี GEMINI_API_KEY → ฟอร์มเปล่า")
        return "", {}, False, "rate_limit"

    async def run_storage():
        return await loop.run_in_executor(
            _executor, lambda: _upload_pdf_to_storage(supabase, file_bytes, file.filename)
        )

    # รัน OCR และ upload พร้อมกัน
    (raw_text, parsed, used_ai, ai_error), pdf_url = await asyncio.gather(run_ocr(), run_storage())

    parsed["raw_text"]     = raw_text
    parsed["pdf_filename"] = file.filename
    parsed["pdf_url"]      = pdf_url
    parsed["pdf_size"]     = len(file_bytes)

    return JSONResponse(content={
        "success":  True,
        "parsed":   parsed,
        "used_ai":  used_ai,
        "ai_error": ai_error,
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

    # Auto-rename pdf_filename ตามทะเบียน + ปี (ถ้ามีไฟล์)
    if save_data.get("pdf_url") or save_data.get("pdf_filename"):
        save_data["pdf_filename"] = _make_display_filename(
            save_data.get("license_plate"),
            "main",
            save_data.get("coverage_end"),
        )

    print("[save-policy] saving:", save_data)
    if skipped:
        print("[save-policy] skipped:", list(skipped.keys()))

    try:
        result = supabase.table("insurance_policies").insert(save_data).execute()
        return {"success": True, "id": result.data[0]["id"]}
    except Exception as e:
        print("[save-policy] ERROR:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")