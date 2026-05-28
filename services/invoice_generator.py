"""
invoice_generator.py
─────────────────────────────────────────────────────────────────────
สร้างใบแจ้งหนี้ PDF — Minimalist design
"""
import io, os
from pathlib import Path
from datetime import datetime

import qrcode
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader


# ── Minimalist palette ─────────────────────────────────────────────
INK    = colors.HexColor("#111827")  # near-black
TEXT   = colors.HexColor("#374151")  # dark gray
MUTED  = colors.HexColor("#9ca3af")  # light gray label
LINE   = colors.HexColor("#e5e7eb")  # thin divider
ACCENT = colors.HexColor("#000000")  # accent (use black for ultra-minimal)

_FONT_REGISTERED = False
_FONT_NORMAL = "Tahoma"
_FONT_MEDIUM = "Tahoma"
_FONT_BOLD   = "Tahoma-Bold"

ASSETS_DIR = Path(__file__).parent / "assets"
LOGO_PATH  = ASSETS_DIR / "logo.png"


def _register_thai_fonts():
    """Register Sarabun — Light/Regular/Medium/SemiBold
    ใช้ Medium แทน Bold เพื่อความเบาตา"""
    global _FONT_REGISTERED, _FONT_NORMAL, _FONT_MEDIUM, _FONT_BOLD
    if _FONT_REGISTERED:
        return

    sarabun_reg    = ASSETS_DIR / "Sarabun-Regular.ttf"
    sarabun_med    = ASSETS_DIR / "Sarabun-Medium.ttf"
    sarabun_semi   = ASSETS_DIR / "Sarabun-SemiBold.ttf"
    sarabun_bold   = ASSETS_DIR / "Sarabun-Bold.ttf"

    if sarabun_reg.exists():
        try:
            pdfmetrics.registerFont(TTFont("Sarabun", str(sarabun_reg)))
            if sarabun_med.exists():
                pdfmetrics.registerFont(TTFont("Sarabun-Medium", str(sarabun_med)))
            if sarabun_semi.exists():
                pdfmetrics.registerFont(TTFont("Sarabun-SemiBold", str(sarabun_semi)))
            if sarabun_bold.exists():
                pdfmetrics.registerFont(TTFont("Sarabun-Bold", str(sarabun_bold)))
            _FONT_NORMAL = "Sarabun"
            _FONT_MEDIUM = "Sarabun-Medium" if sarabun_med.exists() else "Sarabun"
            # ใช้ SemiBold แทน Bold — เบาตา ไม่หนาไป
            _FONT_BOLD   = "Sarabun-SemiBold" if sarabun_semi.exists() else "Sarabun-Bold"
            _FONT_REGISTERED = True
            print(f"[invoice] Using Sarabun (medium for emphasis)")
            return
        except Exception as e:
            print(f"[invoice] Sarabun failed: {e}")

    # Fallback
    for normal_path, bold_path, name in [
        (r"C:\Windows\Fonts\tahoma.ttf", r"C:\Windows\Fonts\tahomabd.ttf", "Tahoma"),
        (r"C:\Windows\Fonts\arial.ttf",  r"C:\Windows\Fonts\arialbd.ttf",  "Arial"),
    ]:
        if os.path.exists(normal_path):
            pdfmetrics.registerFont(TTFont(name, normal_path))
            if os.path.exists(bold_path):
                pdfmetrics.registerFont(TTFont(f"{name}-Bold", bold_path))
            _FONT_NORMAL = name
            _FONT_MEDIUM = name
            _FONT_BOLD   = f"{name}-Bold" if os.path.exists(bold_path) else name
            _FONT_REGISTERED = True
            return
    raise RuntimeError("No Thai font found")


# ── PromptPay QR ───────────────────────────────────────────────────
def _crc16_ccitt(data: str) -> str:
    crc = 0xFFFF
    for b in data.encode("ascii"):
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if (crc & 0x8000) else (crc << 1)
            crc &= 0xFFFF
    return f"{crc:04X}"


def _tlv(tag: str, value: str) -> str:
    return f"{tag}{len(value):02d}{value}"


def generate_promptpay_payload(target: str, amount: float | None = None) -> str:
    pid = target.replace("-", "").replace(" ", "")
    if len(pid) == 10 and pid.startswith("0"):
        merchant_id = _tlv("01", "0066" + pid[1:])
    elif len(pid) == 13:
        merchant_id = _tlv("02", pid)
    elif len(pid) == 15:
        merchant_id = _tlv("03", pid)
    else:
        raise ValueError(f"PromptPay target ต้อง 10/13/15 หลัก ได้ {len(pid)}")
    info = _tlv("00", "A000000677010111") + merchant_id
    payload = (_tlv("00", "01") + _tlv("01", "12" if amount else "11")
               + _tlv("29", info) + _tlv("53", "764")
               + (_tlv("54", f"{amount:.2f}") if amount else "")
               + _tlv("58", "TH"))
    payload += "6304"
    return payload + _crc16_ccitt(payload)


def generate_qr_image(payload: str) -> bytes:
    qr = qrcode.QRCode(version=None,
                       error_correction=qrcode.constants.ERROR_CORRECT_H,
                       box_size=10, border=4)
    qr.add_data(payload); qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO(); img.save(buf, "PNG")
    return buf.getvalue()


# ── Thai utilities ─────────────────────────────────────────────────
_TH_DIGITS = ["", "หนึ่ง", "สอง", "สาม", "สี่", "ห้า", "หก", "เจ็ด", "แปด", "เก้า"]
_TH_UNITS  = ["", "สิบ", "ร้อย", "พัน", "หมื่น", "แสน", "ล้าน"]


