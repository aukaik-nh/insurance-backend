"""Conservative, local OCR for verified table layouts.

Coordinates are relative to detected table rules, never to a filename or known
policy value. Unknown layouts deliberately produce no automatic field values.
OCR agreement is evidence, not a calibrated probability of correctness.
"""
from __future__ import annotations

import hashlib
import base64
import csv
import difflib
import io
import os
import re
import shutil
import tempfile
import time
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps
import pytesseract

LABELS = {
    "policy_number": "เลขกรมธรรม์", "insured_name": "ผู้เอาประกันภัย",
    "insured_address": "ที่อยู่", "license_plate": "ทะเบียนรถ",
    "chassis_no": "เลขตัวถัง", "car_make": "ยี่ห้อและรุ่นรถ",
    "car_year": "ปีรถ", "sum_insured": "ทุนประกัน", "coverage_start": "เริ่มคุ้มครอง", "coverage_end": "สิ้นสุดคุ้มครอง",
    "net_premium": "เบี้ยสุทธิ", "stamp_duty": "อากรแสตมป์",
    "vat": "ภาษีมูลค่าเพิ่ม", "total_premium": "ยอดชำระรวม",
}
MONEY_FIELDS = ("net_premium", "stamp_duty", "vat", "total_premium")


@lru_cache(maxsize=1)
def model_config() -> str:
    """Use pinned models; fail closed if setup was not performed. No downloads in requests."""
    source = Path(os.getenv("LOCAL_OCR_MODELS", str(Path(__file__).resolve().parents[1] / "models/tessdata")))
    for lang in ("eng", "tha"):
        if not (source / f"{lang}.traineddata").is_file():
            raise RuntimeError("ติดตั้งชุดภาษาอ่านเอกสารด้วย scripts/install_ocr_models.py ก่อนใช้งาน")
    # Windows Tesseract cannot open Unicode paths. A versioned cache avoids
    # overwriting the legacy reader's models while another request is running.
    digest = hashlib.sha256()
    for lang in ("eng", "tha"):
        digest.update((source / f"{lang}.traineddata").read_bytes())
    target = Path(tempfile.gettempdir()) / ("insurance_ocr_" + digest.hexdigest()[:16]) if os.name == "nt" else source
    target.mkdir(parents=True, exist_ok=True)
    for lang in ("eng", "tha"):
        src, dst = source / f"{lang}.traineddata", target / f"{lang}.traineddata"
        if src.resolve() != dst.resolve() and not dst.exists():
            temporary = dst.with_suffix(".partial")
            shutil.copy2(src, temporary)
            temporary.replace(dst)
    # Short Windows temp paths contain no spaces on the supported deployment.
    # Forward slashes plus explicit quotes are accepted by POSIX shlex.
    model_path = str(target) if os.name == "nt" else '"' + str(target) + '"'
    return f"--oem 1 --tessdata-dir {model_path}"


