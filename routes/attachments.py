"""
Attachments routes — เอกสารแนบหลายไฟล์ต่อ 1 กรมธรรม์
ประเภท: prb (พ.ร.บ.) | endorsement (สลักหลัง) | other (อื่นๆ)
หมายเหตุ: main ใช้คอลัมน์ pdf_url/pdf_filename ใน insurance_policies (ของเดิม)
"""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from services.supabase_shim import create_client
from services.gemini_parser import parse_with_gemini, is_available as gemini_available
from functools import lru_cache
import os, httpx, asyncio
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from routes.upload import _upload_pdf_to_storage, BUCKET_NAME, _make_display_filename

_executor_extract = ThreadPoolExecutor(max_workers=2)

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=4)

ALLOWED_TYPES = {"prb", "endorsement", "other"}


# cache client at module level — reuse connection pool (ดู comment ใน routes/policies.py)
@lru_cache(maxsize=1)
def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _is_supabase_storage_url(url: str) -> bool:
    return bool(url) and "/storage/v1/object/public/" in url

def _is_r2_url(url: str) -> bool:
    if not url: return False
    r2_pub = os.getenv("R2_PUBLIC_URL", "")
    return (r2_pub and url.startswith(r2_pub.rstrip("/"))) or ".r2.dev/" in url


@router.get("/policies/{policy_id}/attachments")
async def list_attachments(policy_id: str):
    """ดึงเอกสารแนบทั้งหมดของกรมธรรม์ (ไม่รวมไฟล์หลัก)"""
    supabase = get_supabase()
    try:
        result = (
            supabase.table("policy_attachments")
            .select("*")
            .eq("policy_id", policy_id)
            .order("created_at", desc=False)
            .execute()
        )
        return {"data": result.data or []}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _to_float(v):
    if v in (None, "", "null"): return None
    try:
        s = str(v).translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def _to_date(v):
    """แปลง YYYY-MM-DD หรือ DD/MM/YYYY (รองรับ พ.ศ.) → YYYY-MM-DD"""
    if not v: return None
    import re as _re
    s = str(v).strip()
    m = _re.match(r'^(\d{4})-(\d{2})-(\d{2})$', s)
    if m:
        y = int(m.group(1))
        if y >= 2500: y -= 543
        return f"{y:04d}-{m.group(2)}-{m.group(3)}"
    m = _re.match(r'^(\d{1,2})/(\d{1,2})/(\d{2,4})$', s)
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100: y += 2000
        if y >= 2500: y -= 543
        return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


@router.post("/policies/{policy_id}/attachments")
async def upload_attachment(
    policy_id: str,
    doc_type: str = Form(...),
    label: str = Form(""),
    note: str = Form(""),
    net_premium:    str = Form(""),
    stamp_duty:     str = Form(""),
    vat:            str = Form(""),
    total_premium:  str = Form(""),
    coverage_start: str = Form(""),
    coverage_end:   str = Form(""),
    file: UploadFile = File(...),
):
    """อัปโหลดเอกสารแนบใหม่ (พ.ร.บ. / สลักหลัง / อื่นๆ) + เบี้ยถ้ามี"""
    if doc_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"doc_type ต้องเป็น {ALLOWED_TYPES}")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")

    file_bytes = await file.read()
    supabase = get_supabase()

    # ตรวจว่ามี policy อยู่จริง
    policy = supabase.table("insurance_policies").select("id, license_plate").eq("id", policy_id).execute()
    if not policy.data:
        raise HTTPException(status_code=404, detail="ไม่พบกรมธรรม์")
    parent_plate = policy.data[0].get("license_plate") or ""

    loop = asyncio.get_event_loop()

    # ── Auto-extract เลขเบี้ย (เฉพาะ พ.ร.บ. ที่ user ไม่ได้กรอกเอง) ──────────────
    auto = {}
    needs_extract = (
        doc_type == "prb"
        and not net_premium and not total_premium
        and gemini_available()
    )
    if needs_extract:
        try:
            auto = await loop.run_in_executor(
                _executor_extract,
                lambda: parse_with_gemini(file_bytes, filename=file.filename) or {}
            )
            print(f"[attach-prb] AI extract → net={auto.get('net_premium')} total={auto.get('total_premium')}")
        except Exception as e:
            print(f"[attach-prb] AI extract failed (ignored): {str(e)[:200]}")
            auto = {}

    # ใช้ค่าจาก form ก่อน → fallback ไปค่า AI extract
    final_net   = _to_float(net_premium)   if net_premium   else auto.get("net_premium")
    final_stamp = _to_float(stamp_duty)    if stamp_duty    else auto.get("stamp_duty")
    final_vat   = _to_float(vat)           if vat           else auto.get("vat")
    final_total = _to_float(total_premium) if total_premium else auto.get("total_premium")
    final_cs    = _to_date(coverage_start) if coverage_start else _to_date(auto.get("coverage_start"))
    final_ce    = _to_date(coverage_end)   if coverage_end   else _to_date(auto.get("coverage_end"))

    # auto-generate label ถ้าไม่ได้ใส่ — "พ.ร.บ. ปี {YY}"
    final_label = label.strip() if label.strip() else None
    if not final_label and doc_type == "prb" and final_ce:
        try:
            year_ce = int(final_ce.split("-", 1)[0]) + 543  # ค.ศ. → พ.ศ.
            final_label = f"พ.ร.บ. ปี {year_ce}"
        except Exception:
            pass

    # อัปโหลดไป Storage
    pdf_url = await loop.run_in_executor(
        _executor, lambda: _upload_pdf_to_storage(supabase, file_bytes, file.filename)
    )
    if not pdf_url:
        raise HTTPException(status_code=500, detail="อัปโหลดไฟล์ไป storage ไม่สำเร็จ")

    # auto-rename pdf_filename ตามทะเบียน parent + ประเภท + ปี
    display_filename = _make_display_filename(parent_plate, doc_type, final_ce)

    # บันทึก metadata + เบี้ย
    try:
        payload = {
            "policy_id": policy_id,
            "doc_type": doc_type,
            "label": final_label,
            "note": note.strip() or None,
            "pdf_url": pdf_url,
            "pdf_filename": display_filename,
            "pdf_size": len(file_bytes),
            "net_premium":   final_net,
            "stamp_duty":    final_stamp,
            "vat":           final_vat,
            "total_premium": final_total,
            "coverage_start": final_cs,
            "coverage_end":   final_ce,
        }
        result = supabase.table("policy_attachments").insert(payload).execute()
        return {"success": True, "data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/policies/{policy_id}/attachments/{attachment_id}")