def _thai_int(num: int) -> str:
    if num == 0: return "ศูนย์"
    if num < 0:  return "ลบ" + _thai_int(-num)
    if num >= 1_000_000: return _thai_int(num // 1_000_000) + "ล้าน" + _thai_int(num % 1_000_000)
    s = str(num); result = ""
    for i, d in enumerate(s):
        digit = int(d); pos = len(s) - 1 - i
        if digit == 0: continue
        if pos == 1 and digit == 1: result += "สิบ"
        elif pos == 1 and digit == 2: result += "ยี่สิบ"
        elif pos == 0 and digit == 1 and len(s) > 1: result += "เอ็ด"
        else: result += _TH_DIGITS[digit] + _TH_UNITS[pos]
    return result


def baht_in_words(amount: float) -> str:
    b = int(amount); s = int(round((amount - b) * 100))
    return _thai_int(b) + "บาทถ้วน" if s == 0 else f"{_thai_int(b)}บาท{_thai_int(s)}สตางค์"


def fmt_thai_date(d: datetime) -> str:
    m = ["ม.ค.", "ก.พ.", "มี.ค.", "เม.ย.", "พ.ค.", "มิ.ย.",
         "ก.ค.", "ส.ค.", "ก.ย.", "ต.ค.", "พ.ย.", "ธ.ค."]
    return f"{d.day} {m[d.month-1]} {d.year + 543}"


def baht_fmt(n: float) -> str:
    return f"{n:,.2f}"


# ── Tokio Marine constant header info (Template 1 / 2) ────────────────
TM_NAME_EN     = "Tokio Marine Safety Insurance (Thailand) PCL."
TM_NAME_TH     = "บมจ. คุ้มภัยโตเกียวมารีนประกันภัย (ประเทศไทย)"
TM_ADDR_EN     = ["S&A Building, 2nd - 6th floors, No. 302, Silom Road,",
                  "Khwaeng Suriyawong, Khet Bangrak, Bangkok 10500",
                  "Tel. 0-2257-8000  Fax. 0-2253-3701, 0-2253-4222",
                  "Claims Service Tel. 0-2257-8080"]
TM_ADDR_TH     = ["สำนักงานใหญ่ ชั้น 2-6 เลขที่ 302 ถนนสีลม",
                  "แขวงสุริยวงศ์ เขตบางรัก กรุงเทพมหานคร 10500"]
TM_TAX_ID      = "0107563000011"
TM_CHEQUE_TH   = "บริษัท คุ้มภัยโตเกียวมารีนประกันภัย (ประเทศไทย) จำกัด (มหาชน)"
TM_CHEQUE_EN   = "Tokio Marine Safety Insurance (Thailand) Public Company Limited"


def _tm_header(c, W, H, MX):
    """วาด Tokio Marine header (โลโก้ + ที่อยู่ EN/TH + TAX ID) ที่บนสุดของหน้า
    คืน y position หลัง header (สำหรับให้ caller วาดต่อ)"""
    y = H - 18 * mm

    # โลโก้ซ้าย (ถ้ามี)
    if LOGO_PATH.exists():
        try:
            c.drawImage(ImageReader(str(LOGO_PATH)),
                        MX, y - 13 * mm, width=20 * mm, height=20 * mm,
                        mask='auto', preserveAspectRatio=True)
        except Exception as e:
            print(f"[invoice] logo error: {e}")

    # ชื่อ + ที่อยู่ EN (กลาง-ซ้าย)
    info_x = MX + 24 * mm
    c.setFillColor(INK)
    c.setFont(_FONT_BOLD, 10)
    c.drawString(info_x, y, TM_NAME_EN)
    c.setFillColor(TEXT)
    c.setFont(_FONT_NORMAL, 7.5)
    ty = y - 3.5 * mm
    for line in TM_ADDR_EN:
        c.drawString(info_x, ty, line)
        ty -= 3 * mm

    # ชื่อ + ที่อยู่ TH (ขวา) + TAX ID
    rx = W - MX
    c.setFillColor(INK)
    c.setFont(_FONT_BOLD, 9.5)
    c.drawRightString(rx, y, TM_NAME_TH)
    c.setFillColor(TEXT)
    c.setFont(_FONT_NORMAL, 7.5)
    ty = y - 3.5 * mm
    for line in TM_ADDR_TH:
        c.drawRightString(rx, ty, line)
        ty -= 3 * mm
    c.setFont(_FONT_NORMAL, 7.5)
    c.drawRightString(rx, ty - 1 * mm, f"เลขประจำตัวผู้เสียภาษี / ทะเบียนเลขที่: {TM_TAX_ID}")

    return y - 22 * mm


# ── Template 1: Tokio Marine DEBIT NOTE (simple) ──────────────────────
def build_debit_note_template1(
    *,
    invoice_no: str,
    invoice_date: datetime,
    buyer: dict,
    net_premium: float,
    stamp_duty: float = 0,
    vat: float = 0,
    coverage_start: str | None = None,
    coverage_end: str | None = None,
    policy_no: str | None = None,
    endorsement_no: str | None = None,
    branch: str | None = None,
    insurance_type: str | None = None,
    original_policy_no: str | None = None,
    note: str = "",
    series: str | None = None,
    promptpay_target: str | None = None,
) -> bytes:
    """สร้าง DEBIT NOTE แบบ Tokio Marine (template 1 — แบบสั้น)"""
    _register_thai_fonts()

    total = net_premium + stamp_duty + vat
    wht_1pct = round(net_premium * 0.01, 2)  # หัก ณ ที่จ่าย 1% (กรณีนิติบุคคล)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    MX = 18 * mm

    # ── Header (Tokio Marine) ────────────────────────────────────────
    y = _tm_header(c, W, H, MX)

    # ── Series + coverage line + "เอกสารออกเป็นชุด" box ───────────────
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawString(MX, y, series or "UPPOR0")
    c.setFont(_FONT_NORMAL, 9)
    c.setFillColor(TEXT)
    if coverage_start or coverage_end:
        cs = coverage_start or "—"; ce = coverage_end or "—"
        c.drawString(MX + 22 * mm, y, f"เริ่มวันที่: {cs}  สิ้นสุด: {ce}")

    # มุมขวา: (เอกสารออกเป็นชุด / เอกสารสำหรับ (ลูกค้า) / (CUSTOMER))
    rx = W - MX
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawRightString(rx, y, "(เอกสารออกเป็นชุด)")
    c.drawRightString(rx, y - 4 * mm, "เอกสารสำหรับ")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
    c.drawRightString(rx, y - 8 * mm, "(ลูกค้า)")
    c.setFont(_FONT_NORMAL, 8)
    c.setFillColor(MUTED)
    c.drawRightString(rx, y - 12 * mm, "(CUSTOMER)")

    y -= 22 * mm

    # ── Title centered ───────────────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 18)
    c.drawCentredString(W / 2, y, "ใบแจ้งหนี้")
    c.setFont(_FONT_NORMAL, 11)
    c.setFillColor(TEXT)
    c.drawCentredString(W / 2, y - 6 * mm, "DEBIT NOTE")

    # Right of title: เลขที่ + วันที่
    rx2 = W - MX
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(rx2 - 56 * mm, y, "เลขที่")
    c.drawString(rx2 - 56 * mm, y - 4 * mm, "Receipt No.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawRightString(rx2, y - 1 * mm, invoice_no)

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(rx2 - 56 * mm, y - 9 * mm, "วันที่")
    c.drawString(rx2 - 56 * mm, y - 13 * mm, "Date")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawRightString(rx2, y - 10 * mm, invoice_date.strftime("%d/%m/%Y"))

    y -= 18 * mm

    # ── Main table ───────────────────────────────────────────────────
    # ฝั่งซ้าย: ได้รับเงินจาก + ที่อยู่ | ฝั่งขวา: TAX ID + สาขา + premium box
    left_w  = 110 * mm
    right_x = MX + left_w
    right_w = W - MX - right_x

    c.setStrokeColor(LINE); c.setLineWidth(0.6)
    # outer rect
    box_top = y; box_bottom = y - 80 * mm
    c.rect(MX, box_bottom, W - 2 * MX, box_top - box_bottom, stroke=1, fill=0)

    # vertical divider
    c.line(right_x, box_top, right_x, box_bottom)

    # ── Left: ได้รับเงินจาก ────────────────────────────────────
    ly = box_top - 5 * mm
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawString(MX + 3 * mm, ly, "ได้รับเงินจาก / Received From")
    c.setFillColor(INK); c.setFont(_FONT_MEDIUM, 11)
    c.drawString(MX + 3 * mm, ly - 6 * mm, (buyer.get("name") or "—")[:60])

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawString(MX + 3 * mm, ly - 14 * mm, "ที่อยู่ / Address")
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9.5)
    addr_lines = _wrap_text(buyer.get("address") or "", 50)
    ay = ly - 19 * mm
    for line in addr_lines[:4]:
        c.drawString(MX + 3 * mm, ay, line)
        ay -= 4 * mm

    # Policy + endorsement (bottom of left column)
    pol_y = box_bottom + 8 * mm
    c.setStrokeColor(LINE); c.setLineWidth(0.3)
    c.line(MX + 3 * mm, pol_y + 5 * mm, right_x - 3 * mm, pol_y + 5 * mm)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX + 3 * mm, pol_y, "กรมธรรม์เลขที่ / Policy No.")
    c.drawString(MX + 60 * mm, pol_y, "สลักหลังเลขที่ / Endt. No.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawString(MX + 3 * mm, pol_y - 5 * mm, policy_no or "—")
    c.drawString(MX + 60 * mm, pol_y - 5 * mm, endorsement_no or "—")

    # ── Right: TAX ID + Branch + Premium ──────────────────────
    ry = box_top - 5 * mm
    rcol_x = right_x + 3 * mm
    rcol_right = W - MX - 3 * mm

    # Row 1: TAX ID
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawString(rcol_x, ry, "เลขประจำตัวผู้เสียภาษี / TAX ID.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 11)
    c.drawRightString(rcol_right, ry - 5 * mm, buyer.get("tax_id") or "—")

    c.setStrokeColor(LINE); c.setLineWidth(0.3)
    c.line(right_x, ry - 9 * mm, W - MX, ry - 9 * mm)

    # Row 2: Branch
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawString(rcol_x, ry - 13 * mm, "สาขา / Branch")
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 10)
    c.drawRightString(rcol_right, ry - 17 * mm, branch or "—")

    c.line(right_x, ry - 21 * mm, W - MX, ry - 21 * mm)

    # Row 3-6: Premium breakdown
    rows = [
        ("เบี้ยประกันภัย",  "Premium",    net_premium),
        ("อากรแสตมป์",       "Stamp Duty", stamp_duty),
        ("ภาษี",              "Tax",        vat),
    ]
    row_y = ry - 27 * mm
    for th, en, val in rows:
        c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9.5)
        c.drawString(rcol_x, row_y, th)
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
        c.drawString(rcol_x, row_y - 3.5 * mm, en)
        c.setFillColor(INK); c.setFont(_FONT_BOLD, 11)
        c.drawRightString(rcol_right, row_y - 1.5 * mm, baht_fmt(val))
        row_y -= 9 * mm

    # Total
    c.setStrokeColor(INK); c.setLineWidth(0.6)
    c.line(right_x + 2 * mm, row_y + 4 * mm, W - MX - 2 * mm, row_y + 4 * mm)
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 11)
    c.drawString(rcol_x, row_y, "รวมเป็นเงิน")
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rcol_x, row_y - 3.5 * mm, "Total")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 13)
    c.drawRightString(rcol_right, row_y - 1.5 * mm, baht_fmt(total))

    y = box_bottom - 6 * mm

    # ── Footer info ──────────────────────────────────────────────────
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX, y, "กรณีผู้เอาประกันภัยเป็นนิติบุคคล หักภาษี ณ ที่จ่าย 1%")
    c.setFont(_FONT_BOLD, 9.5)
    c.drawString(MX + 80 * mm, y, baht_fmt(wht_1pct))
    y -= 5 * mm

    if insurance_type:
        c.setFont(_FONT_NORMAL, 9)
        line = f"Type: {insurance_type}"
        if original_policy_no:
            line += f"  (กรมธรรม์เดิมเลขที่: {original_policy_no})"
        c.drawString(MX, y, line)
        y -= 5 * mm

    y -= 2 * mm
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9.5)
    c.drawString(MX, y, f'โปรดจ่ายเป็นเช็คขีดคร่อมในนาม  "{TM_CHEQUE_TH}"')
    y -= 4.5 * mm
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX, y, f'Please pay by crossed cheque to  "{TM_CHEQUE_EN}"')

    # ── QR PromptPay (ถ้ามี) ─────────────────────────────────────────
    if promptpay_target:
        try:
            payload = generate_promptpay_payload(promptpay_target, total)
            qr_png = generate_qr_image(payload)
            qr_size = 35 * mm

            y -= 6 * mm
            c.setStrokeColor(LINE); c.setLineWidth(0.3)
            c.line(MX, y + 4 * mm, W - MX, y + 4 * mm)

            # QR ซ้าย
            c.drawImage(ImageReader(io.BytesIO(qr_png)),
                        MX, y - qr_size, width=qr_size, height=qr_size)
            # ข้อความขวา QR
            tx = MX + qr_size + 6 * mm
            c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
            c.drawString(tx, y - 3 * mm, "ชำระเงินผ่าน PromptPay")
            c.setFillColor(INK); c.setFont(_FONT_BOLD, 12)
            c.drawString(tx, y - 10 * mm, f"พร้อมเพย์ {promptpay_target}")
            c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9.5)
            c.drawString(tx, y - 16 * mm, f"ยอด {baht_fmt(total)} บาท")
            c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
            c.drawString(tx, y - 21 * mm, "สแกน QR ผ่านแอปธนาคารเพื่อชำระเงิน")
            y -= qr_size + 2 * mm

    # ── Note (if any) ────────────────────────────────────────────────
        except Exception as e:
            print(f"[invoice tm1] QR error: {e}")

    if note:
        y -= 6 * mm
        c.setStrokeColor(LINE); c.setLineWidth(0.3)
        c.line(MX, y + 4 * mm, W - MX, y + 4 * mm)
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
        c.drawString(MX, y, "หมายเหตุ:")
        c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
        for i, line in enumerate(_wrap_text(note, 100)[:3]):
            c.drawString(MX + 18 * mm, y - i * 4.5 * mm, line)

    # ── Bottom-corner refs (form code) ───────────────────────────────
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX, 10 * mm, "210919_REC1")
    c.drawString(MX, 6 * mm, "106-015-1-69")
    c.drawRightString(W - MX, 6 * mm, invoice_no)

    c.showPage()
    c.save()
    return buf.getvalue()


