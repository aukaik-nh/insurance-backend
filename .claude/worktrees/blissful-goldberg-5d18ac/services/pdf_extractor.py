"""
PDF text extractor — ลำดับการทำงาน:

  1) PyMuPDF direct text layer  (เร็วมาก, แม่น 100% ถ้า PDF มี text)
  2) Tesseract OCR (DPI 250, PSM 6) — อ่านเฉพาะหน้าแรก

EasyOCR ถูกปิดเพราะช้ามากบน CPU (ไม่มี GPU)
"""

import fitz
import io
import re
import os

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

import pytesseract

# Tesseract path (Windows)
TESSERACT_PATH = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
if os.path.exists(TESSERACT_PATH):
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_PATH




# ──────────────────────────────────────────────────────────────
# Text utilities
# ──────────────────────────────────────────────────────────────
def _fix_spaced_thai(text: str) -> str:
    """OCR มักใส่ space ระหว่างทุกตัวอักษรไทย → join คืน"""
    for _ in range(10):
        new = re.sub(r'([ก-๛][็-๎]?) ([ก-๛])', r'\1\2', text)
        if new == text:
            break
        text = new
    return text


def _count_meaningful_chars(text: str) -> int:
    if not text:
        return 0
    thai  = sum(1 for c in text if 'ก' <= c <= '๿')
    latin = sum(1 for c in text if c.isascii() and c.isalpha())
    digit = sum(1 for c in text if c.isdigit())
    return thai + latin + digit


def _has_meaningful_text(text: str, min_chars: int = 50) -> bool:
    return _count_meaningful_chars(text) >= min_chars


def _is_garbled_thai(text: str) -> bool:
    """
    ตรวจว่า text layer เป็น garbled (เช่น Safety/MSIG ใช้ custom font encoding)
    → ตัวอักษรไทยถูกแทนด้วย ASCII garbage ทำให้ Thai ratio ต่ำมาก

    สัญญาณ: มีตัวอักษรเยอะแต่ไม่มีภาษาไทยเลย
    """
    if not text or len(text) < 100:
        return False
    printable = sum(1 for c in text if c.isprintable() and not c.isspace())
    thai_chars = sum(1 for c in text if '฀' <= c <= '๿')
    if printable < 200:
        return False
    thai_ratio = thai_chars / printable
    # เอกสารประกันภัยไทยควรมีตัวอักษรไทย > 15% (form labels, name, address)
    # ถ้า < 3% แสดงว่า garbled font encoding
    return thai_ratio < 0.03


# ──────────────────────────────────────────────────────────────
# 1) Direct text extraction
# ──────────────────────────────────────────────────────────────
def _extract_direct_text(doc) -> str:
    out_parts = []
    for page in doc:
        txt = page.get_text("text")
        if txt:
            out_parts.append(txt)
    return "\n".join(out_parts)


# ──────────────────────────────────────────────────────────────
# 2) Image preprocessing — boost OCR accuracy ~10-20%
# ──────────────────────────────────────────────────────────────
def _preprocess_image(img: Image.Image) -> Image.Image:
    """
    เตรียมภาพให้ OCR แม่นขึ้น:
      - grayscale
      - autocontrast (ทำให้ pixel ใช้ full range 0-255)
      - sharpen (เน้นขอบตัวอักษร)
      - mild noise reduction
    """
    # 1. Grayscale
    img = img.convert("L")

    # 2. Autocontrast — เพิ่ม contrast อัตโนมัติ
    img = ImageOps.autocontrast(img, cutoff=2)

    # 3. Sharpen — เน้นขอบตัวอักษร
    img = img.filter(ImageFilter.SHARPEN)

    # 4. Slight contrast boost
    img = ImageEnhance.Contrast(img).enhance(1.4)

    return img


def _page_to_image(page, dpi: int = 250) -> Image.Image:
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    return Image.open(io.BytesIO(pix.tobytes("png")))


# ──────────────────────────────────────────────────────────────
# Tesseract OCR
# ──────────────────────────────────────────────────────────────
def _tesseract_page(img: Image.Image) -> str:
    pre = _preprocess_image(img)
    t6 = pytesseract.image_to_string(pre, lang="tha+eng", config=r'--oem 1 --psm 6')
    return _fix_spaced_thai(t6)


def _ocr_full(doc) -> str:
    print("[pdf_extractor] OCR engine: Tesseract (หน้าแรกเท่านั้น)")
    # อ่านแค่หน้าแรก — ข้อมูลกรมธรรม์อยู่หน้า 1 เสมอ
    page = doc[0]
    img  = _page_to_image(page)
    txt  = _tesseract_page(img)
    print(f"[pdf_extractor] page 1: {_count_meaningful_chars(txt)} chars")
    return txt


# ══════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════
def extract_text_from_pdf(file_bytes: bytes) -> str:
    doc = fitz.open(stream=file_bytes, filetype="pdf")

    try:
        # 1) Direct text layer
        direct = _extract_direct_text(doc)
        if _has_meaningful_text(direct, min_chars=150):
            if _is_garbled_thai(direct):
                # Safety/MSIG font encoding: มีตัวอักษรเยอะแต่ภาษาไทยหาย → ต้อง OCR
                thai_ratio = sum(1 for c in direct if '฀' <= c <= '๿') / max(1, len(direct))
                print(f"[pdf_extractor] garbled Thai font detected (thai_ratio={thai_ratio:.2%}) → OCR")
            else:
                print(f"[pdf_extractor] using DIRECT text layer ({_count_meaningful_chars(direct)} chars) ✓")
                return direct
        else:
            print(f"[pdf_extractor] direct text insufficient ({_count_meaningful_chars(direct)} chars) → OCR")

        # 2-3) OCR
        ocr_text = _ocr_full(doc)
        print(f"[pdf_extractor] OCR result: {_count_meaningful_chars(ocr_text)} meaningful chars")

        # รวมกับ direct ถ้ามี text บ้าง (เพิ่มโอกาสจับ keyword)
        if direct.strip():
            return ocr_text + "\n\n[--- direct text layer ---]\n" + direct
        return ocr_text

    finally:
        doc.close()
