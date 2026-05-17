"""
routes/invoice.py — สร้างใบแจ้งหนี้ PDF
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from datetime import datetime
from urllib.parse import quote

from services.invoice_generator import build_invoice_pdf

router = APIRouter()


class Party(BaseModel):
    name: str = ""
    address: str = ""
    phone: str = ""
    tax_id: str = ""
    license_plate: str = ""  # buyer only


class Item(BaseModel):
    description: str
    quantity: float = 1
    unit_price: float = 0


class InvoiceRequest(BaseModel):
    invoice_no: str = Field(..., description="เลขที่ใบแจ้งหนี้ เช่น INV-2026/0001")
    invoice_date: str | None = None     # YYYY-MM-DD; default = today
    seller: Party
    buyer: Party
    items: list[Item]
    extra_fees: dict[str, float] = {}   # {"อากร": 10, "บุคคลภายนอก": 200}
    vat_rate: float = 0.07
    promptpay_target: str | None = None  # phone 10 digits or ID 13 digits
    note: str = ""


@router.post("/invoice/generate")
async def generate_invoice(req: InvoiceRequest):
    """สร้าง PDF ใบแจ้งหนี้ — คืนเป็น binary"""
    try:
        # parse date
        if req.invoice_date:
            try:
                d = datetime.strptime(req.invoice_date, "%Y-%m-%d")
            except ValueError:
                raise HTTPException(400, "invoice_date format ผิด ใช้ YYYY-MM-DD")
        else:
            d = datetime.now()

        if not req.items:
            raise HTTPException(400, "ต้องมีอย่างน้อย 1 รายการ")

        pdf_bytes = build_invoice_pdf(
            invoice_no       = req.invoice_no,
            invoice_date     = d,
            seller           = req.seller.model_dump(),
            buyer            = req.buyer.model_dump(),
            items            = [i.model_dump() for i in req.items],
            extra_fees       = req.extra_fees,
            vat_rate         = req.vat_rate,
            promptpay_target = req.promptpay_target,
            note             = req.note,
        )

        filename = f"invoice_{req.invoice_no.replace('/', '_')}.pdf"
        safe_name = quote(filename)
        ascii_fb  = filename.encode("ascii", "ignore").decode("ascii") or "invoice.pdf"
        return Response(
            content=pdf_bytes,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'inline; filename="{ascii_fb}"; filename*=UTF-8\'\'{safe_name}'
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        raise HTTPException(500, f"สร้าง PDF ไม่สำเร็จ: {str(e)}")
