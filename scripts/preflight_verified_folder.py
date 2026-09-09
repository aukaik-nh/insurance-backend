"""Read a folder of scanned insurance PDFs and build a safe import manifest.

This command NEVER writes to the database or R2.  It parses every valid PDF,
classifies documents, pairs motor policies with compulsory insurance, and
attaches endorsements/credit notes only when an exact unique anchor exists.

Usage:
  .venv\Scripts\python.exe scripts\preflight_verified_folder.py \
      --folder "C:\path\to\pdfs" --resume
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from services.doc_pairing import (  # noqa: E402
    CREDIT_NOTE,
    ENDORSEMENT,
    FIRE,
    MOTOR_MAIN,
    MOTOR_PRB,
    SME_PROPERTY,
    UNKNOWN,
    dedupe,
    norm_chassis,
    norm_name,
    pair_documents,
)
from services.filename_matcher_v2 import coverage_year_ad, norm_plate  # noqa: E402
from services.gemini_parser import parse_with_gemini  # noqa: E402

REPORT_PATH = ROOT / "preflight_folder_report.json"
ATTACHMENT_TYPES = {ENDORSEMENT, CREDIT_NOTE}


def _load_report(path: Path) -> dict:
    if not path.exists():
        return {"version": 1, "records": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "records": []}


def _save_report(path: Path, report: dict) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def _page_count(path: Path) -> int:
    try:
        import fitz

        doc = fitz.open(path)
        try:
            return doc.page_count
        finally:
            doc.close()
    except Exception:
        return -1


def _record_key(record: dict) -> str:
    return str(record.get("source_path") or "")


def _candidate_anchors(main: dict, prb: dict | None = None) -> dict:
    records = [x for x in (main, prb) if x]
    return {
        "policy_numbers": {str(x.get("policy_number") or "").strip().upper() for x in records if x.get("policy_number")},
        "chassis": {norm_chassis(x.get("chassis_no")) for x in records if norm_chassis(x.get("chassis_no"))},
        "plates": {norm_plate(x.get("license_plate")) for x in records if norm_plate(x.get("license_plate"))},
        "names": {norm_name(x.get("insured_name")) for x in records if norm_name(x.get("insured_name"))},
        "years": {coverage_year_ad(x) for x in records if coverage_year_ad(x)},
    }


def _attachment_match(attachment: dict, candidates: list[dict]) -> tuple[dict | None, str]:
    """Return one parent only when the evidence is exact and unique."""
    pol = str(attachment.get("policy_number") or "").strip().upper()
    chassis = norm_chassis(attachment.get("chassis_no"))
    plate = norm_plate(attachment.get("license_plate"))
    name = norm_name(attachment.get("insured_name"))
    year = coverage_year_ad(attachment)

    exact_policy = [c for c in candidates if pol and pol in c["anchors"]["policy_numbers"]]
    if len(exact_policy) == 1:
        return exact_policy[0], "เลขกรมธรรม์แม่ตรง"
    if len(exact_policy) > 1:
        return None, "เลขกรมธรรม์ตรงมากกว่าหนึ่งรายการ"

    exact_chassis = [c for c in candidates if chassis and chassis in c["anchors"]["chassis"]]
    if len(exact_chassis) == 1:
        return exact_chassis[0], "เลขตัวถังตรง"
    if len(exact_chassis) > 1:
        return None, "เลขตัวถังตรงมากกว่าหนึ่งรายการ"

    exact_plate = [
        c for c in candidates
        if plate and plate in c["anchors"]["plates"]
        and (not year or not c["anchors"]["years"] or year in c["anchors"]["years"])
        and (not name or not c["anchors"]["names"] or name in c["anchors"]["names"])
    ]
    if len(exact_plate) == 1:
        return exact_plate[0], "ทะเบียนตรงร่วมกับปี/ชื่อ"
    return None, "ไม่พบหลักฐานเอกลักษณ์ที่เพียงพอ"


def _build_import_plan(records: list[dict]) -> dict:
    valid = [r for r in records if not r.get("preflight_error")]
    unique, duplicates = dedupe(valid)
    paired = pair_documents(unique)

    candidates: list[dict] = []
    for index, pair in enumerate(paired.get("pairs", [])):
        candidate = {
            "key": f"pair-{index}",
            "main": pair["main"],
            "prb": pair["prb"],
            "pair_status": pair["status"],
            "pair_score": pair["score"],
            "pair_reasons": pair["reasons"],
            "attachments": [],
        }
        candidate["anchors"] = _candidate_anchors(candidate["main"], candidate["prb"])
        candidates.append(candidate)

    for index, main in enumerate(paired.get("orphan_main", [])):
        candidate = {
            "key": f"main-{index}", "main": main, "prb": None,
            "pair_status": "standalone", "pair_score": None,
            "pair_reasons": [], "attachments": [],
        }
        candidate["anchors"] = _candidate_anchors(main)
        candidates.append(candidate)

    # Fire and SME are valid standalone policies. Unknown documents are held.
    standalone_other = []
    attachments = []
    held = []
    for record in paired.get("others", []):
        doc_type = record.get("doc_type")
        if doc_type in ATTACHMENT_TYPES:
            attachments.append(record)
        elif doc_type in {FIRE, SME_PROPERTY}:
            standalone_other.append(record)
        else:
            held.append(record)

    attachment_review = []
    for attachment in attachments:
        parent, reason = _attachment_match(attachment, candidates)
        if parent:
            parent["attachments"].append({"record": attachment, "match_reason": reason})
        else:
            attachment_review.append({"record": attachment, "reason": reason})

    # JSON cannot serialize sets used internally as anchors.
    for candidate in candidates:
        candidate.pop("anchors", None)

    safe_pairs = [c for c in candidates if c["pair_status"] == "auto"]
    review_pairs = [c for c in candidates if c["pair_status"] == "review"]
    standalone_main = [c for c in candidates if c["pair_status"] == "standalone"]

    return {
        "safe_pairs": safe_pairs,
        "review_pairs": review_pairs,
        "standalone_main": standalone_main,
        "standalone_other": standalone_other,
        "orphan_prb": paired.get("orphan_prb", []),
        "attachment_review": attachment_review,
        "held": held,
        "duplicates": duplicates,
        "summary": {
            "files": len(records),
            "valid": len(valid),
            "invalid": len(records) - len(valid),
            "safe_pairs": len(safe_pairs),
            "review_pairs": len(review_pairs),
            "standalone_main": len(standalone_main),
            "standalone_other": len(standalone_other),
            "orphan_prb": len(paired.get("orphan_prb", [])),
            "attached_endorsements": sum(len(c["attachments"]) for c in candidates),
            "attachment_review": len(attachment_review),
            "held": len(held),
            "duplicates": len(duplicates),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--report", default=str(REPORT_PATH))
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    args = parser.parse_args()

    folder = Path(args.folder).resolve()
    report_path = Path(args.report).resolve()
    if not folder.is_dir():
        print("folder not found")
        return 2
    if not os.getenv("GEMINI_API_KEY"):
        print("GEMINI_API_KEY is not configured")
        return 2

    pdfs = sorted({*folder.rglob("*.pdf"), *folder.rglob("*.PDF")}, key=lambda p: p.name)
    if args.limit:
        pdfs = pdfs[:args.limit]

    report = _load_report(report_path) if args.resume else {"version": 1, "records": []}
    cached = {_record_key(r): r for r in report.get("records", [])}
    records = []

    print(f"Preflight: {len(pdfs)} PDFs (database/R2 writes disabled)")
    for index, pdf in enumerate(pdfs, 1):
        key = str(pdf)
        if args.resume and key in cached:
            records.append(cached[key])
            print(f"[{index}/{len(pdfs)}] cached")
            continue

        pages = _page_count(pdf)
        base = {
            "source_path": key,
            "orig_filename": pdf.name,
            "file_id": hashlib.sha256(key.encode("utf-8")).hexdigest()[:12],
            "sha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
            "size": pdf.stat().st_size,
            "page_count": pages,
        }
        if pages <= 0:
            base["preflight_error"] = "PDF ไม่มีหน้าที่อ่านได้หรือโครงสร้างเสีย"
            records.append(base)
            print(f"[{index}/{len(pdfs)}] invalid PDF")
        else:
            try:
                # Suppress parser field logs because the files contain customer PII.
                with contextlib.redirect_stdout(io.StringIO()):
                    parsed = parse_with_gemini(pdf.read_bytes(), filename=pdf.name) or {}
                base.update(parsed)
                if not any(parsed.get(k) for k in ("doc_type", "policy_number", "insured_name", "chassis_no", "license_plate")):
                    base["preflight_error"] = "AI ไม่พบข้อมูลระบุตัวเอกสาร"
                records.append(base)
                print(f"[{index}/{len(pdfs)}] parsed")
            except Exception as error:
                base["preflight_error"] = f"{type(error).__name__}: {str(error)[:160]}"
                records.append(base)
                print(f"[{index}/{len(pdfs)}] parse failed")

        report.update({"folder": str(folder), "records": records})
        _save_report(report_path, report)
        if args.delay:
            time.sleep(args.delay)

    report["plan"] = _build_import_plan(records)
    _save_report(report_path, report)
    print("SUMMARY " + json.dumps(report["plan"]["summary"], ensure_ascii=False))
    print(f"REPORT {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