async def update_attachment(policy_id: str, attachment_id: str, data: dict):
    """แก้ไข metadata + เบี้ย — ไม่แตะไฟล์"""
    supabase = get_supabase()
    str_fields   = {"label", "note", "doc_type"}
    float_fields = {"net_premium", "stamp_duty", "vat", "total_premium"}
    date_fields  = {"coverage_start", "coverage_end"}

    allowed = {}
    for k, v in data.items():
        if k in str_fields:
            allowed[k] = (v or "").strip() or None if isinstance(v, str) else v
        elif k in float_fields:
            allowed[k] = _to_float(v)
        elif k in date_fields:
            allowed[k] = _to_date(v)

    if "doc_type" in allowed and allowed["doc_type"] not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail="doc_type ไม่ถูกต้อง")
    try:
        supabase.table("policy_attachments")\
            .update(allowed)\
            .eq("id", attachment_id)\
            .eq("policy_id", policy_id)\
            .execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/policies/{policy_id}/attachments/{attachment_id}")
async def delete_attachment(policy_id: str, attachment_id: str):
    """ลบเอกสารแนบ + ไฟล์ใน storage"""
    supabase = get_supabase()
    try:
        row = supabase.table("policy_attachments")\
            .select("pdf_url")\
            .eq("id", attachment_id)\
            .eq("policy_id", policy_id)\
            .execute()
        if not row.data:
            raise HTTPException(status_code=404, detail="ไม่พบเอกสารแนบ")

        pdf_url = row.data[0].get("pdf_url") or ""

        # ลบจาก storage (รองรับทั้ง R2 และ Supabase legacy)
        if _is_r2_url(pdf_url):
            try:
                r2_pub = os.getenv("R2_PUBLIC_URL", "").rstrip("/")
                file_path = pdf_url.split("?", 1)[0].replace(r2_pub + "/", "", 1)
                supabase.storage.from_(os.getenv("R2_BUCKET", "")).remove([file_path])
            except Exception as e:
                print(f"[delete-attachment] R2 cleanup failed: {e}")
        elif _is_supabase_storage_url(pdf_url):
            print(f"[delete-attachment] legacy Supabase URL ข้าม cleanup")

        supabase.table("policy_attachments")\
            .delete()\
            .eq("id", attachment_id)\
            .eq("policy_id", policy_id)\
            .execute()
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/policies/{policy_id}/attachments/{attachment_id}/pdf")
async def get_attachment_pdf(policy_id: str, attachment_id: str, download: int = 0):
    """โหลดไฟล์ PDF ของเอกสารแนบ — stream ผ่าน backend"""
    supabase = get_supabase()
    try:
        row = supabase.table("policy_attachments")\
            .select("pdf_url, pdf_filename")\
            .eq("id", attachment_id)\
            .eq("policy_id", policy_id)\
            .execute()
        if not row.data:
            raise HTTPException(status_code=404, detail="ไม่พบเอกสารแนบ")

        pdf_url  = row.data[0].get("pdf_url") or ""
        filename = row.data[0].get("pdf_filename") or f"{attachment_id}.pdf"
        if not pdf_url:
            raise HTTPException(status_code=404, detail="เอกสารแนบนี้ไม่มีไฟล์")

        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(pdf_url)
            r.raise_for_status()
            pdf_bytes = r.content

        disp = "attachment" if download else "inline"
        safe_name = quote(filename)
        ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "attachment.pdf"
        headers = {
            "Content-Disposition":
                f"{disp}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{safe_name}"
        }
        return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"Storage error: {e.response.status_code}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
