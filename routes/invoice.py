"""
routes/invoice.py — สร้างใบแจ้งหนี้ PDF
"""
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from datetime import datetime
from urllib.parse import quote

from services.invoice_generator import (
    build_invoice_pdf,
    build_debit_note_template1,
    build_debit_note_template2,
)

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
    extra_fees: dict[str, float] = {}   # {"อากรแสตมป์": 10, "บุคคลภายนอก": 200}
    vat_rate: float = 0.07
    promptpay_target: str | None = None  # phone 10 digits or ID 13 digits
    note: str = ""

    # ── template selection ──
    template: str = "default"           # "default" | "tm1" | "tm2"

    # ── extra fields ใช้กับ template tm1/tm2 ──
    coverage_start: str | None = None   # "15/05/2026" (display only)
    coverage_end:   str | None = None
    policy_no:      str | None = None   # กรมธรรม์เลขที่
    endorsement_no: str | None = None   # สลักหลังเลขที่
    branch:         str | None = None   # สาขา
    insurance_type: str | None = None   # "PERSONAL PROPERTY INSURANCE"
    original_policy_no: str | None = None
    series:         str | None = None   # "UPPOR0"

    # ── tm2 specific ──
    registration_no:   str | None = None  # "0107563000011" — TM registration #
    sequence_no:       str | None = None  # "0001"
    discount:          float = 0          # ส่วนลด
    insured_occupation: str | None = None # อาชีพ Occupation
    effective_date:    str | None = None  # "30/04/2026" — วันเริ่มประกัน
    expiry_date:       str | None = None  # "30/04/2027"
    effective_time:    str | None = None  # "16.30 น."
    vehicle_code:      str | None = None  # "110"
    car_make:          str | None = None  # "TOYOTA"
    car_model:         str | None = None  # "COROLLA"
    chassis_no:        str | None = None  # "MB2KLAAG900043133"
    seats:             str | None = None  # "7/1800/-"
    insurance_subtype: str | None = None  # "prb" | "comp" | "3rd" | "3rd_only" | "other"
    sum_insured:       float = 0          # ทุนประกัน
    accessories:       float = 0          # อุปกรณ์ตกแต่ง
    use_of_vehicle:    str | None = None  # "ใช้ส่วนบุคคล ไม่ใช้รับจ้างหรือให้เช่า"
    broker_name:       str | None = None
    broker_code:       str | None = None  # "B200291"
    agreement_date:    str | None = None  # "20/04/2026"
    remark:            str | None = None  # หมายเหตุ Remark


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

        # ── Template Tokio Marine 1 ─────────────────────────────────
        if req.template == "tm1":
            net   = sum(i.quantity * i.unit_price for i in req.items)
            stamp = req.extra_fees.get("อากรแสตมป์", 0) or req.extra_fees.get("อากร", 0) or 0
            # vat = (net + extra) * vat_rate
            extra_for_vat = sum(v for k, v in req.extra_fees.items()
                                if k not in ("อากรแสตมป์", "อากร"))
            vat = (net + extra_for_vat + stamp) * req.vat_rate

            pdf_bytes = build_debit_note_template1(
                invoice_no        = req.invoice_no,
                invoice_date      = d,
                buyer             = req.buyer.model_dump(),
                net_premium       = net,
                stamp_duty        = stamp,
                vat               = vat,
                coverage_start    = req.coverage_start,
                coverage_end      = req.coverage_end,
                policy_no         = req.policy_no,
                endorsement_no    = req.endorsement_no,
                branch            = req.branch,
                insurance_type    = req.insurance_type,
                original_policy_no = req.original_policy_no,
                series            = req.series,
                note              = req.note,
                promptpay_target  = req.promptpay_target,
            )
        elif req.template == "tm2":
            net   = sum(i.quantity * i.unit_price for i in req.items)
            stamp = req.extra_fees.get("อากรแสตมป์", 0) or req.extra_fees.get("อากร", 0) or 0
            extra_for_vat = sum(v for k, v in req.extra_fees.items()
                                if k not in ("อากรแสตมป์", "อากร"))
            vat = (net + extra_for_vat + stamp - (req.discount or 0)) * req.vat_rate

            pdf_bytes = build_debit_note_template2(
                invoice_no        = req.invoice_no,
                invoice_date      = d,
                buyer             = req.buyer.model_dump(),
                net_premium       = net,
                stamp_duty        = stamp,
                vat               = vat,
                discount          = req.discount or 0,
                policy_no         = req.policy_no,
                registration_no   = req.registration_no,
                sequence_no       = req.sequence_no,
                branch            = req.branch,
                insured_occupation = req.insured_occupation,
                effective_date    = req.effective_date,
                expiry_date       = req.expiry_date,
                effective_time    = req.effective_time,
                vehicle_code      = req.vehicle_code,
                car_make          = req.car_make,
                car_model         = req.car_model,
                chassis_no        = req.chassis_no,
                seats             = req.seats,
                insurance_subtype = req.insurance_subtype,
                sum_insured       = req.sum_insured or 0,
                accessories       = req.accessories or 0,
                use_of_vehicle    = req.use_of_vehicle,
                broker_name       = req.broker_name,
                broker_code       = req.broker_code,
                agreement_date    = req.agreement_date,
                remark            = req.remark,
                promptpay_target  = req.promptpay_target,
            )
        else:
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
