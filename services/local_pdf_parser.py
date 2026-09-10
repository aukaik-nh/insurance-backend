"""Local, zero-cost PDF extraction for insurance documents.

This module deliberately does not use a cloud model.  It first reads the text
layer that insurers normally embed in their PDFs.  Only scanned PDFs fall back
to a locally installed Tesseract engine after rendering pages to images.

The result is a *candidate*, never an authority: callers use
``parse_confidence`` to decide whether it can bypass the optional Gemini
fallback or must be reviewed by a person.
"""
from __future__ import annotations

import os
import logging
import re
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any


LOCAL_PARSE_AUTO_CONFIDENCE = 0.70
_THAI_DIGITS = str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")
_MONTHS = {
    "ม.ค.": 1, "ก.พ.": 2, "มี.ค.": 3, "เม.ย.": 4, "พ.ค.": 5, "มิ.ย.": 6,
    "ก.ค.": 7, "ส.ค.": 8, "ก.ย.": 9, "ต.ค.": 10, "พ.ย.": 11, "ธ.ค.": 12,
    "มกราคม": 1, "กุมภาพันธ์": 2, "มีนาคม": 3, "เมษายน": 4, "พฤษภาคม": 5,
    "มิถุนายน": 6, "กรกฎาคม": 7, "สิงหาคม": 8, "กันยายน": 9, "ตุลาคม": 10,
    "พฤศจิกายน": 11, "ธันวาคม": 12,
}
_FIELD_LABELS = {
    "doc_type": "ประเภทเอกสาร", "policy_number": "เลขกรมธรรม์", "company_code": "รหัสบริษัท",
    "insured_name": "ผู้เอาประกันภัย", "insured_address": "ที่อยู่", "license_plate": "ทะเบียนรถ",
    "chassis_no": "เลขตัวถัง", "car_make": "ยี่ห้อและรุ่นรถ", "car_year": "ปีรถ",
    "sum_insured": "ทุนประกัน", "coverage_start": "เริ่มคุ้มครอง", "coverage_end": "สิ้นสุดคุ้มครอง",
    "net_premium": "เบี้ยสุทธิ", "stamp_duty": "อากรแสตมป์", "vat": "ภาษีมูลค่าเพิ่ม", "total_premium": "ยอดชำระรวม",
}
_EMPTY_FIELDS = (
    "doc_type", "policy_number", "company_code", "app_number", "policy_type",
    "new_renew", "insured_name", "insured_address", "phone", "license_plate",
    "license_province", "chassis_no", "car_make", "car_model", "car_year",
    "sum_insured", "coverage_start", "coverage_end", "net_premium", "stamp_duty",
    "vat", "total_premium", "third_party_per_person", "third_party_per_accident",
    "own_damage", "broker_name", "broker_license", "agent_code",
)
_REVIEW_AUTOFILL_FIELDS = {
    "policy_number", "insured_name", "insured_address", "license_plate", "license_province",
    "chassis_no", "car_make", "car_model", "car_year", "coverage_start", "coverage_end",
}


def _blank() -> dict[str, Any]:
    return {field: None for field in _EMPTY_FIELDS}


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    value = re.sub(r"\s+", " ", value).strip(" :|-\t")
    return value or None


def _lines(text: str) -> list[str]:
    return [_clean(line) for line in text.splitlines() if _clean(line)]


def _values_after_label(lines: list[str], labels: tuple[str, ...], max_len: int = 180) -> list[str]:
    candidates: list[str] = []
    for index, line in enumerate(lines):
        lower = line.lower()
        for label in labels:
            pos = lower.find(label.lower())
            if pos < 0:
                continue
            tail = _clean(line[pos + len(label):])
            if tail and len(tail) <= max_len:
                candidates.append(tail)
            if index + 1 < len(lines) and len(lines[index + 1]) <= max_len:
                candidates.append(lines[index + 1])
    return candidates


def _value_after_label(lines: list[str], labels: tuple[str, ...], max_len: int = 180) -> str | None:
    values = _values_after_label(lines, labels, max_len)
    return values[0] if values else None


def _normalise_date(value: str | None) -> str | None:
    if not value:
        return None
    value = value.translate(_THAI_DIGITS).strip()
    numeric = re.search(r"(?<!\d)(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})(?!\d)", value)
    if numeric:
        day, month, year = map(int, numeric.groups())
    else:
        thai = re.search(r"(?<!\d)(\d{1,2})\s*(ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*(\d{2,4})(?!\d)", value)
        if not thai:
            return None
        day, month_name, year = thai.groups()
        day, month, year = int(day), _MONTHS[month_name], int(year)
    if year < 100:
        year += 2500 if year >= 40 else 2000
    if year >= 2500:
        year -= 543
    try:
        return datetime(year, month, day).date().isoformat()
    except ValueError:
        return None


def _amount(value: str | None) -> float | None:
    if not value:
        return None
    match = re.search(r"(?<!\d)([0-9]{1,3}(?:,[0-9]{3})*(?:\.\d{1,2})?|[0-9]+(?:\.\d{1,2})?)(?!\d)", value.translate(_THAI_DIGITS))
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _amount_after_label(lines: list[str], labels: tuple[str, ...]) -> float | None:
    value = _value_after_label(lines, labels, max_len=100)
    return _amount(value)


