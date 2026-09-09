"""Render one PDF page to PNG, OCR the PNG, and save text/JSON locally.

No AI model or network service is used. Example:
    python scripts/pdf_to_image_text.py "C:\\path\\policy.pdf"
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pymupdf
import pytesseract
from PIL import Image, ImageFilter, ImageOps

from services.local_pdf_parser import parse_ocr_text


DEFAULT_TESSERACT = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
DEFAULT_TESSDATA = ROOT / "tmp" / "tessdata"


def _safe_stem(name: str) -> str:
    safe = "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
    return safe.strip("_") or "document"


def render_page(pdf_path: Path, page_number: int, dpi: int, image_path: Path) -> None:
    with pymupdf.open(pdf_path) as document:
        if document.page_count == 0:
            raise ValueError("PDF ไม่มีหน้าเอกสาร")
        if not 1 <= page_number <= document.page_count:
            raise ValueError(f"PDF มี {document.page_count} หน้า แต่เลือกหน้า {page_number}")
        page = document[page_number - 1]
        scale = dpi / 72
        pixmap = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
        pixmap.save(image_path)


def ocr_image(image_path: Path, tessdata: Path, language: str) -> str:
    if not DEFAULT_TESSERACT.exists():
        raise FileNotFoundError(f"ไม่พบ Tesseract: {DEFAULT_TESSERACT}")
    if not (tessdata / "tha.traineddata").exists():
        raise FileNotFoundError(f"ไม่พบภาษาไทยสำหรับ OCR: {tessdata / 'tha.traineddata'}")

    # Tesseract for Windows cannot reliably open traineddata below a Unicode
    # path (this repository includes the Thai folder name "งาน"). Stage only
    # the requested free language files under the ASCII system temp path.
    staged_tessdata = Path(tempfile.gettempdir()) / "insurance_ocr_tessdata"
    staged_tessdata.mkdir(parents=True, exist_ok=True)
    for lang in language.split("+"):
        source = tessdata / f"{lang}.traineddata"
        if not source.exists():
            raise FileNotFoundError(f"ไม่พบภาษา {lang} สำหรับ OCR: {source}")
        target = staged_tessdata / source.name
        if not target.exists() or target.stat().st_size != source.stat().st_size:
            shutil.copy2(source, target)

    pytesseract.pytesseract.tesseract_cmd = str(DEFAULT_TESSERACT)
    os.environ["TESSDATA_PREFIX"] = str(staged_tessdata)
    with Image.open(image_path) as source:
        prepared = ImageOps.autocontrast(source.convert("L"), cutoff=2)
        prepared = prepared.filter(ImageFilter.SHARPEN)
        return pytesseract.image_to_string(prepared, lang=language, config="--oem 1 --psm 6").strip()


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="PDF 1 ไฟล์ -> PNG -> OCR text/JSON (ไม่ใช้ AI)")
    parser.add_argument("pdf", type=Path, help="ไฟล์ PDF ที่ต้องการทดลอง")
    parser.add_argument("--page", type=int, default=1, help="หน้าที่ต้องการอ่าน เริ่มจาก 1")
    parser.add_argument("--dpi", type=int, default=250, choices=range(150, 351), metavar="150-350")
    parser.add_argument("--lang", default="tha+eng")
    parser.add_argument("--tessdata", type=Path, default=DEFAULT_TESSDATA)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    pdf_path = args.pdf.resolve()
    if not pdf_path.is_file() or pdf_path.suffix.lower() != ".pdf":
        parser.error(f"ไม่พบไฟล์ PDF: {pdf_path}")

    output_dir = (args.output or ROOT / "output" / "pdf_to_image_text" / _safe_stem(pdf_path.stem)).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    image_path = output_dir / f"page-{args.page:03d}.png"
    text_path = output_dir / f"page-{args.page:03d}.txt"
    json_path = output_dir / "parsed.json"

    started = time.perf_counter()
    render_page(pdf_path, args.page, args.dpi, image_path)
    text = ocr_image(image_path, args.tessdata.resolve(), args.lang)
    text_path.write_text(text, encoding="utf-8")

    parsed = parse_ocr_text(text)
    parsed.pop("raw_text", None)
    report = {
        "source_pdf": str(pdf_path),
        "page": args.page,
        "dpi": args.dpi,
        "ocr_language": args.lang,
        "uses_ai": False,
        "image_file": str(image_path),
        "text_file": str(text_path),
        "text_characters": len(text),
        "elapsed_seconds": round(time.perf_counter() - started, 2),
        "parsed": parsed,
    }
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
