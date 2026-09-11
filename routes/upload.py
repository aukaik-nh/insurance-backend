from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse
from services.gemini_parser import parse_with_gemini, is_available as gemini_available
from services.local_pdf_parser import parse_pdf_image_locally
from services.supabase_shim import create_client
from functools import lru_cache
import os, json, traceback, uuid, asyncio
from concurrent.futures import ThreadPoolExecutor

# จำกัดหนึ่งงานต่อ instance: Render Free มี RAM เพียง 512MB
_executor = ThreadPoolExecutor(max_workers=1)

router = APIRouter()

try:
    MAX_PDF_BYTES = max(1, int(os.getenv("MAX_PDF_BYTES", str(12 * 1024 * 1024))))
except ValueError:
    MAX_PDF_BYTES = 12 * 1024 * 1024

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
    "insured_name", "insured_address", "risk_address", "original_filename", "phone",
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


from services.document_naming import make_display_filename as _make_display_filename


def _money(value) -> float:
    try:
        return float(_clean_thai_number(str(value))) if value not in (None, "") else 0.0
    except (TypeError, ValueError):
        return 0.0


def _apply_financial_totals(save_data: dict, paired_prb_total=0) -> dict:
    values = [save_data.get(key) for key in ("net_premium", "stamp_duty", "vat", "total_premium")]
    if all(value is not None for value in values):
        expected = round(_money(values[0]) + _money(values[1]) + _money(values[2]), 2)
        if abs(expected - _money(values[3])) > 0.02:
            raise HTTPException(
                status_code=422,
                detail=f"ยอดเบี้ยไม่ตรง: เบี้ยสุทธิ + อากร + VAT ต้องเท่ากับ {expected:.2f} บาท",
            )
    net = _money(save_data.get("net_premium"))
    pct = _money(save_data.get("commission_pct"))
    if save_data.get("commission_baht") is None:
        save_data["commission_baht"] = round(net * pct / 100, 2)
    commission = _money(save_data.get("commission_baht"))
    if save_data.get("wht_10pct") is None:
        save_data["wht_10pct"] = round(commission * 0.10, 2)
    if save_data.get("total_premium") is not None:
        save_data["collected_amount"] = round(
            _money(save_data.get("total_premium"))
            + _money(paired_prb_total)
            - _money(save_data.get("prepaid_tax_1pct"))
            - commission
            + _money(save_data.get("wht_10pct"))
            + _money(save_data.get("rounding")),
            2,
        )
    return save_data


def _safe_storage_name(filename: str) -> str:
    """[DEPRECATED] เก็บไว้ backward compat กับ /upload-pdf-only route"""
    stem = os.path.splitext(filename)[0]
    ext  = os.path.splitext(filename)[1].lower() or ".pdf"
    parts = _re.findall(r'[A-Za-z0-9\-]+', stem)
    base  = '_'.join(parts)[:60] if parts else ''
    short = uuid.uuid4().hex[:6]
    return f"{base}_{short}{ext}" if base else f"{short}{ext}"


_ILLEGAL_FS = _re.compile(r'[<>:"/\\|?*\x00-\x1f]')

def _baby78_storage_key(filename: str, supabase) -> str:
    """Use an immutable ID; display names never determine storage identity."""
    return f"policies/{uuid.uuid4().hex}.pdf"


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
    """Upload using an immutable object ID, independent of the display name."""
    try:
        storage_path = _baby78_storage_key(filename, supabase)
        supabase.storage.from_(BUCKET_NAME).upload(
            path=storage_path,
            file=file_bytes,
            file_options={"content-type": "application/pdf"},
        )
        url_res = supabase.storage.from_(BUCKET_NAME).get_public_url(storage_path)
        if isinstance(url_res, str):
            return url_res
        return url_res.get("publicUrl") or url_res.get("publicURL") or str(url_res)
    except Exception as e:
        print(f"[upload-storage] WARNING: ไม่สามารถอัปโหลด PDF ได้: {e}")
        return None


@router.post("/preview-pdf-local")
async def preview_pdf_local(file: UploadFile = File(...)):
    """PDF -> image -> Thai/English Tesseract using only free local Python tools."""
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    if file.size and file.size > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB")
    file_bytes = await file.read(MAX_PDF_BYTES + 1)
    if len(file_bytes) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB")

    loop = asyncio.get_event_loop()
    parsed = await loop.run_in_executor(
        _executor, lambda: parse_pdf_image_locally(file_bytes, filename=filename)
    )
    preview = parsed.pop("preview", {})
    if parsed.get("parse_error"):
        messages = {
            "ocr_dependency_missing": "ระบบอ่านข้อความยังไม่พร้อมใช้งาน กรุณาให้ผู้ดูแลตรวจการติดตั้ง ระหว่างนี้ดูเอกสารและกรอกเองได้",
            "ocr_failed": "อ่านข้อความไม่สำเร็จ แต่ยังดูภาพต้นฉบับและกรอกข้อมูลเองได้",
            "render_failed": "เปิด PDF ไม่สำเร็จ กรุณาตรวจว่าไฟล์เปิดได้และไม่ได้ตั้งรหัสผ่าน",
        }
        parsed["parse_error"] = messages.get(parsed.get("parse_error_code"), "ระบบอ่านเอกสารไม่พร้อมใช้งาน กรุณาติดต่อผู้ดูแลหรือกรอกข้อมูลเอง")
    return {
        "success": parsed.get("parse_engine") != "local_ocr_failed",
        "parsed": parsed,
        "preview": preview,
        "used_ai": False,
        "parse_engine": parsed.get("parse_engine"),
        "parse_confidence": parsed.get("parse_confidence", 0),
        "requires_review": parsed.get("requires_review", True),
        "pdf_filename": filename,
        "pdf_size": len(file_bytes),
    }