# ── Template 2: Tokio Marine DEBIT NOTE COPY (detailed with vehicle) ──
def build_debit_note_template2(
    *,
    invoice_no: str,
    invoice_date: datetime,
    buyer: dict,
    net_premium: float,
    stamp_duty: float = 0,
    vat: float = 0,
    discount: float = 0,
    policy_no: str | None = None,
    registration_no: str | None = None,
    sequence_no: str | None = None,
    branch: str | None = None,
    insured_occupation: str | None = None,
    effective_date: str | None = None,
    expiry_date: str | None = None,
    effective_time: str | None = None,
    vehicle_code: str | None = None,
    car_make: str | None = None,
    car_model: str | None = None,
    chassis_no: str | None = None,
    seats: str | None = None,
    insurance_subtype: str | None = None,  # "prb" | "comp" | "3rd" | "3rd_only" | "other"
    sum_insured: float = 0,
    accessories: float = 0,
    use_of_vehicle: str | None = None,
    broker_name: str | None = None,
    broker_code: str | None = None,
    agreement_date: str | None = None,
    remark: str | None = None,
    promptpay_target: str | None = None,
) -> bytes:
    """สร้าง DEBIT NOTE COPY แบบ Tokio Marine (template 2 — แบบละเอียดรวมข้อมูลรถ)"""
    _register_thai_fonts()

    sub_after_discount = net_premium - discount
    total_after_stamp = sub_after_discount + stamp_duty
    grand_total = total_after_stamp + vat
    wht_1pct = round(net_premium * 0.01, 2)
    commission_rate = 18.0
    commission = round(net_premium * commission_rate / 100, 2)

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    MX = 14 * mm

    # ── Header (Tokio Marine) ────────────────────────────────────────
    y = _tm_header(c, W, H, MX)

    # ── Top-left: TAX NO / Registration No / Policy Number ───────────
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX, y, "เลขประจำตัวผู้เสียภาษีอากร  TAX ID. NO.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
    c.drawString(MX + 64 * mm, y, TM_TAX_ID)

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX, y - 5 * mm, "ทะเบียนเลขที่")
    c.drawString(MX + 22 * mm, y - 5 * mm, "Registration No.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
    c.drawString(MX + 50 * mm, y - 5 * mm, registration_no or TM_TAX_ID)

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX, y - 10 * mm, "กรมธรรม์เลขที่/ใบสลักหลังเลขที่")
    c.drawString(MX + 42 * mm, y - 10 * mm, "Policy Number")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
    c.drawString(MX + 50 * mm, y - 15 * mm, policy_no or "—")

    # Top-right: branch + เลขที่ + วันที่
    rx = W - MX
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rx - 70 * mm, y, "สาขา")
    c.drawString(rx - 60 * mm, y, "Branch")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9.5)
    c.drawString(rx - 40 * mm, y, branch or "สำนักงานใหญ่")

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rx - 70 * mm, y - 5 * mm, "เลขที่")
    c.drawString(rx - 65 * mm, y - 5 * mm, "No.")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9.5)
    c.drawString(rx - 40 * mm, y - 5 * mm, invoice_no)

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rx - 70 * mm, y - 10 * mm, "วันที่ Date")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 9.5)
    c.drawString(rx - 40 * mm, y - 10 * mm, invoice_date.strftime("%d/%m/%Y"))

    y -= 20 * mm

    # ── Title centered ───────────────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 15)
    c.drawCentredString(W / 2, y, "สำเนาใบแจ้งหนี้")
    c.setFont(_FONT_NORMAL, 10)
    c.setFillColor(TEXT)
    c.drawCentredString(W / 2, y - 5 * mm, "DEBIT NOTE COPY")

    y -= 12 * mm

    # ── Main grid: insured (left) + premium (right) ──────────────────
    left_w = 115 * mm
    right_x = MX + left_w
    box_top = y; box_bottom = y - 65 * mm

    c.setStrokeColor(LINE); c.setLineWidth(0.5)
    c.rect(MX, box_bottom, W - 2 * MX, box_top - box_bottom, stroke=1, fill=0)
    c.line(right_x, box_top, right_x, box_bottom)

    # Left: Insured info
    ly = box_top - 5 * mm
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX + 3 * mm, ly, "ชื่อผู้เอาประกันภัย Insured Name")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10.5)
    c.drawString(MX + 5 * mm, ly - 6 * mm, (buyer.get("name") or "—")[:55])

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX + 3 * mm, ly - 13 * mm, "ที่อยู่ Address")
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
    addr = _wrap_text(buyer.get("address") or "", 60)
    ay = ly - 18 * mm
    for line in addr[:3]:
        c.drawString(MX + 5 * mm, ay, line)
        ay -= 4 * mm

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX + 3 * mm, box_bottom + 11 * mm, "อาชีพ Occupation")
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9.5)
    c.drawString(MX + 5 * mm, box_bottom + 6 * mm, insured_occupation or "-")

    # Right: Premium breakdown
    rcol_x = right_x + 3 * mm
    rcol_right = W - MX - 3 * mm

    rows = [
        ("เบี้ยประกันภัย",  "Premium",     net_premium, False),
        ("ส่วนลด",           "Discount",    discount,    False),
        ("อากรแสตมป์",       "Stamp Duty",  stamp_duty,  False),
        ("รวมเงิน",           "Total",       total_after_stamp, True),
        ("ภาษีมูลค่าเพิ่ม",  "VAT 7%",      vat,         False),
    ]

    row_h = (box_top - box_bottom - 14 * mm) / len(rows)  # leave bottom for grand total
    rry = box_top - 4 * mm
    for th, en, val, divider in rows:
        c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
        c.drawString(rcol_x, rry, th)
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
        c.drawString(rcol_x, rry - 3 * mm, en)
        c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
        c.drawRightString(rcol_right - 14, rry - 1.5 * mm, baht_fmt(val))
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
        c.drawString(rcol_right - 10, rry - 1.5 * mm, "บาท")
        c.drawString(rcol_right - 10, rry - 4.5 * mm, "Baht")
        rry -= row_h
        c.setStrokeColor(LINE); c.setLineWidth(0.3)
        c.line(right_x + 2 * mm, rry + 1 * mm, W - MX - 2 * mm, rry + 1 * mm)

    # Grand Total (last row)
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawString(rcol_x, rry, "รวมเงินทั้งสิ้น")
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rcol_x, rry - 3 * mm, "Grand Total")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 12)
    c.drawRightString(rcol_right - 14, rry - 1.5 * mm, baht_fmt(grand_total))
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(rcol_right - 10, rry - 1.5 * mm, "บาท")
    c.drawString(rcol_right - 10, rry - 4.5 * mm, "Baht")

    # Seq line (above main box, in right margin) — like "Seq.: 0001"
    if sequence_no:
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
        c.drawString(right_x + 3 * mm, box_top + 2 * mm, f"Seq. : {sequence_no}")

    y = box_bottom - 4 * mm

    # ── Vehicle period row ───────────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX, y, "วันเริ่มประกัน")
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX, y - 3 * mm, "Effective Date")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawString(MX + 28 * mm, y, effective_date or "—")

    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX + 78 * mm, y, "วันหมดอายุ")
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX + 78 * mm, y - 3 * mm, "Expiry Date")
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    c.drawString(MX + 105 * mm, y, expiry_date or "—")

    if effective_time:
        c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
        c.drawString(W - MX - 30 * mm, y, f"เวลา {effective_time}")

    y -= 8 * mm

    # ── Vehicle info table ───────────────────────────────────────────
    tbl_top = y
    tbl_bottom = y - 16 * mm
    cols_x = [MX, MX + 12*mm, MX + 50*mm, MX + 86*mm, MX + 122*mm, W - MX]
    headers = ["รหัส\nCode", "ชื่อรถยนต์\nMake/Model", "เลขทะเบียน\nLicense No.",
               "เลขตัวถัง\nChassis No.", "จำนวนที่นั่ง/ขนาด/น้ำหนัก\nNo. of seats/Disp/GVW"]

    c.setStrokeColor(LINE); c.setLineWidth(0.4)
    # outer
    c.rect(MX, tbl_bottom, W - 2 * MX, tbl_top - tbl_bottom, stroke=1, fill=0)
    # vertical lines
    for x in cols_x[1:-1]:
        c.line(x, tbl_top, x, tbl_bottom)
    # horizontal divider (after header)
    c.line(MX, tbl_top - 8 * mm, W - MX, tbl_top - 8 * mm)

    # headers
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    for i, h in enumerate(headers):
        lines = h.split("\n")
        for j, ln in enumerate(lines):
            c.drawString(cols_x[i] + 2 * mm, tbl_top - 3.5 * mm - j * 3 * mm, ln)

    # values
    values = [
        vehicle_code or "—",
        " ".join(filter(None, [car_make, car_model])) or "—",
        buyer.get("license_plate") or "—",
        chassis_no or "—",
        seats or "—",
    ]
    c.setFillColor(INK); c.setFont(_FONT_BOLD, 10)
    for i, v in enumerate(values):
        c.drawString(cols_x[i] + 2 * mm, tbl_top - 12 * mm, str(v)[:18])

    y = tbl_bottom - 4 * mm

    # ── Insurance type checkboxes ────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX, y, "ประเภทการประกันภัย")
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX, y - 3 * mm, "Type of Insurance")

    # 5 options with checkboxes
    options = [
        ("prb",       "พ.ร.บ.",       "Compulsory"),
        ("comp",      "ประเภท 1",     "Comprehensive"),
        ("3rd",       "ประเภท 2",     "Third Party,Fire & Theft"),
        ("3rd_only",  "ประเภท 3",     "Third Party Only"),
        ("other",     "อื่นๆ",         "Other"),
    ]
    opt_x = MX + 32 * mm
    for key, th, en in options:
        checked = (insurance_subtype == key)
        # box
        c.setStrokeColor(INK); c.setLineWidth(0.6)
        c.rect(opt_x, y - 1 * mm, 3 * mm, 3 * mm, stroke=1, fill=0)
        if checked:
            c.setFont(_FONT_BOLD, 9)
            c.drawString(opt_x + 0.5 * mm, y - 0.4 * mm, "X")
        c.setFillColor(INK); c.setFont(_FONT_NORMAL, 8.5)
        c.drawString(opt_x + 4.5 * mm, y, th)
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
        c.drawString(opt_x + 4.5 * mm, y - 3 * mm, en)
        opt_x += 35 * mm

    y -= 9 * mm

    # ── Sum Insured + Accessories ────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX, y, "จำนวนเงินเอาประกันภัย Sum Insured")
    c.setFont(_FONT_BOLD, 10)
    c.drawString(MX + 58 * mm, y, baht_fmt(sum_insured))
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX + 82 * mm, y, "บาท Baht")

    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX + 100 * mm, y, "อุปกรณ์ตกแต่งเพิ่ม Accessories")
    c.setFont(_FONT_BOLD, 10)
    c.drawString(MX + 145 * mm, y, baht_fmt(accessories))
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX + 168 * mm, y, "บาท")

    y -= 7 * mm

    # ── Use of vehicle + Broker ──────────────────────────────────────
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX, y, "การใช้รถยนต์ Use of vehicle")
    c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX + 42 * mm, y, use_of_vehicle or "ใช้ส่วนบุคคล ไม่ใช้รับจ้างหรือให้เช่า")

    y -= 5 * mm
    c.drawString(MX, y, "นายหน้า / ตัวแทน Broker / Agent")
    c.setFont(_FONT_BOLD, 9.5)
    bk = " ".join(filter(None, [broker_code, broker_name]))
    c.drawString(MX + 50 * mm, y, bk or "—")

    if agreement_date:
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
        c.drawString(W - MX - 80 * mm, y, "วันที่สัญญาประกันภัย Agreement made on")
        c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
        c.drawRightString(W - MX, y, agreement_date)

    y -= 7 * mm
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX, y, "โปรดจ่ายเป็นเช็คขีดคร่อม ในนาม")
    c.setFont(_FONT_BOLD, 9)
    c.drawString(MX + 48 * mm, y, f'"{TM_CHEQUE_TH}"')

    y -= 6 * mm

    # ── Remark + Commission ──────────────────────────────────────────
    c.setStrokeColor(LINE); c.setLineWidth(0.3)
    c.line(MX, y, W - MX, y)
    y -= 4 * mm

    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX, y, "หมายเหตุ Remark")
    if broker_code:
        c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
        c.drawString(MX + 24 * mm, y, broker_code)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawString(MX, y - 4 * mm, "Ins.")
    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX + 24 * mm, y - 4 * mm, (buyer.get("name") or "")[:55])

    # commission box (right)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
    c.drawString(W - MX - 65 * mm, y - 4 * mm, "กรณีผู้เอาประกันภัยเป็นนิติบุคคล หักภาษี ณ ที่จ่าย 1%")
    c.drawString(W - MX - 20 * mm, y - 4 * mm, f"= {baht_fmt(wht_1pct)} บาท")

    c.setFillColor(INK); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(W - MX - 65 * mm, y - 9 * mm, "ค่าคอมมิชชั่น")
    c.drawString(W - MX - 50 * mm, y - 9 * mm, "Commission")
    c.setFont(_FONT_BOLD, 9.5)
    c.drawString(W - MX - 28 * mm, y - 9 * mm, f"{commission_rate:.3f}%")
    c.drawRightString(W - MX, y - 9 * mm, baht_fmt(commission))

    y -= 18 * mm

    # ── Authorized signature box (right) ─────────────────────────────
    sig_x = W - MX - 70 * mm
    c.setStrokeColor(LINE); c.setLineWidth(0.4)
    c.line(sig_x, y, W - MX, y)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7.5)
    c.drawCentredString(sig_x + 35 * mm, y - 4 * mm, "ผู้รับมอบอำนาจ Authorized Signature")

    # broker signature (bottom-left)
    c.line(MX, y, MX + 80 * mm, y)
    c.drawCentredString(MX + 40 * mm, y - 4 * mm, "นายหน้า/ตัวแทน Broker / Agent")
    if broker_code:
        c.setFillColor(INK); c.setFont(_FONT_BOLD, 9)
        c.drawCentredString(MX + 40 * mm, y + 2 * mm, broker_code)

    # ── QR (if any) ──────────────────────────────────────────────────
    if promptpay_target:
        try:
            payload = generate_promptpay_payload(promptpay_target, grand_total)
            qr_png = generate_qr_image(payload)
            qr_size = 28 * mm
            y -= 16 * mm
            c.drawImage(ImageReader(io.BytesIO(qr_png)),
                        MX, y - qr_size, width=qr_size, height=qr_size)
            tx = MX + qr_size + 5 * mm
            c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8)
            c.drawString(tx, y - 3 * mm, "ชำระเงินผ่าน PromptPay")
            c.setFillColor(INK); c.setFont(_FONT_BOLD, 11)
            c.drawString(tx, y - 9 * mm, f"พร้อมเพย์ {promptpay_target}")
            c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
            c.drawString(tx, y - 14 * mm, f"ยอด {baht_fmt(grand_total)} บาท")
        except Exception as e:
            print(f"[invoice tm2] QR error: {e}")

    # ── Footer refs ──────────────────────────────────────────────────
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 7)
    c.drawString(MX, 8 * mm, "UPJRR0")
    c.drawString(MX, 5 * mm, invoice_date.strftime("%d/%m/%Y"))
    c.drawRightString(W - MX, 5 * mm, "V_VD-12-S")

    c.showPage()
    c.save()
    return buf.getvalue()


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """แบ่งบรรทัดง่ายๆ ตาม max_chars (ไม่ตัดคำกลาง)"""
    if not text: return []
    words = text.split()
    if not words: return []
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 <= max_chars:
            cur = (cur + " " + w).strip()
        else:
            if cur: lines.append(cur)
            cur = w
    if cur: lines.append(cur)
    return lines


