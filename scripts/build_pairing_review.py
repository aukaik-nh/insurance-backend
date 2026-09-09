"""Build an auditable, local-only pairing review for a batch of insurance PDFs.

This is deliberately a *review* stage.  It writes no database rows and uploads
nothing.  Each result contains the evidence and score that led to its suggested
pair, so a later import can commit only reviewed/high-confidence pairs.

Usage (PowerShell):
  $env:TESSERACT_CMD = 'C:\Program Files\Tesseract-OCR\tesseract.exe'
  $env:TESSDATA_PREFIX = 'C:\path\to\tessdata'
  python scripts/build_pairing_review.py <pdf-folder> <ocr-review.json>
"""
from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any


MOTOR_MAIN_TYPES = {"motor_main"}
MOTOR_PRB_TYPES = {"motor_prb"}
OCR_RELEVANT_TYPES = MOTOR_MAIN_TYPES | MOTOR_PRB_TYPES | {"unknown"}


def save_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def as_records(review: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for index, (filename, record) in enumerate(review.get("records", {}).items()):
        parsed = record.get("parsed") or {}
        result.append({
            "index": index,
            "filename": filename,
            "doc_type": parsed.get("doc_type") or "unknown",
            "coverage_start": parsed.get("coverage_start"),
            "coverage_end": parsed.get("coverage_end"),
            "policy_number": parsed.get("policy_number"),
            "company_code": parsed.get("company_code"),
            "initial_text": parsed.get("raw_text") or "",
        })
    return result


def ocr_sparse_first_page(pdf_path: Path, dpi: int) -> str:
    """Read layout-sparse text, which keeps schedule headings/IDs more intact."""
    import pymupdf
    import pytesseract
    from PIL import Image, ImageOps

    command = os.getenv("TESSERACT_CMD")
    if command:
        pytesseract.pytesseract.tesseract_cmd = command
    doc = pymupdf.open(pdf_path)
    try:
        page = doc[0]
        scale = dpi / 72
        pix = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale),
            colorspace=pymupdf.csGRAY,
            alpha=False,
        )
        image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
        # Mild autocontrast is safe for the faded scans in this batch.  Do not
        # threshold: it can erase Thai tone marks and document serial numbers.
        image = ImageOps.autocontrast(image, cutoff=1)
        return pytesseract.image_to_string(
            image, lang="tha+eng", config="--oem 1 --psm 11", timeout=70
        )
    finally:
        doc.close()


def normalized_policy_numbers(text: str) -> set[str]:
    # OCR commonly substitutes O/0 in Tokio Marine's D0 prefix.
    found = set()
    for value in re.findall(r"\bD[O0]-\d{2}-\d{2}\s*/\s*\d{4,8}\b", text.upper()):
        found.add(re.sub(r"\s+", "", value).replace("DO-", "D0-"))
    return found


def chassis_candidates(text: str) -> set[str]:
    compact = re.sub(r"[^A-Z0-9\n]", " ", text.upper())
    values = set()
    for value in re.findall(r"\b[A-Z0-9]{11,18}\b", compact):
        if any(char.isalpha() for char in value) and any(char.isdigit() for char in value):
            # Reject boilerplate company/tax ids; a VIN has at least 11 chars
            # and normally includes several letters after the first two chars.
            if sum(char.isalpha() for char in value) >= 3:
                values.add(value)
    return values


_STOPWORDS = {
    "tokio", "marine", "safety", "insurance", "thailand", "policy", "schedule",
    "the", "and", "for", "from", "with", "item", "company", "code", "motor",
    "vehicle", "insured", "coverage", "premium", "baht", "direct", "agent",
}


def identity_tokens(text: str) -> set[str]:
    """Tokens useful for comparing two schedule pages; generic boilerplate is removed."""
    values = re.findall(r"[A-Z0-9]{3,}|[ก-๙]{3,}", text.upper())
    return {value for value in values if value.lower() not in _STOPWORDS}


def classify(record: dict[str, Any]) -> str:
    text = record.get("sparse_text", "") + "\n" + record.get("initial_text", "")
    upper = text.upper()
    if "PROTECTION FOR VICTIMS" in upper or "ผู้ประสบภัยจากรถ" in text:
        return "motor_prb"
    if "THE MOTOR INSURANCE SCHEDULE" in upper or "ประกันภัยรถยนต์" in text:
        return "motor_main"
    return record["doc_type"]