_VERIFIED_FIELDS = (
    "doc_type", "policy_number", "company_code", "app_number", "policy_type", "new_renew",
    "insured_name", "insured_address", "phone", "license_plate", "license_province",
    "chassis_no", "car_make", "car_model", "car_year", "sum_insured",
    "coverage_start", "coverage_end", "net_premium", "stamp_duty", "vat",
    "total_premium", "third_party_per_person", "third_party_per_accident", "own_damage",
    "broker_name", "broker_license", "agent_code",
)


def _comparison_value(value) -> str:
    """Normalize only for comparison; never alter the value shown to the user."""
    return _re.sub(r"[^0-9A-Za-zก-๙]", "", str(value or "")).casefold()


def _merge_verified_result(local: dict, ai: dict) -> dict:
    """Use structured vision values, retaining local OCR evidence and disagreements."""
    merged = dict(local)
    warnings = list(local.get("parse_warnings") or [])
    evidence = {key: dict(value) for key, value in (local.get("field_evidence") or {}).items()}

    for field in _VERIFIED_FIELDS:
        ai_value = ai.get(field)
        local_value = local.get(field)
        local_item = evidence.get(field, {})
        local_read = local_item.get("manual_value") or local_item.get("text") or local_value
        if ai_value not in (None, ""):
            merged[field] = ai_value
            if local_read not in (None, "") and _comparison_value(local_read) != _comparison_value(ai_value):
                warnings.append(f"{field}: ผลอ่านสองระบบไม่ตรงกัน กรุณาตรวจช่องนี้กับ PDF")
            evidence[field] = {
                **local_item,
                "status": "review" if local_read not in (None, "") and _comparison_value(local_read) != _comparison_value(ai_value) else "candidate",
                "value": ai_value,
                "manual_value": ai_value,
                "text": str(ai_value),
                "label": local_item.get("label") or field,
                "source": "document_vision",
            }
        elif local_value not in (None, ""):
            merged[field] = local_value

    if not merged.get("policy_type"):
        if merged.get("doc_type") == "motor_main":
            merged["policy_type"] = "M"
        elif merged.get("doc_type") == "motor_prb":
            merged["policy_type"] = "P"

    money = [merged.get(field) for field in ("net_premium", "stamp_duty", "vat", "total_premium")]
    if all(value is not None for value in money):
        net, stamp, vat, total = money
        if abs(round(float(net) + float(stamp) + float(vat), 2) - round(float(total), 2)) > 1:
            warnings.append("ยอดเงินไม่ผ่านสมการ เบี้ยสุทธิ + อากร + VAT กรุณาตรวจจาก PDF")

    merged.update({
        "parse_engine": "document_vision_verified",
        "used_ai": True,
        "requires_review": True,
        "parse_warnings": list(dict.fromkeys(warnings)),
        "field_evidence": evidence,
        "review_fields": [key for key, item in evidence.items() if item.get("status") == "review"],
        "raw_text": local.get("raw_text") or "",
        "text_scope": local.get("text_scope") or "page",
    })
    return merged


async def _parse_with_ai_retry(loop, file_bytes: bytes, filename: str) -> dict:
    """Retry transient provider failures without repeating local OCR."""
    last_error = None
    for attempt in range(3):
        try:
            return await loop.run_in_executor(
                _executor, lambda: parse_with_gemini(file_bytes, filename=filename) or {}
            )
        except Exception as exc:
            last_error = exc
            message = str(exc).lower()
            transient = any(token in message for token in (
                "503", "unavailable", "high demand", "timeout", "timed out",
            ))
            if not transient or attempt == 2:
                raise
            await asyncio.sleep(2 * (attempt + 1))
    raise last_error