# ── PDF Builder (Minimalist) ───────────────────────────────────────
def build_invoice_pdf(
    *,
    invoice_no: str,
    invoice_date: datetime,
    seller: dict,
    buyer: dict,
    items: list[dict],
    extra_fees: dict,
    vat_rate: float = 0.07,
    promptpay_target: str | None = None,
    note: str = "",
) -> bytes:
    _register_thai_fonts()

    subtotal = sum(it.get("quantity", 1) * it.get("unit_price", 0) for it in items)
    extra_total = sum(extra_fees.values()) if extra_fees else 0
    vat   = (subtotal + extra_total) * vat_rate
    total = subtotal + extra_total + vat

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    W, H = A4
    MX = 22 * mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ HEADER — Logo (left) + INVOICE title (right)                ║
    # ╚════════════════════════════════════════════════════════════╝
    y = H - 25*mm

    # Logo + company info (left)
    if LOGO_PATH.exists():
        try:
            c.drawImage(ImageReader(str(LOGO_PATH)),
                        MX, y - 8*mm, width=18*mm, height=18*mm,
                        mask='auto', preserveAspectRatio=True)
        except Exception as e:
            print(f"[invoice] logo error: {e}")

    info_x = MX + 22*mm
    c.setFillColor(INK)
    c.setFont(_FONT_MEDIUM, 13)
    c.drawString(info_x, y + 3*mm, seller.get("name") or "บริษัทของคุณ")

    c.setFillColor(MUTED)
    c.setFont(_FONT_NORMAL, 8.5)
    addr = (seller.get("address") or "")[:80]
    if addr:
        c.drawString(info_x, y - 1.5*mm, addr)
    contact = []
    if seller.get("phone"):  contact.append(f"โทร. {seller['phone']}")
    if seller.get("tax_id"): contact.append(f"เลขผู้เสียภาษี {seller['tax_id']}")
    if contact:
        c.drawString(info_x, y - 5.5*mm, "  ".join(contact))

    # Title "INVOICE" (right, big — use Medium for elegant look)
    c.setFillColor(INK)
    c.setFont(_FONT_MEDIUM, 34)
    c.drawRightString(W - MX, y + 3*mm, "INVOICE")
    c.setFont(_FONT_NORMAL, 9)
    c.setFillColor(MUTED)
    c.drawRightString(W - MX, y - 3*mm, "ใบแจ้งหนี้")

    y -= 22*mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ META: Bill To + Invoice info (2 columns)                    ║
    # ╚════════════════════════════════════════════════════════════╝
    # Left column: Bill To
    c.setFillColor(MUTED)
    c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX, y, "BILL TO  ·  ผู้ซื้อ")

    c.setFillColor(INK)
    c.setFont(_FONT_MEDIUM, 11)
    c.drawString(MX, y - 6*mm, buyer.get("name") or "—")

    c.setFillColor(TEXT)
    c.setFont(_FONT_NORMAL, 9)
    yL = y - 11*mm
    for line in [
        buyer.get("address") or "",
        f"โทร. {buyer['phone']}" if buyer.get("phone") else "",
        f"ทะเบียนรถ {buyer['license_plate']}" if buyer.get("license_plate") else "",
        f"เลขผู้เสียภาษี {buyer['tax_id']}" if buyer.get("tax_id") else "",
    ]:
        if line:
            c.drawString(MX, yL, line[:60])
            yL -= 4.5*mm

    # Right column: Invoice details
    rx = W/2 + 10*mm
    c.setFillColor(MUTED)
    c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(rx, y, "DETAILS  ·  รายละเอียด")

    # 2-row table-like layout
    def meta_row(yy, label, value):
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 9)
        c.drawString(rx, yy, label)
        c.setFillColor(INK); c.setFont(_FONT_MEDIUM, 10)
        c.drawString(rx + 30*mm, yy, value)

    meta_row(y - 6*mm,  "เลขที่",        invoice_no)
    meta_row(y - 11*mm, "วันที่",        fmt_thai_date(invoice_date))
    if promptpay_target:
        meta_row(y - 16*mm, "ชำระภายใน", "เมื่อได้รับเอกสาร")

    y -= 28*mm

    # ── Divider thin line ─────────────────────────────────────────
    c.setStrokeColor(LINE); c.setLineWidth(0.5)
    c.line(MX, y, W - MX, y)
    y -= 8*mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ ITEMS TABLE — ultra-clean, only bottom borders              ║
    # ╚════════════════════════════════════════════════════════════╝
    cols = [
        ("#",          MX,              8*mm,  "left"),
        ("DESCRIPTION", MX + 10*mm,      88*mm, "left"),
        ("QTY",        MX + 100*mm,     14*mm, "right"),
        ("UNIT PRICE", MX + 116*mm,     22*mm, "right"),
        ("TOTAL",      MX + 140*mm,     26*mm, "right"),
    ]

    # Header (no fill, just uppercase muted text + bottom line)
    c.setFillColor(MUTED)
    c.setFont(_FONT_BOLD, 8)
    for label, x, w, align in cols:
        ty = y
        if align == "left":     c.drawString(x, ty, label)
        else:                   c.drawRightString(x + w, ty, label)

    y -= 3*mm
    c.setStrokeColor(INK); c.setLineWidth(0.8)
    c.line(MX, y, W - MX, y)
    y -= 5*mm

    # Rows — no borders, just bottom thin line between
    c.setFont(_FONT_NORMAL, 10)
    for idx, it in enumerate(items, 1):
        desc = str(it.get("description", ""))[:55]
        qty  = it.get("quantity", 1)
        unit = it.get("unit_price", 0)
        line_total = qty * unit

        c.setFillColor(MUTED)
        c.setFont(_FONT_NORMAL, 9)
        c.drawString(cols[0][1], y, str(idx))
        c.setFillColor(INK)
        c.setFont(_FONT_NORMAL, 10)
        c.drawString(cols[1][1], y, desc)
        c.drawRightString(cols[2][1] + cols[2][2], y, f"{qty:g}")
        c.drawRightString(cols[3][1] + cols[3][2], y, baht_fmt(unit))
        c.setFont(_FONT_MEDIUM, 10)
        c.drawRightString(cols[4][1] + cols[4][2], y, baht_fmt(line_total))

        y -= 6.5*mm
        c.setStrokeColor(LINE); c.setLineWidth(0.3)
        c.line(MX, y + 1*mm, W - MX, y + 1*mm)
        y -= 0.5*mm

    # Extra fees as additional rows (subtle)
    if extra_fees:
        for label, val in extra_fees.items():
            if not val: continue
            c.setFillColor(MUTED)
            c.setFont(_FONT_NORMAL, 9)
            c.drawString(cols[1][1], y, f"  · {label}")
            c.drawRightString(cols[4][1] + cols[4][2], y, baht_fmt(val))
            y -= 5.5*mm
            c.setStrokeColor(LINE); c.setLineWidth(0.3)
            c.line(MX, y + 1*mm, W - MX, y + 1*mm)
            y -= 0.5*mm

    y -= 4*mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ SUMMARY (right-aligned)                                     ║
    # ╚════════════════════════════════════════════════════════════╝
    sum_x = W - MX - 60*mm

    def sum_line(label, value, bold=False, size=10):
        nonlocal y
        c.setFillColor(MUTED if not bold else INK)
        c.setFont(_FONT_NORMAL if not bold else _FONT_MEDIUM, size)
        c.drawString(sum_x, y, label)
        c.setFillColor(INK)
        c.setFont(_FONT_MEDIUM if bold else _FONT_NORMAL, size)
        c.drawRightString(W - MX, y, baht_fmt(value))
        y -= (size * 0.6) * mm

    sum_line("Subtotal", subtotal + extra_total)
    sum_line(f"VAT ({vat_rate*100:.0f}%)", vat)

    y -= 2*mm
    # Total line (top + bottom thin line, no fill)
    c.setStrokeColor(INK); c.setLineWidth(0.8)
    c.line(sum_x, y + 1*mm, W - MX, y + 1*mm)
    y -= 6*mm
    c.setFillColor(INK)
    c.setFont(_FONT_MEDIUM, 14)
    c.drawString(sum_x, y, "Total")
    c.drawRightString(W - MX, y, f"{baht_fmt(total)} ฿")
    y -= 2*mm
    c.setStrokeColor(INK); c.setLineWidth(0.8)
    c.line(sum_x, y, W - MX, y)
    y -= 6*mm

    # บาทถ้วน
    c.setFillColor(MUTED)
    c.setFont(_FONT_NORMAL, 8.5)
    c.drawRightString(W - MX, y, f"({baht_in_words(total)})")
    y -= 12*mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ PAYMENT QR + INSTRUCTIONS                                    ║
    # ╚════════════════════════════════════════════════════════════╝
    if promptpay_target:
        try:
            payload = generate_promptpay_payload(promptpay_target, total)
            qr_png  = generate_qr_image(payload)
            qr_size = 36*mm

            # QR on left
            c.drawImage(ImageReader(io.BytesIO(qr_png)),
                        MX, y - qr_size,
                        width=qr_size, height=qr_size)

            # Payment instructions (right of QR)
            tx = MX + qr_size + 8*mm
            c.setFillColor(MUTED)
            c.setFont(_FONT_NORMAL, 8.5)
            c.drawString(tx, y - 3*mm, "PAYMENT  ·  ชำระเงิน")

            c.setFillColor(INK)
            c.setFont(_FONT_MEDIUM, 13)
            c.drawString(tx, y - 10*mm, "PromptPay")

            c.setFont(_FONT_NORMAL, 10)
            c.setFillColor(TEXT)
            c.drawString(tx, y - 16*mm, f"พร้อมเพย์: {promptpay_target}")

            c.setFont(_FONT_NORMAL, 8.5)
            c.setFillColor(MUTED)
            c.drawString(tx, y - 23*mm, "สแกน QR ผ่านแอปธนาคารเพื่อชำระเงิน")
            c.drawString(tx, y - 27*mm, f"ยอด {baht_fmt(total)} บาท")

            y -= qr_size + 6*mm
        except Exception as e:
            print(f"[invoice] QR error: {e}")

    # ── Note ──────────────────────────────────────────────────────
    if note:
        c.setStrokeColor(LINE); c.setLineWidth(0.3)
        c.line(MX, y, W - MX, y)
        y -= 5*mm
        c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
        c.drawString(MX, y, "NOTE")
        c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
        c.drawString(MX + 18*mm, y, note[:120])
        y -= 8*mm

    # ╔════════════════════════════════════════════════════════════╗
    # ║ FOOTER — Thank you + signature lines                        ║
    # ╚════════════════════════════════════════════════════════════╝
    # Signature section
    sig_y = 30 * mm
    sig_w = 55 * mm

    c.setStrokeColor(INK); c.setLineWidth(0.5)
    c.line(MX, sig_y, MX + sig_w, sig_y)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(MX, sig_y - 4*mm, "ผู้จัดทำ  ·  Prepared by")
    c.setFillColor(TEXT); c.setFont(_FONT_NORMAL, 9)
    c.drawString(MX, sig_y - 9*mm, (seller.get('name') or '')[:35])

    rx = W - MX - sig_w
    c.setStrokeColor(INK)
    c.line(rx, sig_y, rx + sig_w, sig_y)
    c.setFillColor(MUTED); c.setFont(_FONT_NORMAL, 8.5)
    c.drawString(rx, sig_y - 4*mm, "ผู้รับเงิน  ·  Received by")

    # Thank you / footer
    c.setFillColor(MUTED)
    c.setFont(_FONT_NORMAL, 9)
    c.drawCentredString(W/2, 12*mm, "Thank you for your business")
    c.setFont(_FONT_NORMAL, 7.5)
    c.drawCentredString(W/2, 8*mm,
                        f"เอกสารนี้สร้างโดยระบบประกันคุ้มภัย · {fmt_thai_date(invoice_date)}")

    c.showPage()
    c.save()
    return buf.getvalue()