def _validate_result(result: dict[str, Any]) -> list[str]:
    """Reject unsafe OCR guesses instead of silently pre-filling bad data."""
    warnings: list[str] = []

    name = result.get("insured_name")
    boilerplate = ("กรมธรรม์ประกันภัยนี้", "อิเล็กทรอนิกส์", "สำนักงาน คปภ")
    if name and (len(str(name)) > 120 or any(token in str(name) for token in boilerplate)):
        result["insured_name"] = None
        warnings.append("ชื่อผู้เอาประกันอ่านได้ไม่ชัด จึงเว้นไว้ให้ตรวจเอง")

    vin = str(result.get("chassis_no") or "").replace(" ", "").upper()
    # VIN มาตรฐานมี 17 ตัวและไม่ใช้ I/O/Q; ตัดค่าที่ OCR เดาเสี่ยงออก
    if vin and (len(vin) != 17 or any(char in vin for char in "IOQ")):
        result["chassis_no"] = None
        warnings.append("เลขตัวถังไม่ผ่านรูปแบบ VIN จึงไม่กรอกอัตโนมัติ")

    province = _clean(result.get("license_province"))
    if province in {"กท", "กทม", "กทม."}:
        result["license_province"] = "กรุงเทพมหานคร"

    amount_fields = ("net_premium", "stamp_duty", "vat", "total_premium")
    for field in amount_fields:
        value = result.get(field)
        if value is not None and (value <= 0 or value > 100_000_000):
            result[field] = None
            warnings.append("พบยอดเงินที่ผิดช่วง จึงไม่กรอกอัตโนมัติ")

    amounts = [result.get(field) for field in amount_fields]
    if all(value is not None for value in amounts):
        net, stamp, vat, total = amounts
        tolerance = max(1.0, total * 0.02)
        if abs((net + stamp + vat) - total) > tolerance:
            for field in amount_fields:
                result[field] = None
            warnings.append("ยอดเบี้ยไม่ผ่านสมการ เบี้ยสุทธิ + อากร + VAT = ยอดรวม")

    start, end = result.get("coverage_start"), result.get("coverage_end")
    if start and end:
        try:
            if datetime.fromisoformat(end) <= datetime.fromisoformat(start):
                result["coverage_start"] = result["coverage_end"] = None
                warnings.append("ช่วงวันคุ้มครองไม่สมเหตุผล จึงไม่กรอกอัตโนมัติ")
        except ValueError:
            result["coverage_start"] = result["coverage_end"] = None
            warnings.append("รูปแบบวันคุ้มครองไม่ถูกต้อง")

    return list(dict.fromkeys(warnings))


def _classify(text: str) -> str:
    upper = text.upper()
    if (
        "หนังสือแจ้งเตือนต่ออายุ" in text
        or "หนังสือแจ้งต่ออายุ" in text
        or "RENEWAL NOTICE" in upper
        or "MOTOR INSURANCE RENEWAL" in upper
    ):
        return "renewal_notice"
    if "ผู้ประสบภัยจากรถ" in text or "PROTECTION FOR VICTIMS" in upper:
        return "motor_prb"
    if any(marker in text for marker in ("สลักหลัง", "ยกเลิกกรมธรรม์", "ร.ย.11", "ร.ย. 11")):
        return "endorsement"
    if any(marker in upper for marker in ("CREDIT NOTE", "CREDIT-NOTE")) or "ใบคืนเบี้ย" in text or "ใบลดหนี้" in text:
        return "credit_note"
    if "ใบแจ้งหนี้" in text or "INVOICE" in upper:
        return "invoice"
    if "ใบเสร็จรับเงิน" in text or "RECEIPT" in upper:
        return "receipt"
    if "อัคคีภัย" in text or "FIRE INSURANCE" in upper:
        return "fire"
    if "SME INSURANCE" in upper or "สรรพธุรกิจ" in text:
        return "sme_property"
    if any(marker in upper for marker in ("PERSONAL ACCIDENT", "TRAVEL INSURANCE")) or any(
        marker in text for marker in ("อุบัติเหตุส่วนบุคคล", "ประกันภัยการเดินทาง")
    ):
        return "other_policy"
    if "ประกันภัยรถยนต์" in text or "MOTOR INSURANCE" in upper:
        return "motor_main"
    return "unknown"


