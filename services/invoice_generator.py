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