def same_thai_year(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Pair the policy term's start year; PRB start day may legitimately differ."""
    def year(value: Any) -> str | None:
        value = str(value or "")
        return value[:4] if re.match(r"^\d{4}-", value) else None
    return bool(year(a.get("coverage_start")) and year(a.get("coverage_start")) == year(b.get("coverage_start")))


def score(main: dict[str, Any], prb: dict[str, Any]) -> tuple[int, list[str]]:
    reasons: list[str] = []
    value = 0
    shared_vins = main["vins"] & prb["vins"]
    if shared_vins:
        value += 80
        reasons.append("VIN ตรงกัน: " + ", ".join(sorted(shared_vins)))
    if same_thai_year(main, prb):
        value += 20
        reasons.append("ปีเริ่มคุ้มครองตรงกัน")
    distance = abs(main["index"] - prb["index"])
    if distance == 1:
        value += 18
        reasons.append("ไฟล์อยู่ติดกันในชุดที่รับเข้า")
    elif distance <= 3:
        value += 8
        reasons.append("ไฟล์อยู่ใกล้กันในชุดที่รับเข้า")
    overlap = main["tokens"] & prb["tokens"]
    # Numeric address/model tokens are useful corroboration, but capped so
    # boilerplate never becomes enough proof by itself.
    specific = {token for token in overlap if len(token) >= 5 or any(ch.isdigit() for ch in token)}
    if specific:
        value += min(22, 3 * len(specific))
        reasons.append("ข้อมูลผู้เอาประกัน/รถซ้ำกัน: " + ", ".join(sorted(specific)[:5]))
    return value, reasons


def build_pairs(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    mains = [record for record in records if record["final_type"] == "motor_main"]
    prbs = [record for record in records if record["final_type"] == "motor_prb"]
    pairs: list[dict[str, Any]] = []
    used_prbs: set[str] = set()
    for main in mains:
        candidates = []
        for prb in prbs:
            if prb["filename"] in used_prbs:
                continue
            candidate_score, reasons = score(main, prb)
            candidates.append((candidate_score, prb, reasons))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            continue
        best_score, best, reasons = candidates[0]
        runner_up = candidates[1][0] if len(candidates) > 1 else 0
        # Exact VIN is auto-ready.  Otherwise require adjacent source files,
        # same term year and at least one non-boilerplate corroborating token.
        auto_ready = best_score >= 80 or (
            best_score >= 44 and abs(main["index"] - best["index"]) == 1 and same_thai_year(main, best)
        )
        status = "ready_for_review" if auto_ready and best_score - runner_up >= 8 else "needs_manual_check"
        pairs.append({
            "status": status,
            "score": best_score,
            "runner_up_score": runner_up,
            "main_file": main["filename"],
            "prb_file": best["filename"],
            "main_policy_number_candidates": sorted(main["policy_candidates"]),
            "prb_policy_number_candidates": sorted(best["policy_candidates"]),
            "coverage_start_main": main.get("coverage_start"),
            "coverage_start_prb": best.get("coverage_start"),
            "evidence": reasons,
        })
        if status == "ready_for_review":
            used_prbs.add(best["filename"])
    paired = {entry["main_file"] for entry in pairs if entry["status"] == "ready_for_review"}
    paired |= {entry["prb_file"] for entry in pairs if entry["status"] == "ready_for_review"}
    unmatched = [record["filename"] for record in records if record["final_type"] in ("motor_main", "motor_prb") and record["filename"] not in paired]
    return pairs, unmatched


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf_folder", type=Path)
    parser.add_argument("ocr_review", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/pairing_review.json"))
    parser.add_argument("--dpi", type=int, default=180)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    review = json.loads(args.ocr_review.read_text(encoding="utf-8"))
    records = as_records(review)
    previous = {}
    if args.resume and args.output.exists():
        previous = json.loads(args.output.read_text(encoding="utf-8")).get("documents", {})

    documents: dict[str, Any] = {}
    for position, record in enumerate(records, 1):
        cached = previous.get(record["filename"])
        if cached and cached.get("sparse_text"):
            record.update(cached)
        elif record["doc_type"] in OCR_RELEVANT_TYPES:
            pdf = args.pdf_folder / record["filename"]
            record["sparse_text"] = ocr_sparse_first_page(pdf, args.dpi)
        else:
            record["sparse_text"] = ""
        text = record["sparse_text"] + "\n" + record["initial_text"]
        record["final_type"] = classify(record)
        record["policy_candidates"] = normalized_policy_numbers(text)
        record["vins"] = chassis_candidates(text)
        record["tokens"] = identity_tokens(text)
        documents[record["filename"]] = {
            **{key: record[key] for key in ("index", "filename", "doc_type", "final_type", "coverage_start", "coverage_end", "company_code", "sparse_text")},
            "policy_candidates": sorted(record["policy_candidates"]),
            "vins": sorted(record["vins"]),
        }
        # Save a resumable result after every PDF; OCR can take several minutes.
        partial = {"documents": documents, "summary": {"processed": position, "total": len(records)}}
        save_json(args.output, partial)
        print(f"{position}/{len(records)} {record['filename']} -> {record['final_type']}", flush=True)

    pairs, unmatched = build_pairs(records)
    summary = Counter(entry["status"] for entry in pairs)
    save_json(args.output, {
        "source_folder": str(args.pdf_folder),
        "documents": documents,
        "pairs": pairs,
        "unmatched_motor_documents": unmatched,
        "summary": {"documents": len(records), **summary, "unmatched_motor_documents": len(unmatched)},
        "safety": "No upload or database write was performed. Only ready_for_review pairs may enter the import stage.",
    })
    print(json.dumps({"pairs": dict(summary), "unmatched": len(unmatched)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