def _policy_number(text: str) -> str | None:
    def valid(value: str | None) -> bool:
        compact = re.sub(r"\s", "", str(value or ""))
        upper = compact.upper()
        return (
            8 <= len(compact) <= 30
            and sum(char.isdigit() for char in compact) >= 5
            and not any(word in upper for word in ("EXPIRY", "DATE", "POLICYNO", "NUMBER"))
        )

    candidates: list[str] = []
    for pattern in (
        r"\bD[O0]-\d{2}-\d{2}/\d{4,8}\b",
        r"\bD[O0]-\d{2}-\d{7,10}\b",
        r"\b\d{2,4}-\d{4,6}-\d{4,8}\b",
        r"(?:Policy\s*(?:No\.?|Number)|เลขที่กรมธรรม์|กรมธรรม์ประกันภัยเลขที่)\s*[:#-]?\s*([A-Z0-9][A-Z0-9 /-]{6,29})",
    ):
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            value = match.group(1) if match.lastindex else match.group(0)
            value = _clean(value.upper())
            if not value:
                continue
            value = value.rstrip(". ,")
            if re.fullmatch(r"D[O0]-\d{2}-\d{2}/\d{4,8}", value, flags=re.IGNORECASE):
                value = "D0" + value[2:]
            compact = re.sub(r"\s", "", value)
            if compact.isdigit() and len(compact) >= 14:
                continue
            if valid(value):
                candidates.append(value)
    # Scanned schedules frequently turn the leading ``D0`` into ``00`` and
    # the slash into ``!`` or ``|``.  Accept that narrow motor-policy shape
    # and restore the canonical prefix/separators; dates cannot match because
    # the prefix is restricted to D/O/0.
    tolerant = re.compile(
        r"(?<![A-Z0-9])([D0OP]{1,2})\s*[-–—:!|]?\s*(\d{2})\s*[-–—:!|]\s*"
        r"(\d{2})\s*[/\\!|:]?\s*((?:\d[ \t]*){6,8})(?!\d)", re.IGNORECASE
    )
    for match in tolerant.finditer(text.upper()):
        prefix = match.group(1).replace("O", "0").replace("P", "D")
        if prefix != "D0":
            prefix = "D0"
        suffix = re.sub(r"\s", "", match.group(4))
        value = f"{prefix}-{match.group(2)}-{match.group(3)}/{suffix}"
        if valid(value) and value not in candidates:
            candidates.append(value)
    return candidates[0] if candidates else None


def _plate(text: str) -> tuple[str | None, str | None]:
    # Thai plates vary: 1กก 1234, กข 9999, and may include province on the same line.
    pattern = re.compile(r"(?<![ก-๙A-Za-z0-9])([0-9]?\s*[ก-ฮ]{1,3}\s*[0-9]{1,4})(?:\s+(กรุงเทพมหานคร|กทม\.?|[ก-ฮ]{2,20}))?")
    candidates = []
    for match in pattern.finditer(text):
        plate = re.sub(r"\s+", "", match.group(1))
        if plate in {"0ก00000", "00000"} or len(plate) < 4:
            continue
        province = _clean(match.group(2))
        if province == "กทม.":
            province = "กรุงเทพมหานคร"
        # A province on the same line is strong evidence; otherwise favor a
        # longer genuine plate over a short fragment created by OCR noise.
        candidates.append((bool(province), len(plate), plate, province))
    if not candidates:
        return None, None
    _, _, plate, province = max(candidates)
    return plate, province


def _vin(text: str) -> str | None:
    for candidate in re.findall(r"\b[A-Z0-9]{17}\b", text.upper()):
        if any(char.isalpha() for char in candidate) and any(char.isdigit() for char in candidate):
            return candidate
    # OCR frequently inserts one space inside a VIN at a printed cell boundary.
    for left, right in re.findall(r"\b([A-Z0-9]{6,12})\s+([A-Z0-9]{4,10})\b", text.upper()):
        candidate = left + right
        if len(candidate) == 17 and any(char.isalpha() for char in candidate) and any(char.isdigit() for char in candidate):
            return candidate
    return None


def _extract_dates(lines: list[str], text: str) -> tuple[str | None, str | None]:
    date_lines = [line for line in lines if any(token in line.lower() for token in (
        "เริ่มคุ้มครอง", "สิ้นสุดคุ้มครอง", "ระยะเวลาประกัน", "from", "to"))]
    candidates: list[str] = []
    for line in date_lines or [text]:
        for token in re.findall(r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*\d{2,4}", line):
            parsed = _normalise_date(token)
            if parsed and parsed not in candidates:
                candidates.append(parsed)
    # PSM 11 often places a duration label and its two dates in separate
    # blocks.  If the label-line pass found fewer than two values, inspect a
    # short window after the duration heading instead of the whole page (which
    # could mistake an issue date or a vehicle year for coverage dates).
    if len(candidates) < 2:
        date_pattern = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}\s*(?:ม\.ค\.|ก\.พ\.|มี\.ค\.|เม\.ย\.|พ\.ค\.|มิ\.ย\.|ก\.ค\.|ส\.ค\.|ก\.ย\.|ต\.ค\.|พ\.ย\.|ธ\.ค\.|มกราคม|กุมภาพันธ์|มีนาคม|เมษายน|พฤษภาคม|มิถุนายน|กรกฎาคม|สิงหาคม|กันยายน|ตุลาคม|พฤศจิกายน|ธันวาคม)\s*\d{2,4}"
        for index, line in enumerate(lines):
            if not any(token in line.lower() for token in (
                "ระยะเวลาประกัน", "period of insurance", "renewal period insured"
            )):
                continue
            for nearby in lines[max(0, index - 4):index + 12]:
                for token in re.findall(date_pattern, nearby):
                    parsed = _normalise_date(token)
                    if parsed and parsed not in candidates:
                        candidates.append(parsed)
            if len(candidates) >= 2:
                break
    return (candidates[0], candidates[1]) if len(candidates) >= 2 else (None, None)


