from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response
from supabase import create_client
import os
import base64
from urllib.parse import quote

router = APIRouter()

# คอลัมน์ที่ list endpoint จะดึง (ไม่รวม pdf_data เพราะใหญ่มาก)
LIST_COLUMNS = (
    "id, created_at, "
    "app_number, policy_number, company_code, policy_type, new_renew, "
    "insured_name, phone, insured_address, "
    "license_plate, license_province, chassis_no, car_make, car_model, car_year, "
    "sum_insured, "
    "coverage_start, coverage_end, date_notify, date_cancel, date_policy_receive, "
    "net_premium, stamp_duty, vat, total_premium, "
    "third_party_per_person, third_party_per_accident, own_damage, "
    "agent_code, broker_name, broker_license, manually_edited, "
    "pdf_url, pdf_filename, pdf_size"
)

# คอลัมน์ที่ detail endpoint จะดึง (ไม่รวม pdf_data เช่นกัน — โหลดแยกผ่าน /pdf)
DETAIL_COLUMNS = LIST_COLUMNS


def get_supabase():
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_KEY")
    )

@router.get("/policies")
async def get_policies(
    page: int = 1,
    limit: int = 20,
    search: str = Query(None)
):
    supabase = get_supabase()
    offset = (page - 1) * limit

    try:
        query = supabase.table("insurance_policies").select(LIST_COLUMNS, count="exact")

        if search:
            query = query.or_(
                f"policy_number.ilike.%{search}%,"
                f"insured_name.ilike.%{search}%,"
                f"license_plate.ilike.%{search}%"
            )

        result = query.order("created_at", desc=True)\
                      .range(offset, offset + limit - 1)\
                      .execute()

        return {
            "data": result.data,
            "page": page,
            "total": result.count or 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.get("/policies/{policy_id}")
async def get_policy(policy_id: str):
    supabase = get_supabase()
    try:
        result = supabase.table("insurance_policies")\
                         .select(DETAIL_COLUMNS).eq("id", policy_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูล")
        return result.data[0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.get("/policies/{policy_id}/pdf")
async def get_policy_pdf(policy_id: str, download: int = 0):
    """
    คืนไฟล์ PDF ที่เก็บใน DB (pdf_data เป็น base64 text)
    - download=0 (default) → inline (เปิดใน browser)
    - download=1           → attachment (บังคับดาวน์โหลด)
    """
    supabase = get_supabase()
    try:
        result = supabase.table("insurance_policies")\
                         .select("pdf_data, pdf_filename")\
                         .eq("id", policy_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูล")

        row      = result.data[0]
        pdf_b64  = row.get("pdf_data")
        filename = row.get("pdf_filename") or f"{policy_id}.pdf"

        if not pdf_b64:
            raise HTTPException(status_code=404, detail="ไม่มีไฟล์ PDF ในฐานข้อมูลสำหรับรายการนี้")

        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception:
            raise HTTPException(status_code=500, detail="PDF data เสียหาย (decode ไม่ได้)")

        disp = "attachment" if download else "inline"
        # encode filename ให้ปลอดภัยทั้งภาษาไทย/อังกฤษ
        safe_name = quote(filename)
        headers = {
            "Content-Disposition": f"{disp}; filename=\"{filename}\"; filename*=UTF-8''{safe_name}"
        }
        return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.put("/policies/{policy_id}")
async def update_policy(policy_id: str, data: dict):
    supabase = get_supabase()
    try:
        data["manually_edited"] = True
        supabase.table("insurance_policies")\
                .update(data).eq("id", policy_id).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")


@router.delete("/policies/{policy_id}")
async def delete_policy(policy_id: str):
    supabase = get_supabase()
    try:
        supabase.table("insurance_policies")\
                .delete().eq("id", policy_id).execute()
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Supabase error: {str(e)}")