def table_geometry(image: Image.Image):
    gray = np.asarray(image.convert("L")).copy()
    height, width = gray.shape
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    lines = cv2.HoughLinesP(binary, 1, np.pi / 1800, 200, minLineLength=width * .55, maxLineGap=50)
    angles = [np.degrees(np.arctan2(y2-y1, x2-x1)) for x1,y1,x2,y2 in lines[:, 0]
              if abs(y2-y1) < abs(x2-x1) * .08] if lines is not None else []
    angle = float(np.median(angles)) if angles else 0.0
    gray = cv2.warpAffine(gray, cv2.getRotationMatrix2D((width/2, height/2), angle, 1),
                         (width, height), borderValue=255)
    binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
    horizontal = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((1, max(30, width//30)), np.uint8))
    projection = cv2.dilate(horizontal, np.ones((21, 1), np.uint8))
    ys = np.where(np.count_nonzero(projection, axis=1) > width * .65)[0]
    groups = np.split(ys, np.where(np.diff(ys) > 8)[0]+1)
    rows = [int(np.median(g)) for g in groups if len(g)]
    if not rows:
        return Image.fromarray(gray), [], [], angle
    # The outer rules span all major rows, unlike text strokes or internal cells.
    vertical = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((35, 1), np.uint8))
    vertical = cv2.dilate(vertical, np.ones((1, 13), np.uint8))
    cols = []
    for top, bottom in zip(rows, rows[1:]):
        xs = np.where(np.count_nonzero(vertical[top+10:bottom-10], axis=0) > max(1, bottom-top-20)*.7)[0]
        groups = np.split(xs, np.where(np.diff(xs)>6)[0]+1)
        cols.append([int(np.median(g)) for g in groups if len(g)])
    return Image.fromarray(gray), rows, cols, angle


def _read(image, config, language="eng", scale=1, psm=7, deadline=None, strip_rules=False, extra_config=""):
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("เกินเวลาประมวลผลเอกสาร")
    if strip_rules:
        gray = np.asarray(image.convert("L")).copy()
        ink = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV | cv2.THRESH_OTSU)[1]
        count, components, stats, _ = cv2.connectedComponentsWithStats(ink)
        mask = np.zeros_like(ink)
        for component in range(1, count):
            x, y, w, h, area = stats[component]
            if area >= 3 and x > 0 and y > 0 and x+w < image.width and y+h < image.height and w < image.width*.45:
                mask[components == component] = 255
        clean = np.full_like(gray, 255)
        retained = cv2.dilate(mask, np.ones((3, 3), np.uint8)) > 0
        clean[retained] = gray[retained]
        image = Image.fromarray(clean)
    crop = image.resize((max(1, round(image.width*scale)), max(1, round(image.height*scale))), Image.Resampling.LANCZOS)
    crop = ImageOps.expand(crop, border=16, fill=255)
    # A crop that finishes in a few seconds locally can need more than 20s on
    # a shared production CPU. The document-level deadline still bounds the
    # total work, so allow each active OCR subprocess enough time to complete.
    timeout = min(60, max(5, deadline-time.monotonic())) if deadline is not None else 60
    return _native_text_and_confidence(crop, language, f"{config} --psm {psm} {extra_config}".strip(), timeout)


def _native_text_and_confidence(image, language, config, timeout):
    """One OCR process, native UTF-8 text plus TSV confidence.

    Thai TSV words can be individual glyphs. Rebuilding prose from bounding-box
    gaps corrupts word boundaries/combining marks, so TSV is ONLY for confidence.
    pytesseract 0.3.13's public multi-output helper cannot accept psm/model config;
    use its subprocess/timeout/tempfile adapter, covered by contract tests.
    """
    runner = pytesseract.pytesseract
    with runner.save(image) as (base, input_filename):
        runner.run_tesseract(
            input_filename=input_filename, output_filename_base=base,
            extension="", lang=language,
            config=f"{config} -c tessedit_create_txt=1 -c tessedit_create_tsv=1",
            timeout=timeout,
        )
        text = Path(base + ".txt").read_text(encoding="utf-8").strip()
        with Path(base + ".tsv").open(encoding="utf-8", newline="") as stream:
            rows = csv.DictReader(stream, delimiter="\t", quoting=csv.QUOTE_NONE)
            scores = [float(row["conf"]) for row in rows if row.get("text", "").strip() and float(row["conf"]) >= 0]
    return {"text": text, "min_confidence": round(min(scores), 1) if scores else 0.0}


def _evidence_text(evidence):
    parts = ["ผลอ่านหน้าแรก · ช่องรอตรวจเป็นข้อมูลเบื้องต้น กรุณาเทียบกับภาพต้นฉบับ"]
    for field, item in evidence.items():
        status = "รอตรวจ" if item["status"] == "review" else "อ่านตรงกัน · โปรดตรวจต้นฉบับ"
        parts.append(f"{LABELS[field]} [{status}]\n{item['text'] or 'อ่านไม่ได้'}")
    return "\n\n".join(parts)


def _read_prb(deskewed, rows, columns, config, deadline):
    """Recognize the ruled compulsory-motor schedule, not a particular customer.

    Reject other templates unless both table geometry and four printed anchors
    match. Coordinates are relative to detected table bands/cells.
    """
    if (len(rows) != 14 or any(len(columns[i]) != 7 for i in (4, 5, 9, 10))
            or len(columns[0]) != 2):
        return None
    left, right = columns[0][0], columns[0][-1]
    anchors = {}
    # The tiny company-code token is frequently lost beside the barcode
    # (for example ``TMSTH`` becomes a run of unrelated glyphs).  The printed
    # title and the three section anchors below remain stable enough to
    # identify this compulsory-motor profile.
    required = {0: ("theschedule",), 2: ("periodofinsurance", "from"),
                4: ("motorvehiclemodel", "chassis", "bodytype"),
                9: ("netpremium", "stamp", "vat")}
    for index, words in required.items():
        crop = deskewed.crop((left+16, rows[index]+6, right-16, rows[index+1]-6))
        value = re.sub(r"[^a-z0-9]", "", _read(crop, config, psm=6, deadline=deadline)["text"].lower())
        anchors[index] = value
        if not all(word in value for word in words):
            return None
    width = right-left
    def band(index, x1, y1, x2, y2):
        top, bottom = rows[index:index+2]
        return (int(left+width*x1), int(top+(bottom-top)*y1), int(left+width*x2), int(top+(bottom-top)*y2))
    regions = {
        # Some revisions place the leading ``D0`` a few pixels left of the
        # barcode. Start early enough to retain it, while staying below the
        # printed header text.
        "policy_number": band(0, .32, .65, .55, .89),
        "insured_name": band(1, .247, .06, .84, .53),
        "insured_address": band(1, .247, .52, .82, .97),
        "coverage_start": band(2, .37, .20, .52, .83),
        "coverage_end": band(2, .64, .20, .78, .83),
    }
    for field, cell in (("car_make", 1), ("license_plate", 2), ("chassis_no", 3)):
        regions[field] = (columns[5][cell]+10, rows[5]+8, columns[5][cell+1]-10, rows[6]-8)
    for field, cell in zip(MONEY_FIELDS, (2, 3, 4, 5)):
        regions[field] = (columns[10][cell]+12, rows[10]+6, columns[10][cell+1]-12, rows[11]-6)
    evidence = {}
    for field, box in regions.items():
        language = "tha+eng" if field in ("insured_name", "insured_address", "license_plate", "coverage_start", "coverage_end") else "eng"
        crop = deskewed.crop(box)
        psm = 6 if field == "insured_address" else 7
        if field == "insured_name":
            # Some policies add a second insured person below the company.
            # Include that line in the evidence; don't force two lines into PSM 7.
            lower = np.asarray(crop.convert("L"))[int(crop.height*.7):]
            if np.count_nonzero(lower < 160) > max(8, crop.width*.01):
                psm = 6
        # A single Thai name line needs line segmentation, not block OCR that
        # sometimes mistakes the tone marks for another line. Never spell-correct.
        whitelist = {
            "policy_number": "-c tessedit_char_whitelist=D0123456789-/",
            "chassis_no": "-c tessedit_char_whitelist=ABCDEFGHJKLMNPRSTUVWXYZ0123456789",
            "car_year": "-c tessedit_char_whitelist=0123456789",
            "net_premium": "-c tessedit_char_whitelist=0123456789,.",
            "stamp_duty": "-c tessedit_char_whitelist=0123456789,.",
            "vat": "-c tessedit_char_whitelist=0123456789,.",
            "total_premium": "-c tessedit_char_whitelist=0123456789,.",
        }.get(field, "")
        reads = [_read(crop, config, language, scale=scale,
                       psm=psm,
                       deadline=deadline, strip_rules=field in MONEY_FIELDS,
                       extra_config=whitelist)
                 for scale in ((2, 1) if field in ("insured_name", "insured_address") else (1, 2))]
        item = decide(field, reads)
        if field == "policy_number":
            normalised = [_normalize(field, read["text"]) for read in reads]
            if normalised[0] is not None and normalised[0] == normalised[1]:
                item.update(status="candidate", value=normalised[0],
                            text=normalised[0], manual_value=normalised[0])
        elif field == "chassis_no":
            valid = [value for value in (_normalize(field, read["text"]) for read in reads)
                     if value is not None]
            if valid:
                checked = [value for value in valid if _vin_check_digit_valid(value)]
                selected = checked[0] if len(set(checked)) == 1 else max(set(valid), key=valid.count)
                item["text"] = item["manual_value"] = selected
        if field in ("insured_name", "insured_address"):
            clean = _clean_review_prose(field, item.get("text", ""))
            if clean:
                item["text"] = item["manual_value"] = clean
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        item.update(box=list(box), label=LABELS[field],
                    source_image_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
        evidence[field] = item
    validate_groups(evidence)
    return {"layout": "tmsth_compulsory_motor_v1", "layout_checks": anchors,
            "field_evidence": evidence, "raw_text": _evidence_text(evidence)}


def _read_cancellation(deskewed, rows, columns, config, deadline):
    """Read the ruled TMSTH cancellation endorsement (ร.ย.11).

    This profile intentionally extracts only fields that map to the policy form.
    Refund figures are not copied into normal premium fields because they have a
    different accounting meaning.  A human can still verify the original page.
    """
    if (len(rows) != 19 or len(columns) < 18 or len(columns[7]) != 10
            or len(columns[8]) != 10
            or len(columns[0]) != 2):
        return None
    left, right = columns[0][0], columns[0][-1]
    anchors = {}
    required = {
        # The small word "Endorsement" is often degraded by the barcode, while
        # this longer printed sentence remains stable in the same band.
        1: ("attaching", "policyno"),
        7: ("license", "chassis", "model"),
    }
    for index, words in required.items():
        crop = deskewed.crop((left + 12, rows[index] + 3, right - 12, rows[index + 1] - 3))
        value = re.sub(r"[^a-z0-9]", "", _read(crop, config, "eng", psm=6, deadline=deadline)["text"].lower())
        anchors[index] = value
        if not all(word in value for word in words):
            return None

    width = right - left
    def band(index, x1, y1, x2, y2):
        top, bottom = rows[index:index + 2]
        return (int(left + width*x1), int(top + (bottom-top)*y1),
                int(left + width*x2), int(top + (bottom-top)*y2))

    regions = {
        "policy_number": band(1, .79, .08, .985, .82),
        "insured_name": band(2, .21, .03, .76, .38),
        "insured_address": band(2, .21, .36, .70, .94),
        "coverage_start": band(5, .28, .12, .49, .88),
        "coverage_end": band(5, .60, .12, .78, .88),
    }
    for field, cell in (("car_make", 2), ("license_plate", 3),
                        ("chassis_no", 4), ("car_year", 5)):
        band_height = rows[9] - rows[8]
        regions[field] = (columns[8][cell] + 6, int(rows[8] + band_height*.18),
                          columns[8][cell + 1] - 6, int(rows[9] - band_height*.06))

    evidence = {}
    for field, box in regions.items():
        language = "tha+eng" if field in ("insured_name", "insured_address", "license_plate",
                                            "coverage_start", "coverage_end") else "eng"
        prose = field in ("insured_name", "insured_address", "car_make")
        crop = deskewed.crop(box)
        # One pass keeps this profile responsive.  Only policy/chassis receive a
        # second independent scale and may be auto-filled; all prose stays review.
        scales = ((1, 2) if field in ("policy_number", "chassis_no")
                  else (2,) if field == "insured_address"
                  else (1,))
        reads = [_read(crop, config, language, scale=scale,
                       psm=6 if prose else 7, deadline=deadline)
                 for scale in scales]
        item = decide(field, reads)
        normalised = _normalize(field, reads[0]["text"])
        if normalised is not None:
            item["manual_value"] = normalised
        # In this narrow, anchor-verified cell, two scale reads that produce the
        # same fully formed policy identifier are sufficient for a draft value.
        # The whole document still requires review before saving.
        if field == "policy_number" and len(reads) == 2:
            normalised_reads = [_normalize(field, read["text"]) for read in reads]
            if normalised_reads[0] is not None and normalised_reads[0] == normalised_reads[1]:
                item.update(status="candidate", value=normalised_reads[0])
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        item.update(box=list(box), label=LABELS[field],
                    source_image_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
        evidence[field] = item
    return {"layout": "tmsth_cancellation_endorsement_v1", "layout_checks": anchors,
            "field_evidence": evidence, "raw_text": _evidence_text(evidence)}


def _fire_money_value(text):
    """Extract one printed currency value, tolerating OCR's comma/dot swap.

    Fire schedules use both a thousands separator and exactly two decimal
    places.  We only accept that complete shape; tax percentages and integers
    elsewhere in the cell are deliberately ignored.
    """
    translated = str(text or "").translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789"))
    values = []
    for token in re.findall(r"(?<!\d)(\d{1,3}(?:[,.]\d{3})*[,.]\d{2})(?!\d)", translated):
        whole, decimals = token[:-3], token[-2:]
        try:
            value = float(re.sub(r"[,.]", "", whole) + "." + decimals)
            if value > 0:
                values.append(value)
        except ValueError:
            pass
    return values[0] if len(values) == 1 else None


def _clean_fire_address(lines):
    """Clean only stable OCR label variants; never invent address numbers."""
    cleaned = []
    for line in lines:
        line = line.strip(" |")
        line = re.sub(r"^[A-Za-z|]+\s*", "", line)
        for marker in ("ตําบล", "ตำบล", "จังหวัด"):
            if marker in line:
                line = line[line.index(marker):]
                break
        line = (line.replace("ตําบล", "ตำบล")
                    .replace("อําเภอ", "อำเภอ")
                    .replace("สมทรสาคร", "สมุทรสาคร"))
        if line:
            cleaned.append(line)
    return "\n".join(cleaned)


def _read_fire(deskewed, rows, columns, config, deadline):
    """Read the ruled TMSTH fire-insurance schedule.

    The page is accepted only when its table structure and four independent
    printed anchor bands match.  Thai prose remains review-only, while narrow
    identifiers, paired dates and the complete premium equation may be used as
    draft values when two different OCR passes agree.
    """
    if (len(rows) != 20 or len(columns) < 19 or len(columns[0]) != 3
            or len(columns[7]) != 5 or len(columns[18]) != 8):
        return None
    left, right = columns[0][0], columns[0][-1]
    anchors = {}
    anchor_regions = {
        "title": (left + 12, max(0, rows[0] - 150), right - 12, rows[0] - 5),
        0: (left + 12, rows[0] + 2, right - 12, rows[1] - 2),
        2: (left + 12, rows[2] + 2, right - 12, rows[3] - 2),
        7: (left + 12, rows[7] + 2, right - 12, rows[8] - 2),
    }
    required = {
        "title": ("fireinsuranceschedule",),
        0: ("tmsth", "policyno"),
        2: ("periodofinsurance", "from", "to"),
        7: ("netpremium", "stampduty", "vat", "total"),
    }
    for key, box in anchor_regions.items():
        value = re.sub(r"[^a-z0-9]", "", _read(
            deskewed.crop(box), config, "eng", psm=6, deadline=deadline
        )["text"].lower())
        anchors[key] = value
        if not all(word in value for word in required[key]):
            return None

    width = right - left
    def band(index, x1, y1, x2, y2):
        top, bottom = rows[index:index + 2]
        return (int(left + width*x1), int(top + (bottom-top)*y1),
                int(left + width*x2), int(top + (bottom-top)*y2))

    regions = {
        "policy_number": band(0, .62, .08, .94, .85),
        "insured_name": band(1, .05, .20, .57, .45),
        "insured_address": band(1, .05, .45, .57, .98),
        "coverage_start": band(2, .18, .08, .52, .78),
        "coverage_end": band(2, .61, .08, .88, .78),
        "sum_insured": band(3, .25, .05, .68, .90),
    }
    for field, cell in zip(MONEY_FIELDS, range(4)):
        regions[field] = (columns[7][cell] + 10, rows[7] + 5,
                          columns[7][cell + 1] - 10, rows[8] - 5)

    from services.local_pdf_parser import _normalise_date

    evidence = {}
    for field, box in regions.items():
        crop = deskewed.crop(box)
        if field in (*MONEY_FIELDS, "sum_insured"):
            # PSM 11 finds the isolated amount among bilingual labels; PSM 6 at
            # another scale provides independent readings of the same cell.
            reads = [
                _read(crop, config, "eng", scale=scale, psm=psm, deadline=deadline)
                for scale, psm in ((1, 6), (2, 6), (1, 11), (2, 11))
            ]
            values = [_fire_money_value(read["text"]) for read in reads]
        elif field == "policy_number":
            reads = [_read(crop, config, "eng", scale=scale, psm=7, deadline=deadline)
                     for scale in (1, 2)]
            values = []
            for read in reads:
                match = re.search(r"D[0O]-\d{2}-\d{2}/\d{6,8}", read["text"].upper())
                values.append(match.group(0) if match else None)
        elif field in ("coverage_start", "coverage_end"):
            reads = [_read(crop, config, "tha+eng", scale=scale, psm=7, deadline=deadline)
                     for scale in (1, 2)]
            values = [_normalise_date(read["text"]) for read in reads]
        else:
            reads = [_read(crop, config, "tha+eng" if field != "sum_insured" else "eng",
                           scale=scale, psm=6 if field in ("insured_name", "insured_address") else 7,
                           deadline=deadline) for scale in (1, 2)]
            values = [None, None]

        recognised = [value for value in values if value is not None]
        agreed = len(recognised) >= 2 and len(set(recognised)) == 1
        agreed_value = recognised[0] if agreed else None
        display_index = next((index for index, value in enumerate(values)
                              if value == agreed_value), 0)
        item = {
            "status": "candidate" if agreed and field not in ("insured_name", "insured_address") else "review",
            "value": agreed_value if agreed and field not in ("insured_name", "insured_address") else None,
            "text": reads[display_index]["text"],
            "alternatives": list(dict.fromkeys(read["text"] for read in reads)),
            "min_confidence": min(read["min_confidence"] for read in reads),
        }
        if field == "insured_name":
            lines = [re.sub(r"^[|๒2\s]+|[|๒2\s]+$", "", line).strip()
                     for line in reads[0]["text"].splitlines()]
            clean = " ".join(line for line in lines if len(line) > 2)
            if clean:
                item["text"] = item["manual_value"] = clean
        elif field == "insured_address":
            clean = _clean_fire_address(reads[0]["text"].splitlines())
            if clean:
                item["text"] = item["manual_value"] = clean
        elif agreed_value is not None:
            item["manual_value"] = agreed_value

        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        item.update(box=list(box), label=LABELS[field],
                    source_image_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
        evidence[field] = item

    validate_groups(evidence)
    return {"layout": "tmsth_fire_schedule_v1", "layout_checks": anchors,
            "field_evidence": evidence, "raw_text": _evidence_text(evidence)}


def _read_electric_motor(deskewed, rows, columns, config, deadline):
    """Read the TMSTH electric-motor schedule with a separate policy row."""
    if (len(rows) != 21 or len(columns) < 20 or len(columns[1]) < 3
            or len(columns[7]) != 10 or len(columns[8]) != 10
            or len(columns[16]) != 5):
        return None
    left, right = columns[0][0], columns[0][-1]
    anchors = {}
    required = {
        0: ("electricmotorinsurance", "tmsth"),
        15: ("netpremium", "stampduty", "vat", "total"),
    }
    for index, words in required.items():
        crop = deskewed.crop((left + 16, rows[index] + 2, right - 16, rows[index + 1] - 2))
        value = re.sub(r"[^a-z0-9]", "", _read(crop, config, "eng", psm=6, deadline=deadline)["text"].lower())
        anchors[index] = value
        if not all(word in value for word in words):
            return None

    width = right - left
    def band(index, x1, y1, x2, y2):
        top, bottom = rows[index:index + 2]
        return (int(left + width*x1), int(top + (bottom-top)*y1),
                int(left + width*x2), int(top + (bottom-top)*y2))

    regions = {
        "policy_number": band(1, .17, .08, .36, .78),
        "insured_name": band(2, .19, .03, .58, .39),
        "insured_address": band(2, .19, .43, .60, .95),
        "coverage_start": band(5, .31, .10, .49, .90),
        "coverage_end": band(5, .62, .10, .77, .90),
    }
    value_height = rows[9] - rows[8]
    for field, cell in (("car_make", 2), ("license_plate", 3),
                        ("chassis_no", 4), ("car_year", 5)):
        regions[field] = (columns[8][cell] + 7, int(rows[8] + value_height*.12),
                          columns[8][cell + 1] - 7, int(rows[9] - value_height*.07))
    for field, cell in zip(MONEY_FIELDS, range(4)):
        regions[field] = (columns[16][cell] + 12, rows[16] + 4,
                          columns[16][cell + 1] - 12, rows[17] - 3)

    evidence = {}
    for field, box in regions.items():
        language = "tha+eng" if field in ("insured_name", "insured_address", "license_plate",
                                            "coverage_start", "coverage_end") else "eng"
        prose = field in ("insured_name", "insured_address", "car_make")
        crop = deskewed.crop(box)
        repeat_for_candidate = field in ("policy_number", "car_year", *MONEY_FIELDS)
        reads = [_read(crop, config, language, scale=scale,
                       psm=6 if prose else 7, deadline=deadline,
                       strip_rules=field in MONEY_FIELDS)
                 for scale in ((1, 2) if repeat_for_candidate else (1,))]
        item = decide(field, reads)
        normalised = _normalize(field, reads[0]["text"])
        if normalised is not None:
            item["manual_value"] = normalised
        if field == "policy_number" and len(reads) == 2:
            normalised_reads = [_normalize(field, read["text"]) for read in reads]
            if normalised_reads[0] is not None and normalised_reads[0] == normalised_reads[1]:
                item.update(status="candidate", value=normalised_reads[0])
        buffer = io.BytesIO()
        crop.save(buffer, format="PNG")
        item.update(box=list(box), label=LABELS[field],
                    source_image_url="data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
        evidence[field] = item
    validate_groups(evidence)
    return {"layout": "tmsth_electric_motor_schedule_v1", "layout_checks": anchors,
            "field_evidence": evidence, "raw_text": _evidence_text(evidence)}


def _normalize(field, text):
    from services.local_pdf_parser import _normalise_date
    compact = re.sub(r"\s+", "", text)
    if field in MONEY_FIELDS:
        if re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}", compact):
            return float(compact.replace(",", ""))
    elif field in ("coverage_start", "coverage_end"):
        parsed = _normalise_date(compact)
        if parsed:
            return parsed
        # Thai month names are long enough that one OCR glyph error should not
        # discard an otherwise complete date.  Correct only a close month name;
        # the day/year still have to be present and form a valid calendar date.
        fuzzy = re.fullmatch(r"(\d{1,2})([ก-๙.]+)(\d{4})", compact.translate(str.maketrans("๐๑๒๓๔๕๖๗๘๙", "0123456789")))
        if fuzzy:
            months = ("มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
                      "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม")
            close = difflib.get_close_matches(fuzzy.group(2), months, n=1, cutoff=.78)
            if close:
                return _normalise_date(f"{fuzzy.group(1)} {close[0]} {fuzzy.group(3)}")
        return None
    elif field == "policy_number":
        match = re.search(r"D[0O]-\d{2}-\d{2}/\d{6,8}", compact)
        if match:
            return "D0" + match.group(0)[2:]
    elif field == "chassis_no":
        if re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", compact):
            return compact
    elif field == "license_plate":
        match = re.fullmatch(r"([0-9]?[ก-ฮ]{2}\d{1,4})(?:กท|กทม\.?|กรุงเทพมหานคร)", compact)
        if match:
            return match[1]
    elif field == "car_year":
        if re.fullmatch(r"(?:19|20)\d{2}", compact):
            return int(compact)
    return None


def _vin_check_digit_valid(value):
    """Validate the ISO 3779 check digit when a 17-character VIN provides it."""
    if not re.fullmatch(r"[A-HJ-NPR-Z0-9]{17}", value or ""):
        return False
    transliteration = {
        **{str(number): number for number in range(10)},
        **dict(zip("ABCDEFGH", range(1, 9))),
        "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
        "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
    }
    weights = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
    remainder = sum(transliteration[char] * weight for char, weight in zip(value, weights)) % 11
    expected = "X" if remainder == 10 else str(remainder)
    return value[8] == expected


def _clean_review_prose(field, text):
    lines = []
    label_only = {"ชื่อ", "name", "ที่อยู่", "address", "อาชีพ", "occupation"}
    for raw_line in (text or "").splitlines():
        line = re.sub(r"\s+[A-Za-z]{1,3}$", "", re.sub(r"[ \t]+", " ", raw_line)).strip(" .|_'")
        # A scan can leave a single Thai glyph beside a column edge (for
        # example ``Ca ง w``). It is not a name/address and only makes the
        # review text look corrupt, so require a real Thai word fragment.
        if not line or len(re.findall(r"[ก-๙]", line)) < 3 or line.lower() in label_only:
            continue
        lines.append(line)
    return "\n".join(lines)


def decide(field, reads):
    """Keep uncertain values as evidence, not auto-filled fields."""
    values = [_normalize(field, read["text"]) for read in reads]
    minimum = min(read["min_confidence"] for read in reads)
    # These are OCR heuristics, not measured accuracy percentages. Identifiers
    # need a higher threshold; Thai prose always stays manual (diacritics matter).
    threshold = (95 if field in ("policy_number", "license_plate", "chassis_no")
                 else 70 if field in ("coverage_start", "coverage_end") else 75)
    accepted = len(reads) >= 2 and values[0] is not None and all(v == values[0] for v in values) and minimum >= threshold
    return {"status": "candidate" if accepted else "review", "value": values[0] if accepted else None,
            "text": reads[0]["text"], "alternatives": list(dict.fromkeys(r["text"] for r in reads)),
            "min_confidence": minimum}


def validate_groups(evidence):
    """Money is atomic, never inferred from tax rates or silently repaired."""
    from datetime import date
    money = [evidence[k]["value"] for k in MONEY_FIELDS]
    valid = all(v is not None and 0 <= v < 100_000_000 for v in money)
    if valid:
        net, stamp, vat, total = money
        valid = net > 0 and total > 0 and abs(round(net+stamp+vat, 2)-total) <= .01
    if not valid:
        for key in MONEY_FIELDS:
            evidence[key].update(value=None, status="review", reason="ยอดเงินต้องอ่านครบและบวกรวมตรงกันถึงระดับสตางค์")
    start, end = (evidence[k]["value"] for k in ("coverage_start", "coverage_end"))
    if not start or not end or not 0 < (date.fromisoformat(end)-date.fromisoformat(start)).days <= 730:
        for key in ("coverage_start", "coverage_end"):
            evidence[key].update(value=None, status="review", reason="ต้องตรวจวันเริ่มและสิ้นสุดคู่กัน")


def read_document(image: Image.Image):
    config = model_config()
    # A small production CPU can need longer than a developer machine. Keep a
    # hard deadline so one damaged file cannot block the whole batch forever.
    timeout_seconds = max(60, min(int(os.getenv("SEGMENTED_OCR_TIMEOUT_SECONDS", "180")), 300))
    deadline = time.monotonic() + timeout_seconds
    deskewed, rows, columns, angle = table_geometry(image)
    result = {"layout": None, "field_evidence": {}, "deskew_degrees": round(angle, 3)}
    fire = _read_fire(deskewed, rows, columns, config, deadline)
    if fire:
        return {**result, **fire}
    electric = _read_electric_motor(deskewed, rows, columns, config, deadline)
    if electric:
        return {**result, **electric}
    cancellation = _read_cancellation(deskewed, rows, columns, config, deadline)
    if cancellation:
        return {**result, **cancellation}
    prb = _read_prb(deskewed, rows, columns, config, deadline)
    if prb:
        return {**result, **prb}
    # First supported profile: one-vehicle TMSTH schedule. A wrong layout is
    # not forced into this profile. Four independent printed anchors are required.
    if len(rows) == 21 and len(columns[7]) == 10 and len(columns[16]) == 5:
        left, right = columns[0][0], columns[0][-1]
        anchors = {}
        for index in (0, 4, 6, 15):
            crop = deskewed.crop((left+18, rows[index]+2, right-18, rows[index+1]-2))
            anchors[index] = re.sub(r"[^a-z0-9]", "", _read(crop, config, psm=6, deadline=deadline)["text"].lower())
        # The company code is printed immediately beside a dense barcode and
        # is the least stable part of this band (TMSTH is often read as TVSTH,
        # MTVSTH, etc.).  The full English schedule title plus three separate
        # table anchors are a safer layout signature than requiring that one
        # noisy five-letter token.
        valid = ("motorinsuranceschedule" in anchors[0]
                 and "from" in anchors[4] and "to" in anchors[4]
                 and "chassis" in anchors[6] and "license" in anchors[6]
                 and "netpremium" in anchors[15] and "stampduty" in anchors[15] and "vat" in anchors[15])
        result["layout_checks"] = anchors
        if valid:
            result["layout"] = "tmsth_motor_schedule_v1"
            width = right-left
            def band(index, x1, y1, x2, y2):
                top, bottom = rows[index:index+2]
                return (int(left+width*x1), int(top+(bottom-top)*y1), int(left+width*x2), int(top+(bottom-top)*y2))
            regions = {
                # Stop above the barcode; bars touching the text baseline turn
                # printed hyphens into colons in the OCR result.
                "policy_number": band(0, .13, .34, .34, .58),
                "insured_name": band(1, .13, .02, .76, .38),
                "insured_address": band(1, .15, .52, .74, .97),
                "coverage_start": band(4, .34, .28, .46, .92),
                "coverage_end": band(4, .60, .28, .86, .92),
            }
            for field, cell in (("car_make",2), ("license_plate",3), ("chassis_no",4), ("car_year",5)):
                regions[field] = (columns[7][cell]+12, rows[7]+10, columns[7][cell+1]-10, rows[8]-20)
            for i, field in enumerate(MONEY_FIELDS):
                cell_left, cell_right = columns[16][i:i+2]
                # Leave room below the digits: Thai scans often have long comma
                # descenders. Clipping these converts thousands separators to dots.
                regions[field] = (cell_left+15, rows[16]+4, cell_right-15, rows[17]-3)
            for field, box in regions.items():
                language = "tha+eng" if field in ("insured_name", "insured_address", "license_plate", "coverage_start", "coverage_end") else "eng"
                prose = field in ("insured_name", "insured_address", "car_make")
                whitelist = {
                    "policy_number": "-c tessedit_char_whitelist=D0123456789-/",
                    "chassis_no": "-c tessedit_char_whitelist=ABCDEFGHJKLMNPRSTUVWXYZ0123456789",
                    "car_year": "-c tessedit_char_whitelist=0123456789",
                    "net_premium": "-c tessedit_char_whitelist=0123456789,.",
                    "stamp_duty": "-c tessedit_char_whitelist=0123456789,.",
                    "vat": "-c tessedit_char_whitelist=0123456789,.",
                    "total_premium": "-c tessedit_char_whitelist=0123456789,.",
                }.get(field, "")
                scales = (1, 2, 3) if field in ("policy_number", "chassis_no") else (1, 2)
                psm = 11 if field == "insured_name" else 6 if prose else 7
                candidate_boxes = [box]
                if field == "policy_number":
                    # Two print revisions place the identifier at different
                    # vertical offsets above the barcode.  Pick the crop that
                    # yields the most structurally valid reads.
                    candidate_boxes.append(band(0, .167, .44, .31, .65))
                    candidate_boxes.append(band(0, .165, .43, .30, .64))
                elif field == "insured_name":
                    candidate_boxes.append(band(1, .198, .06, .76, .32))
                elif field == "insured_address":
                    candidate_boxes.append(band(1, .198, .52, .74, .94))
                selected = None
                for candidate_box in candidate_boxes:
                    candidate_crop = deskewed.crop(candidate_box)
                    candidate_reads = [_read(candidate_crop, config, language, scale=scale, psm=psm,
                                             deadline=deadline, strip_rules=field in MONEY_FIELDS,
                                             extra_config=whitelist)
                                       for scale in scales]
                    candidate_values = [_normalize(field, read["text"]) for read in candidate_reads]
                    if field in ("insured_name", "insured_address"):
                        review_text = _clean_review_prose(field, candidate_reads[0]["text"])
                        score = len(re.sub(r"\s", "", review_text))
                        if field == "insured_name" and re.match(r"^(?:นาย|นาง|นางสาว|บริษัท|บมจ|หจก)", review_text):
                            score += 200
                        if field == "insured_address" and re.match(r"^\d", review_text):
                            score += 200
                    else:
                        score = sum(value is not None for value in candidate_values) * 1000
                    if selected is None or score > selected[0]:
                        selected = (score, candidate_box, candidate_crop, candidate_reads, candidate_values)
                _, box, crop, reads, normalised_reads = selected
                item = decide(field, reads)
                if field in ("insured_name", "insured_address"):
                    item["text"] = _clean_review_prose(field, reads[0]["text"])
                    if item["text"]:
                        item["manual_value"] = item["text"]
                elif field == "car_make" and reads[0]["text"].strip():
                    item["manual_value"] = reads[0]["text"].strip()
                if field in ("coverage_start", "coverage_end"):
                    # The 2x pass retains Thai month glyphs more reliably.  The
                    # value still requires both passes to normalize to one date.
                    item["text"] = re.sub(r"^[\s|:!.'_-]+", "", reads[-1]["text"]).strip()
                elif field == "policy_number" and normalised_reads[0] is not None and len(set(normalised_reads)) == 1:
                    # Three scale passes agreeing on a canonical identifier are
                    # stronger than TSV's minimum token confidence beside a barcode.
                    item.update(status="candidate", value=normalised_reads[0], text=normalised_reads[0],
                                manual_value=normalised_reads[0])
                elif field == "chassis_no":
                    valid = [value for value in normalised_reads if value is not None]
                    if valid:
                        checked = [value for value in valid if _vin_check_digit_valid(value)]
                        majority = checked[0] if len(set(checked)) == 1 else max(set(valid), key=valid.count)
                        if checked or valid.count(majority) >= 2:
                            # Keep VIN review-only when any pass disagrees, but
                            # show the majority reading for one-click confirmation.
                            item.update(text=majority, manual_value=majority)
                # Some scans include a small barcode/label beside the policy
                # number, which lowers the aggregate TSV confidence even when
                # both independent reads agree on the complete identifier.
                # Keep the global strict `decide` contract unchanged, but allow
                # this layout-scoped, regex-verified consensus at >=80%.
                if field == "policy_number" and item["status"] == "review" and item["min_confidence"] >= 80:
                    identifiers = []
                    for read in reads:
                        match = re.search(r"D[0O]-\d{2}-\d{2}/\d{6,8}", read["text"].upper())
                        identifiers.append(match.group(0) if match else None)
                    if identifiers[0] and identifiers[0] == identifiers[1]:
                        item.update(status="candidate", value=identifiers[0], manual_value=identifiers[0])
                item["box"] = list(box)
                item["label"] = LABELS[field]
                # Preserve the unprocessed crop for human verification. Never
                # display line-cleaned OCR input as if it were the original scan.
                buffer = io.BytesIO()
                crop.save(buffer, format="PNG")
                item["source_image_url"] = "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
                result["field_evidence"][field] = item
            validate_groups(result["field_evidence"])
            result["raw_text"] = _evidence_text(result["field_evidence"])
            return result
    # Full-page OCR on an unknown table is slow and mixes logos, rules and
    # unrelated columns into convincing-looking gibberish. Fail closed instead.
    result["raw_text"] = "ยังไม่รองรับรูปแบบเอกสารนี้ กรุณาดูภาพต้นฉบับและกรอกข้อมูลเอง"
    return result
