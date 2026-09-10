"""Inbox for PDFs that are not issued policies or cannot be matched yet."""
from functools import lru_cache
import asyncio
import json
import os

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from routes.upload import _upload_pdf_to_storage
from services import doc_pairing
from services.supabase_shim import create_client


router = APIRouter()
INBOX_TYPES = {
    doc_pairing.MOTOR_PRB, doc_pairing.RENEWAL_NOTICE, doc_pairing.ENDORSEMENT,
    doc_pairing.CREDIT_NOTE, doc_pairing.INVOICE, doc_pairing.RECEIPT, doc_pairing.UNKNOWN,
}


@lru_cache(maxsize=1)
def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _year(value):
    text = str(value or "")
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


@router.post("/documents/resolve-parent")
def resolve_parent(data: dict):
    """Find one safe parent. Ambiguous evidence returns no match."""
    document_type = data.get("doc_type") or doc_pairing.UNKNOWN
    columns = "id, created_at, policy_number, company_code, insured_name, license_plate, chassis_no, policy_type, coverage_start"
    supabase = get_supabase()

    if document_type != doc_pairing.MOTOR_PRB and data.get("policy_number"):
        query = supabase.table("insurance_policies").select(columns).eq(
            "policy_number", data["policy_number"]
        )
        if data.get("company_code"):
            query = query.eq("company_code", data["company_code"])
        rows = query.order("created_at", desc=True).range(0, 1).execute().data or []
        return {"match": rows[0] if len(rows) == 1 else None,
                "reason": "policy_number_exact" if len(rows) == 1 else "not_unique"}

    if document_type == doc_pairing.MOTOR_PRB:
        query = supabase.table("insurance_policies").select(columns).in_("policy_type", ["M", "STY"])
        chassis = doc_pairing.norm_chassis(data.get("chassis_no"))
        plate = doc_pairing.norm_plate(data.get("license_plate"))
        if chassis:
            query = query.raw_filter(
                "REPLACE(REPLACE(UPPER(chassis_no), ' ', ''), '-', '') = %s", [chassis]
            )
            reason = "chassis_and_year"
        elif plate:
            query = query.raw_filter(
                "REPLACE(REPLACE(license_plate, ' ', ''), '-', '') = %s", [plate]
            )
            reason = "plate_and_year"
        else:
            return {"match": None, "reason": "missing_vehicle_identity"}
        rows = query.order("created_at", desc=True).range(0, 20).execute().data or []
        year = _year(data.get("coverage_start"))
        same_year = [row for row in rows if not year or _year(row.get("coverage_start")) == year]
        return {"match": same_year[0] if len(same_year) == 1 else None,
                "reason": reason if len(same_year) == 1 else "not_unique"}

    return {"match": None, "reason": "no_reference"}


@router.get("/documents/inbox")
def list_inbox(limit: int = 100):
    result = (
        get_supabase().table("document_inbox")
        .select("*")
        .order("created_at", desc=True)
        .range(0, max(1, min(limit, 500)) - 1)
        .execute()
    )
    return {"data": result.data or []}


@router.post("/documents/inbox")
async def save_to_inbox(
    file: UploadFile = File(...),
    document_type: str = Form("unknown"),
    reference_policy_number: str = Form(""),
    insured_name: str = Form(""),
    license_plate: str = Form(""),
    coverage_start: str = Form(""),
    coverage_end: str = Form(""),
    extracted_json: str = Form("{}"),
):
    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF เท่านั้น")
    if document_type not in INBOX_TYPES:
        raise HTTPException(status_code=400, detail="ประเภทเอกสารไม่ใช่เอกสารรอตรวจ")
    blob = await file.read()
    if not blob:
        raise HTTPException(status_code=400, detail="ไฟล์ PDF ว่าง")
    try:
        extracted = json.loads(extracted_json or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="ข้อมูลที่อ่านจากเอกสารไม่ถูกต้อง")

    url = await asyncio.to_thread(_upload_pdf_to_storage, get_supabase(), blob, file.filename)
    if not url:
        raise HTTPException(status_code=500, detail="อัปโหลดไฟล์ไม่สำเร็จ")
    payload = {
        "document_type": (document_type or "unknown").strip(),
        "status": "needs_review",
        "reference_policy_number": reference_policy_number.strip() or None,
        "insured_name": insured_name.strip() or None,
        "license_plate": license_plate.strip() or None,
        "coverage_start": coverage_start or None,
        "coverage_end": coverage_end or None,
        "extracted_data": extracted,
        "pdf_url": url,
        "pdf_filename": file.filename,
        "pdf_size": len(blob),
        "original_filename": file.filename,
    }
    result = get_supabase().table("document_inbox").insert(payload).execute()
    return {"success": True, "data": result.data[0] if result.data else None}