def _vehicle_details(text: str) -> tuple[str | None, str | None, str | None]:
    """Extract conservative vehicle details from noisy full-page OCR."""
    lines = _lines(text)
    upper = " ".join(lines).upper()
    makes = (
        "TOYOTA", "HONDA", "ISUZU", "MITSUBISHI", "MAZDA", "NISSAN", "FORD",
        "CHEVROLET", "SUZUKI", "SUBARU", "BMW", "MERCEDES-BENZ", "MG", "BYD",
        "VOLVO", "LEXUS", "HYUNDAI", "KIA",
    )
    make = next((value for value in makes if re.search(rf"\b{re.escape(value)}\b", upper)), None)
    model = None
    if make:
        for line in lines:
            match = re.search(rf"\b{re.escape(make)}\s+([A-Z0-9][A-Z0-9 .+/-]{{1,24}})", line.upper())
            if match:
                model = match.group(1).strip(" .-/") or None
                break
    vehicle_year = None
    for index, line in enumerate(lines):
        if not re.search(r"\bYEAR\b|ปีรถ|ปีที่ผลิต", line, re.IGNORECASE):
            continue
        nearby = lines[max(0, index - 3):index + 4]
        values = [int(value) for value in re.findall(r"(?<!\d)(19\d{2}|20\d{2}|25\d{2})(?!\d)", " ".join(nearby))]
        vehicle_year = next((str(value) for value in values if 1950 <= value <= 2100), None)
        if vehicle_year:
            break
    return make, model, vehicle_year