@router.post("/preview-pdf-verified")
async def preview_pdf_verified(file: UploadFile = File(...)):
    """Read once locally for evidence, then use document vision to complete the form."""
    filename = file.filename or "document.pdf"
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    if file.size and file.size > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB")
    file_bytes = await file.read(MAX_PDF_BYTES + 1)
    if len(file_bytes) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB")

    loop = asyncio.get_event_loop()
    local = await loop.run_in_executor(
        _executor, lambda: parse_pdf_image_locally(file_bytes, filename=filename)
    )
    preview = local.pop("preview", {})
    parsed = local
    used_ai = False
    ai_error = None
    if gemini_available():
        try:
            ai = await _parse_with_ai_retry(loop, file_bytes, filename)
            parsed = _merge_verified_result(local, ai)
            used_ai = True
        except Exception as exc:
            ai_error = str(exc)[:160]
            parsed["parse_warnings"] = list(dict.fromkeys(
                (parsed.get("parse_warnings") or [])
                + ["ตัวอ่านเอกสารหลักไม่พร้อม จึงแสดงผล OCR สำรอง กรุณาตรวจทุกช่อง"]
            ))

    return {
        "success": parsed.get("parse_engine") != "local_ocr_failed",
        "parsed": parsed,
        "preview": preview,
        "used_ai": used_ai,
        "ai_error": ai_error,
        "parse_engine": parsed.get("parse_engine"),
        "parse_confidence": parsed.get("parse_confidence", 0),
        "requires_review": parsed.get("requires_review", True),
        "pdf_filename": filename,
        "pdf_size": len(file_bytes),
    }


@router.post("/preview-pdf")
async def preview_pdf(file: UploadFile = File(...)):
    """Extract เลขเบี้ย/วันที่จาก PDF ผ่าน Gemini Vision — ไม่ upload, ไม่ save DB
    ใช้สำหรับ pre-fill ตอนเลือกไฟล์ พ.ร.บ. บนหน้า /upload"""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    if file.size and file.size > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB กรุณาลดขนาดไฟล์ก่อนอ่าน")
    if not gemini_available():
        raise HTTPException(
            status_code=503,
            detail="ยังไม่ได้ตั้งค่า GEMINI_API_KEY ใน backend — AI จึงไม่สามารถอ่านเอกสารได้",
        )
    file_bytes = await file.read()
    if len(file_bytes) > MAX_PDF_BYTES:
        raise HTTPException(status_code=413, detail="ไฟล์ PDF ใหญ่เกินกำหนด 12 MB กรุณาลดขนาดไฟล์ก่อนอ่าน")
    loop = asyncio.get_event_loop()
    try:
        parsed = await loop.run_in_executor(
            _executor, lambda: parse_with_gemini(file_bytes, filename=file.filename) or {}
        )
    except Exception as e:
        message = str(e)
        print(f"[preview-pdf] gemini error: {message[:200]}")
        if "API_KEY_INVALID" in message or "API key not valid" in message:
            raise HTTPException(
                status_code=502,
                detail="GEMINI_API_KEY ไม่ถูกต้องหรือถูกปิดใช้งาน กรุณาสร้าง API key ใหม่จาก Google AI Studio แล้วตั้งค่า backend ใหม่",
            )
        raise HTTPException(status_code=502, detail=f"AI อ่านเอกสารไม่สำเร็จ: {message[:160]}")
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
    paired_prb_total = data.get("paired_prb_total")

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
    _apply_financial_totals(save_data, paired_prb_total)

    # Auto-rename pdf_filename ตามประเภทกรมธรรม์ (ถ้ามีไฟล์)
    # motor → ทะเบียน, fire → ที่อยู่, PA/TA → ชื่อ
    # ⚠️ เคารพชื่อที่ frontend ส่งมา ถ้ามี — frontend ใช้ computeDisplayFilename() ตั้งชื่ออัตโนมัติ
    # ตรงกับ backend อยู่แล้ว (WYSIWYG) เว้นแต่ user จะแก้ชื่อเอง → ก็เก็บตามที่แก้
    if save_data.get("pdf_url") or save_data.get("pdf_filename"):
        provided = save_data.get("pdf_filename")
        if not provided:
            save_data["pdf_filename"] = _make_display_filename(
                plate=save_data.get("license_plate"),
                doc_type="main",
                coverage_start=save_data.get("coverage_start"),
                coverage_end=save_data.get("coverage_end"),
                policy_type=save_data.get("policy_type"),
                risk_address=data.get("risk_address"),
                name=save_data.get("insured_name"),
            )

    if save_data.get("pdf_filename") == "รอตรวจข้อมูล.pdf":
        raise HTTPException(status_code=422, detail="ข้อมูลตั้งชื่อไฟล์ไม่ครบ กรุณาตรวจข้อมูลก่อนบันทึก")

    print("[save-policy] saving:", save_data)
    if skipped:
        print("[save-policy] skipped:", list(skipped.keys()))

    try:
        result = supabase.table("insurance_policies").insert(save_data).execute()
        return {"success": True, "id": result.data[0]["id"]}
    except Exception as e:
        print("[save-policy] ERROR:\n", traceback.format_exc())
        raise HTTPException(status_code=500, detail=f"Database error: {str(e)}")
