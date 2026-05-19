"""
Attachments routes — เอกสารแนบหลายไฟล์ต่อ 1 กรมธรรม์
ประเภท: prb (พ.ร.บ.) | endorsement (สลักหลัง) | other (อื่นๆ)
หมายเหตุ: main ใช้คอลัมน์ pdf_url/pdf_filename ใน insurance_policies (ของเดิม)
"""
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from fastapi.responses import Response
from supabase import create_client
import os, httpx, asyncio
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import quote

from routes.upload import _upload_pdf_to_storage, BUCKET_NAME

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=4)

ALLOWED_TYPES = {"prb", "endorsement", "other"}


def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _is_supabase_storage_url(url: str) -> bool:
    return bool(url) and "/storage/v1/object/public/" in url


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


@router.post("/policies/{policy_id}/attachments")
async def upload_attachment(
    policy_id: str,
    doc_type: str = Form(...),
    label: str = Form(""),
    note: str = Form(""),
    file: UploadFile = File(...),
):
    """อัปโหลดเอกสารแนบใหม่ (พ.ร.บ. / สลักหลัง / อื่นๆ)"""
    if doc_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=400, detail=f"doc_type ต้องเป็น {ALLOWED_TYPES}")
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")

    file_bytes = await file.read()
    supabase = get_supabase()

    # ตรวจว่ามี policy อยู่จริง
    policy = supabase.table("insurance_policies").select("id").eq("id", policy_id).execute()
    if not policy.data:
        raise HTTPException(status_code=404, detail="ไม่พบกรมธรรม์")

    # อัปโหลดไป Supabase Storage
    loop = asyncio.get_event_loop()
    pdf_url = await loop.run_in_executor(
        _executor, lambda: _upload_pdf_to_storage(supabase, file_bytes, file.filename)
    )
    if not pdf_url:
        raise HTTPException(status_code=500, detail="อัปโหลดไฟล์ไป storage ไม่สำเร็จ")

    # บันทึก metadata
    try:
        result = supabase.table("policy_attachments").insert({
            "policy_id": policy_id,
            "doc_type": doc_type,
            "label": label.strip() or None,
            "note": note.strip() or None,
            "pdf_url": pdf_url,
            "pdf_filename": file.filename,
            "pdf_size": len(file_bytes),
        }).execute()
        return {"success": True, "data": result.data[0] if result.data else None}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.put("/policies/{policy_id}/attachments/{attachment_id}")
async def update_attachment(policy_id: str, attachment_id: str, data: dict):
    """แก้ไข metadata (label, note) — ไม่แตะไฟล์"""
    supabase = get_supabase()
    allowed = {k: v for k, v in data.items() if k in ("label", "note", "doc_type")}
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

        # ลบจาก storage (ถ้าเป็น Supabase Storage)
        if _is_supabase_storage_url(pdf_url):
            try:
                marker = "/storage/v1/object/public/"
                idx = pdf_url.find(marker)
                if idx != -1:
                    rest = pdf_url[idx + len(marker):]
                    parts = rest.split("/", 1)
                    if len(parts) == 2:
                        bucket, file_path = parts
                        supabase.storage.from_(bucket).remove([file_path])
            except Exception as e:
                print(f"[delete-attachment] storage cleanup failed: {e}")

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
