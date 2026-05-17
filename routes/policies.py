from fastapi import APIRouter, Query, HTTPException
from fastapi.responses import Response, RedirectResponse
from supabase import create_client
import os
import base64
import httpx
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
    "agent_code, broker_name, broker_license, notes, manually_edited, "
    "pdf_url, pdf_filename, pdf_size"
)

# คอลัมน์ที่ detail endpoint จะดึง (ไม่รวม pdf_data เช่นกัน — โหลดแยกผ่าน /pdf)
DETAIL_COLUMNS = LIST_COLUMNS


def get_supabase():
    return create_client(
        os.getenv("SUPABASE_URL"),
        os.getenv("SUPABASE_KEY")
    )

def _drive_file_id(pdf_url: str) -> str | None:
    """ดึง file_id จาก Google Drive URL (legacy)"""
    if "drive.google.com/file/d/" in pdf_url:
        return pdf_url.split("/file/d/")[1].split("/")[0]
    return None


def _is_supabase_storage_url(pdf_url: str) -> bool:
    """เช็คว่าเป็น Supabase Storage public URL หรือไม่"""
    return bool(pdf_url) and "/storage/v1/object/public/" in pdf_url

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
    คืนไฟล์ PDF
    - ถ้ามี pdf_url (S3) → redirect ไป presigned URL
    - fallback: pdf_data (base64 ใน DB เก่า)
    """
    supabase = get_supabase()
    try:
        result = supabase.table("insurance_policies")\
                         .select("pdf_data, pdf_filename, pdf_url")\
                         .eq("id", policy_id).execute()
        if not result.data:
            raise HTTPException(status_code=404, detail="ไม่พบข้อมูล")

        row      = result.data[0]
        filename = row.get("pdf_filename") or f"{policy_id}.pdf"
        pdf_url  = row.get("pdf_url") or ""
        pdf_b64  = row.get("pdf_data")

        # ── Supabase Storage: stream bytes ผ่าน backend (กัน CORS + ปลอม URL) ──
        if _is_supabase_storage_url(pdf_url):
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    r = await client.get(pdf_url)
                    r.raise_for_status()
                    pdf_bytes = r.content
                disp = "attachment" if download else "inline"
                safe_name = quote(filename)
                # ใช้ filename*= สำหรับ UTF-8 (RFC 5987) + fallback ASCII กัน latin-1 encoding error
                ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "policy.pdf"
                headers = {"Content-Disposition": f"{disp}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{safe_name}"}
                return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    raise HTTPException(status_code=404, detail="ไฟล์ PDF ถูกลบจาก storage")
                raise HTTPException(status_code=502, detail=f"Storage error: {e.response.status_code}")
            except Exception as e:
                print(f"[pdf] Supabase stream error: {e}")
                raise HTTPException(status_code=502, detail=f"ไม่สามารถโหลด PDF: {str(e)}")

        # ── Google Drive (legacy): stream bytes ตรงๆ ──
        file_id = _drive_file_id(pdf_url)
        if file_id:
            try:
                import io
                from googleapiclient.discovery import build
                from googleapiclient.http import MediaIoBaseDownload
                from google.oauth2.credentials import Credentials
                creds = Credentials(
                    token=None,
                    refresh_token=os.getenv("GOOGLE_REFRESH_TOKEN"),
                    client_id=os.getenv("GOOGLE_CLIENT_ID"),
                    client_secret=os.getenv("GOOGLE_CLIENT_SECRET"),
                    token_uri="https://oauth2.googleapis.com/token",
                )
                service = build("drive", "v3", credentials=creds, cache_discovery=False)
                request = service.files().get_media(fileId=file_id)
                buf = io.BytesIO()
                dl = MediaIoBaseDownload(buf, request)
                done = False
                while not done:
                    _, done = dl.next_chunk()
                pdf_bytes = buf.getvalue()
                disp = "attachment" if download else "inline"
                safe_name = quote(filename)
                # ใช้ filename*= สำหรับ UTF-8 (RFC 5987) + fallback ASCII กัน latin-1 encoding error
                ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "policy.pdf"
                headers = {"Content-Disposition": f"{disp}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{safe_name}"}
                return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)
            except Exception as e:
                print(f"[pdf] Drive stream error: {e}")
                target = f"https://drive.google.com/uc?id={file_id}&export=download" if download else f"https://drive.google.com/file/d/{file_id}/preview"
                return RedirectResponse(url=target, status_code=302)

        # ── Fallback: base64 ใน DB (record เก่า) ──
        if not pdf_b64:
            raise HTTPException(status_code=404, detail="ไม่มีไฟล์ PDF สำหรับรายการนี้")
        try:
            pdf_bytes = base64.b64decode(pdf_b64)
        except Exception:
            raise HTTPException(status_code=500, detail="PDF data เสียหาย")

        disp      = "attachment" if download else "inline"
        safe_name = quote(filename)
        ascii_fallback = filename.encode("ascii", "ignore").decode("ascii") or "policy.pdf"
        headers   = {"Content-Disposition": f"{disp}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{safe_name}"}
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