def _insured_name_from_filename(filename: str) -> str | None:
    stem = re.sub(r"\.pdf$", "", str(filename or ""), flags=re.IGNORECASE).strip()
    stem = re.sub(r"[_]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    if re.match(r"^(?:นาย|นางสาว|นาง|บริษัท|ห้างหุ้นส่วน|บจก\.)\s*\S+", stem) and len(stem) <= 120:
        return stem
    return None


def _fill_missing_candidates(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    """Fill only empty OCR fields, preserving the first pass when readers disagree."""
    for field in _EMPTY_FIELDS:
        if primary.get(field) in (None, "") and secondary.get(field) not in (None, ""):
            primary[field] = secondary[field]
    primary["parse_warnings"] = list(dict.fromkeys(
        (primary.get("parse_warnings") or []) + (secondary.get("parse_warnings") or [])
    ))
    return primary


def _parse_text(text: str, engine: str) -> dict[str, Any]:
    result = _blank()
    lines = _lines(text)
    result["doc_type"] = _classify(text)
    result["policy_number"] = _policy_number(text)
    result["chassis_no"] = _vin(text)
    result["license_plate"], result["license_province"] = _plate(text)
    result["coverage_start"], result["coverage_end"] = _extract_dates(lines, text)
    result["car_make"], result["car_model"], result["car_year"] = _vehicle_details(text)

    names = _values_after_label(lines, ("ผู้เอาประกัน", "The Insured", "ชื่อ Name"))
    names = [name for name in names if name.lower() not in {"name", "ชื่อ"} and 4 < len(name) <= 120]
    result["insured_name"] = max(names, key=len) if names else None
    result["insured_address"] = _value_after_label(lines, ("ที่อยู่ Address", "ที่อยู่"), max_len=300)
    result["net_premium"] = _amount_after_label(lines, ("เบี้ยสุทธิ", "Net Premium"))
    result["stamp_duty"] = _amount_after_label(lines, ("อากร", "Stamp Duty"))
    result["vat"] = _amount_after_label(lines, ("ภาษี", "VAT"))
    result["total_premium"] = _amount_after_label(lines, ("เบี้ยรวม", "Total Premium", "รวมทั้งสิ้น"))

    company_markers = {
        "TOKIO MARINE": "TMSTH", "เมืองไทยประกันภัย": "MTI", "กรุงเทพประกันภัย": "BKI",
        "VIRIAH": "VIR", "วิริยะ": "VIR", "MSIG": "MSIG", "AXA": "AXA",
    }
    upper = text.upper()
    result["company_code"] = next((code for marker, code in company_markers.items() if marker.upper() in upper), None)

    warnings = _validate_result(result)
    evidence = sum(bool(result.get(field)) for field in (
        "doc_type", "policy_number", "license_plate", "chassis_no", "coverage_start", "coverage_end",
        "insured_name", "total_premium",
    ))
    confidence = min(0.95, evidence / 8)
    if result["doc_type"] == "unknown":
        confidence = max(0.0, confidence - 0.15)
    if result["net_premium"] is not None and result["stamp_duty"] is not None and result["vat"] is not None and result["total_premium"] is not None:
        if abs((result["net_premium"] + result["stamp_duty"] + result["vat"]) - result["total_premium"]) <= 1.0:
            confidence = min(0.98, confidence + 0.10)

    result.update({
        "parse_engine": engine,
        "parse_confidence": round(confidence, 2),
        "parse_warnings": warnings,
        "requires_review": bool(warnings) or confidence < LOCAL_PARSE_AUTO_CONFIDENCE,
        "raw_text": text[:12000],
    })
    return result


def parse_ocr_text(text: str) -> dict[str, Any]:
    """Convert already-OCRed text into safe insurance-field candidates."""
    return _parse_text(text, "local_ocr")


def _native_text(file_bytes: bytes) -> str:
    import pymupdf as fitz

    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        return "\n".join(page.get_text("text", sort=True) for page in doc).strip()
    finally:
        doc.close()


def _configure_tesseract(pytesseract: Any, language: str) -> str:
    """Configure free Thai/English OCR on Windows and in the Linux container."""
    command = os.getenv("TESSERACT_CMD")
    if not command and os.name == "nt":
        default_command = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
        if default_command.exists():
            command = str(default_command)
    if command:
        pytesseract.pytesseract.tesseract_cmd = command

    configured = os.getenv("LOCAL_OCR_TESSDATA")
    tessdata = Path(configured) if configured else Path(__file__).resolve().parents[1] / "tmp" / "tessdata"
    if not tessdata.is_dir():
        return "--oem 1 --psm 6"

    # Tesseract on Windows fails when traineddata is below a Unicode path.
    target_dir = Path(tempfile.gettempdir()) / "insurance_ocr_tessdata" if os.name == "nt" else tessdata
    target_dir.mkdir(parents=True, exist_ok=True)
    for lang in language.split("+"):
        source = tessdata / f"{lang}.traineddata"
        if not source.exists():
            continue
        target = target_dir / source.name
        if source.resolve() != target.resolve() and (
            not target.exists() or target.stat().st_size != source.stat().st_size
        ):
            shutil.copy2(source, target)
    os.environ["TESSDATA_PREFIX"] = str(target_dir)
    return "--oem 1 --psm 6"


def _ordered_full_page_ocr(image: Any, pytesseract: Any, config: str) -> str:
    """Use Tesseract's native block order without rebuilding Thai glyphs.

    TSV word boxes split Thai combining marks into separate pseudo-words.  That
    makes coordinate-based reconstruction visibly corrupt (spaces between every
    glyph), so the native TXT renderer is the safe ordered representation.  PSM
    11 emits each detected text block in reading order, which keeps the two
    columns of Thai insurance schedules from being interleaved line by line.
    """
    return pytesseract.image_to_string(image, lang="tha+eng", config=config, timeout=90).strip()


def _targeted_policy_number_ocr(image: Any, pytesseract: Any, config: str) -> str | None:
    """Read the small policy-number header without OCRing every table cell."""
    from PIL import Image, ImageOps
    from services.segmented_ocr import table_geometry

    deskewed, rows, columns, _ = table_geometry(image)
    boxes = []
    if rows and columns and len(columns[0]) >= 2:
        left, right = columns[0][0], columns[0][-1]
        width = right - left

        def band(index: int, x1: float, y1: float, x2: float, y2: float):
            top, bottom = rows[index:index + 2]
            return (int(left + width*x1), int(top + (bottom-top)*y1),
                    int(left + width*x2), int(top + (bottom-top)*y2))

        if len(rows) == 21:
            boxes.extend((band(0, .13, .34, .34, .58),
                          band(0, .167, .44, .31, .65),
                          band(0, .165, .43, .30, .64)))
        elif len(rows) == 14:
            boxes.append(band(0, .32, .65, .55, .89))
    if not boxes:
        boxes.append((0, 0, round(image.width * .7), round(image.height * .38)))

    targeted_config = re.sub(r"--psm\s+\d+", "--psm 7", config)
    targeted_config += " -c tessedit_char_whitelist=D0123456789-/"
    for box in boxes:
        crop = ImageOps.autocontrast(deskewed.crop(box), cutoff=1)
        for scale in (2, 3):
            enlarged = crop.resize(
                (round(crop.width * scale), round(crop.height * scale)),
                resample=Image.Resampling.LANCZOS,
            )
            text = pytesseract.image_to_string(
                enlarged, lang="eng", config=targeted_config, timeout=60
            )
            value = _policy_number(text)
            if value:
                return value
    return None


def _ocr_text(file_bytes: bytes, max_pages_override: int | None = None, artifacts: dict | None = None) -> str:
    import pymupdf as fitz
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps

    max_pages = max_pages_override or max(1, min(int(os.getenv("LOCAL_OCR_MAX_PAGES", "2")), 4))
    dpi = max(150, min(int(os.getenv("LOCAL_OCR_DPI", "220")), 350))
    language = os.getenv("LOCAL_OCR_LANG", "tha+eng")
    config = _configure_tesseract(pytesseract, language)
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        pages = []
        scale = dpi / 72
        for page_number in range(min(doc.page_count, max_pages)):
            page = doc[page_number]
            # Bound memory for scans with poster-sized page dimensions.
            page_scale = min(scale, 3600 / max(page.rect.width, page.rect.height))
            # Greyscale keeps peak RAM low when a browser drops a large batch of scans.
            pix = doc[page_number].get_pixmap(
                matrix=fitz.Matrix(page_scale, page_scale), colorspace=fitz.csGRAY, alpha=False
            )
            try:
                image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                if artifacts is not None and page_number == 0:
                    import base64
                    artifacts["image_data_url"] = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")
                    artifacts["page_count"] = doc.page_count
                image = ImageOps.autocontrast(image, cutoff=2).filter(ImageFilter.SHARPEN)
                pages.append(pytesseract.image_to_string(image, lang=language, config=config, timeout=90))
            finally:
                image = None
                pix = None
        return "\n".join(pages).strip()
    finally:
        doc.close()


def _remaining_pages_text(file_bytes: bytes) -> str:
    """Read pages after the first, preferring embedded text before OCR.

    The first page is handled by the layout reader. Later pages often contain
    endorsements or premium details, so silently ignoring them can produce an
    incomplete database record.
    """
    import pymupdf as fitz
    import pytesseract
    from PIL import Image, ImageFilter, ImageOps

    max_pages = max(1, min(int(os.getenv("LOCAL_OCR_MAX_PAGES", "10")), 20))
    dpi = max(150, min(int(os.getenv("LOCAL_OCR_DPI", "220")), 350))
    language = os.getenv("LOCAL_OCR_LANG", "tha+eng")
    config = re.sub(r"--psm\s+\d+", "--psm 11", _configure_tesseract(pytesseract, language))
    doc = fitz.open(stream=file_bytes, filetype="pdf")
    try:
        pages = []
        scale = dpi / 72
        for page_number in range(1, min(doc.page_count, max_pages)):
            page = doc[page_number]
            native = page.get_text("text", sort=True).strip()
            if len(re.sub(r"\s+", "", native)) >= 40:
                pages.append(native)
                continue
            page_scale = min(scale, 3600 / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(
                matrix=fitz.Matrix(page_scale, page_scale), colorspace=fitz.csGRAY, alpha=False
            )
            try:
                with Image.frombytes("L", (pix.width, pix.height), pix.samples) as image:
                    prepared = ImageOps.autocontrast(image, cutoff=2).filter(ImageFilter.SHARPEN)
                    pages.append(
                        pytesseract.image_to_string(
                            prepared, lang=language, config=config, timeout=90
                        ).strip()
                    )
            finally:
                pix = None
        return "\n".join(page for page in pages if page).strip()
    finally:
        doc.close()


def parse_pdf_locally(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """Parse a PDF without a paid API; OCR is used only when no text layer exists."""
    try:
        text = _native_text(file_bytes)
    except Exception as exc:
        return {**_blank(), "parse_engine": "local_failed", "parse_confidence": 0.0,
                "parse_error": f"เปิด PDF ไม่สำเร็จ: {str(exc)[:120]}"}

    meaningful = len(re.sub(r"\s+", "", text))
    if meaningful >= 80:
        return _parse_text(text, "native_text")

    try:
        ocr_text = _ocr_text(file_bytes)
    except Exception as exc:
        return {**_parse_text(text, "native_text_low_text"), "parse_error":
                f"PDF ไม่มี text layer และ OCR ในเครื่องอ่านไม่ได้: {str(exc)[:120]}"}
    result = _parse_text(ocr_text, "local_ocr")
    result["raw_text"] = ocr_text[:12000]
    return result


def parse_pdf_image_locally(file_bytes: bytes, filename: str = "") -> dict[str, Any]:
    """Render page 1 and fill only independently checked, layout-scoped candidates."""
    artifacts = {}
    generic_supplement = None
    stage = "setup"
    try:
        import base64
        import pymupdf as fitz
        from PIL import Image

        stage = "render"
        with fitz.open(stream=file_bytes, filetype="pdf") as doc:
            page = doc[0]
            full_page_mode = bool(os.getenv("RENDER_GIT_COMMIT")) or os.getenv(
                "LOCAL_OCR_MODE", ""
            ).lower() == "full_page"
            # Keep identifier crops sharp. Production workers get a longer OCR
            # deadline rather than sacrificing policy/VIN accuracy. The
            # bounded full-page production pass uses fewer pixels so it can
            # finish before the subprocess timeout on a shared CPU.
            default_dpi = "220" if full_page_mode else "300"
            layout_dpi = max(200, min(int(os.getenv("LOCAL_OCR_LAYOUT_DPI", default_dpi)), 300))
            scale = min(layout_dpi / 72, 3600 / max(page.rect.width, page.rect.height))
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csGRAY, alpha=False)
            artifacts["image_data_url"] = "data:image/png;base64," + base64.b64encode(pix.tobytes("png")).decode("ascii")
            artifacts["page_count"] = doc.page_count
            # Render first: missing OCR dependencies must not remove the user's
            # source preview or make a valid PDF appear broken.
            stage = "ocr"
            import pytesseract
            from services.segmented_ocr import read_document
            ocr_config = _configure_tesseract(pytesseract, "tha+eng")
            with Image.frombytes("L", (pix.width, pix.height), pix.samples) as image:
                # The segmented reader launches many crop OCR processes and is
                # ideal on a dedicated/local worker. Render's shared CPU can
                # time out every crop, so use two full-page passes there. All
                # such values stay review-only before commit.
                extracted = ({"layout": None, "field_evidence": {}, "raw_text": ""}
                             if full_page_mode else read_document(image))
                # Layout readers are intentionally strict.  Unknown insurer
                # templates still get a useful, review-only full-page OCR pass
                # so the form is not left completely empty.  This is local
                # Tesseract only; no image or text leaves the backend.
                if not extracted.get("layout"):
                    try:
                        native_fallback = _native_text(file_bytes)
                    except Exception:
                        native_fallback = ""
                    if len(re.sub(r"\s+", "", native_fallback)) >= 40:
                        fallback_text = native_fallback.strip()
                        fallback_engine = "native_text_fallback"
                    else:
                        # Let Tesseract detect sparse columns/blocks in native
                        # reading order. PSM 6 treats a multi-column policy as
                        # one paragraph and interleaves headings and values.
                        fallback_config = re.sub(r"--psm\s+\d+", "--psm 11", ocr_config)
                        fallback_text = _ordered_full_page_ocr(image, pytesseract, fallback_config)
                        fallback_engine = "python_tesseract_full_page_fallback"
                    if fallback_text:
                        generic = _parse_text(fallback_text, fallback_engine)
                        # A second page segmentation mode is useful for renewal
                        # notices: PSM 11 preserves sparse tables while PSM 6
                        # more reliably keeps policy numbers and vehicle rows.
                        if fallback_engine == "python_tesseract_full_page_fallback":
                            try:
                                second_text = _ordered_full_page_ocr(image, pytesseract, ocr_config)
                                if second_text and second_text != fallback_text:
                                    generic = _fill_missing_candidates(
                                        generic, _parse_text(second_text, "python_tesseract_block_fallback")
                                    )
                            except Exception:
                                generic.setdefault("parse_warnings", []).append(
                                    "รอบอ่านเสริมใช้เวลานานเกินไป จึงแสดงผลจากรอบแรกให้ตรวจ"
                                )
                        if not generic.get("policy_number"):
                            try:
                                generic["policy_number"] = _targeted_policy_number_ocr(
                                    image, pytesseract, ocr_config
                                )
                            except Exception:
                                generic.setdefault("parse_warnings", []).append(
                                    "กรอบเลขกรมธรรม์ใช้เวลานานเกินไป กรุณาตรวจและกรอกเลขจากต้นฉบับ"
                                )
                        later_text = _remaining_pages_text(file_bytes)
                        if later_text:
                            generic = _fill_missing_candidates(
                                generic, _parse_text(later_text, "later_pages_supplement")
                            )
                        filename_name = _insured_name_from_filename(filename)
                        if filename_name:
                            generic["insured_name"] = filename_name
                        generic["layout"] = None
                        generic["requires_review"] = True
                        generic["parse_warnings"] = list(dict.fromkeys(
                            ["อ่านเอกสารทั้งหน้าแบบอัตโนมัติแล้ว กรุณาตรวจหัวข้อและค่ากับต้นฉบับก่อนบันทึก"]
                            + generic.get("parse_warnings", [])
                        ))
                        generic["text_scope"] = "key_fields"
                        generic["preview"] = artifacts
                        # Only carry strongly structured values into the form.
                        # Names, addresses, plates and VINs remain evidence-only
                        # because full-page OCR can mix neighbouring columns.
                        safe_fields = {"policy_number", "coverage_start", "coverage_end"}
                        money = [generic.get(field) for field in ("net_premium", "stamp_duty", "vat", "total_premium")]
                        if all(value is not None for value in money):
                            net, stamp, vat, total = money
                            if abs(round(net + stamp + vat, 2) - round(total, 2)) <= .01:
                                safe_fields.update(("net_premium", "stamp_duty", "vat", "total_premium"))
                        evidence = {}
                        for field in _EMPTY_FIELDS:
                            value = generic.get(field)
                            if value is None or field in {"doc_type", "company_code"}:
                                continue
                            if field not in safe_fields and field not in _REVIEW_AUTOFILL_FIELDS:
                                continue
                            evidence[field] = {
                                "status": "candidate" if field in safe_fields else "review",
                                "value": value if field in safe_fields else None,
                                "manual_value": value,
                                "text": str(value),
                                "alternatives": [str(value)],
                                "label": _FIELD_LABELS.get(field, field),
                            }
                        generic["field_evidence"] = evidence
                        generic["review_fields"] = [field for field, item in evidence.items()
                                                    if item["status"] == "review"]
                        structured = ["ผลอ่านเอกสาร · ช่องรอตรวจเป็นข้อมูลเบื้องต้น กรุณาเทียบกับภาพต้นฉบับ"]
                        for field, item in evidence.items():
                            status = "รอตรวจ" if item["status"] == "review" else "อ่านตรงกัน · โปรดตรวจต้นฉบับ"
                            structured.append(f"{item['label']} [{status}]\n{item['text']}")
                        generic["raw_text"] = "\n\n".join(structured)
                        for field in _EMPTY_FIELDS:
                            if field in safe_fields:
                                continue
                            item = evidence.get(field) or {}
                            if field in _REVIEW_AUTOFILL_FIELDS and item.get("manual_value") not in (None, ""):
                                generic[field] = item["manual_value"]
                            elif field not in {"doc_type", "company_code"}:
                                generic[field] = None
                        if generic.get("doc_type") == "fire":
                            generic["policy_type"] = "FIRE"
                        return generic
                else:
                    # A recognised table reader can still miss a cell when the
                    # scan is faint. Supplement only its empty fields from an
                    # independent full-page pass; keep every supplement marked
                    # for operator review.
                    fallback_config = re.sub(r"--psm\s+\d+", "--psm 11", ocr_config)
                    fallback_text = _ordered_full_page_ocr(image, pytesseract, fallback_config)
                    if fallback_text:
                        generic_supplement = _parse_text(
                            fallback_text, "python_tesseract_full_page_supplement"
                        )
                    later_text = _remaining_pages_text(file_bytes)
                    if later_text:
                        later_supplement = _parse_text(later_text, "later_pages_supplement")
                        generic_supplement = _fill_missing_candidates(
                            generic_supplement or _blank(), later_supplement
                        )
    except Exception as exc:
        error_code = "ocr_dependency_missing" if isinstance(exc, ImportError) else f"{stage}_failed"
        logging.getLogger(__name__).warning("Local preview failed: stage=%s type=%s missing_module=%s",
                                           stage, type(exc).__name__, getattr(exc, "name", None))
        return {
            **_blank(),
            "parse_engine": "local_ocr_failed",
            "parse_confidence": 0.0,
            "parse_error": f"Python OCR อ่านภาพไม่สำเร็จ: {str(exc)[:160]}",
            "parse_error_code": error_code,
            "parse_warnings": ["ไม่สามารถอ่านข้อความจากภาพได้"],
            "requires_review": True,
            "preview": artifacts,
        }
    result = _blank()
    evidence = extracted.get("field_evidence", {})
    for field, item in evidence.items():
        if field in result and item.get("status") == "candidate":
            result[field] = item.get("value")
        elif field in result and field in _REVIEW_AUTOFILL_FIELDS and item.get("manual_value") not in (None, ""):
            # Show review-only OCR in the form for operator convenience. The UI
            # marks these fields yellow and they remain listed in review_fields.
            result[field] = item.get("manual_value")
    if extracted.get("layout"):
        result["company_code"] = "TMSTH"
        if extracted["layout"] == "tmsth_compulsory_motor_v1":
            result["doc_type"] = "motor_prb"
        elif extracted["layout"] == "tmsth_cancellation_endorsement_v1":
            result["doc_type"] = "endorsement"
        elif extracted["layout"] == "tmsth_fire_schedule_v1":
            result["doc_type"] = "fire"
            result["policy_type"] = "FIRE"
        else:
            result["doc_type"] = "motor_main"
    # A renewal notice uses the same insurer layout and old policy number as a
    # motor schedule. The explicit document title must win over that layout.
    if generic_supplement and generic_supplement.get("doc_type") == "renewal_notice":
        result["doc_type"] = "renewal_notice"
    if generic_supplement:
        for field in _REVIEW_AUTOFILL_FIELDS:
            value = generic_supplement.get(field)
            if result.get(field) not in (None, "") or value in (None, ""):
                continue
            result[field] = value
            evidence[field] = {
                "status": "review",
                "value": None,
                "manual_value": value,
                "text": str(value),
                "alternatives": [str(value)],
                "label": _FIELD_LABELS.get(field, field),
                "source": "full_page_supplement",
            }
    if result.get("car_make"):
        make, model, _ = _vehicle_details(str(result["car_make"]))
        if make and model:
            result["car_make"], result["car_model"] = make, model
            original = evidence.get("car_make", {})
            for field, value in (("car_make", make), ("car_model", model)):
                evidence[field] = {**original, "text": value, "manual_value": value,
                                   "label": "ยี่ห้อรถ" if field == "car_make" else "รุ่นรถ"}
    filename_name = _insured_name_from_filename(filename)
    if filename_name:
        result["insured_name"] = filename_name
        evidence["insured_name"] = {
            **evidence.get("insured_name", {}),
            "status": "review", "value": None, "manual_value": filename_name,
            "text": filename_name, "alternatives": [filename_name],
            "label": _FIELD_LABELS["insured_name"], "source": "filename",
        }
    warnings = (["แสดงข้อมูลที่อ่านได้แล้ว กรุณาตรวจช่องสีเหลืองกับภาพต้นฉบับก่อนบันทึก"]
                if evidence else ["ยังไม่รองรับรูปแบบตารางนี้ จึงไม่กรอกข้อมูลอัตโนมัติ กรุณากรอกโดยเทียบต้นฉบับ"])
    result.update({
        **extracted, "parse_engine": "python_tesseract_segmented",
        # Do not report a field-presence count as an accuracy probability.
        "parse_confidence": 0.0, "requires_review": True, "parse_warnings": warnings,
        "preview": artifacts, "text_scope": "key_fields" if evidence else "page",
        "review_fields": [key for key, value in evidence.items() if value["status"] == "review"],
    })
    return result


def is_confident(parsed: dict[str, Any]) -> bool:
    return float(parsed.get("parse_confidence") or 0) >= LOCAL_PARSE_AUTO_CONFIDENCE
