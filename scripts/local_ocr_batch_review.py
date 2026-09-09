"""Create a resumable review file for a folder of scanned insurance PDFs.

No network call and no database write occurs here.  The output has one row per
PDF, its local-OCR candidate and the legacy-compatible name that would be used
only after duplicate checks against the production system.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services.local_pdf_parser import parse_pdf_locally


def atomic_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def storage_doc_type(doc_type: str | None) -> str:
    if doc_type == "motor_prb":
        return "prb"
    if doc_type == "endorsement":
        return "endorsement"
    return "main"


def make_display_filename(parsed: dict, document_type: str) -> str:
    """Same legacy convention as the API, kept dependency-free for local OCR."""
    start = str(parsed.get("coverage_start") or parsed.get("coverage_end") or "")
    year = (int(start[:4]) + 543) % 100 if re.match(r"^\d{4}-", start) else None
    plate = re.sub(r"\s+", "", str(parsed.get("license_plate") or "").strip())
    address = str(parsed.get("insured_address") or "").split("\n", 1)[0].strip()[:40]
    name = str(parsed.get("insured_name") or "").strip()
    policy_type = str(parsed.get("policy_type") or "").upper().strip()
    if document_type == "prb":
        identity, label = plate, "พรบ"
    elif policy_type in {"FIRE", "ASSET", "IAR", "BURGLAR"}:
        identity, label = address or plate or name, "กธ"
    elif policy_type in {"PA", "TA", "3RD", "PUBLIC", "MISC", "GOLF", "MARINE"}:
        identity, label = name or plate or address, "กธ"
    else:
        identity, label = plate or name or address, "สลักหลัง" if document_type == "endorsement" else "กธ"
    identity = identity or "ไม่ทราบ"
    return f"{identity} {label}.{year:02d}.pdf" if year is not None else f"{identity} {label}.pdf"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/local_ocr_review.json"))
    args = parser.parse_args()

    state = {"source": str(args.source), "records": {}, "errors": {}}
    if args.output.exists():
        state = json.loads(args.output.read_text(encoding="utf-8"))
    records: dict = state.setdefault("records", {})
    errors: dict = state.setdefault("errors", {})
    files = sorted(args.source.glob("*.pdf"))

    for index, pdf in enumerate(files, 1):
        if pdf.name in records:
            print(f"SKIP {index}/{len(files)} {pdf.name}", flush=True)
            continue
        try:
            parsed = parse_pdf_locally(pdf.read_bytes(), pdf.name)
            dtype = storage_doc_type(parsed.get("doc_type"))
            proposed = make_display_filename(parsed, dtype)
            records[pdf.name] = {
                "source_file": pdf.name,
                "source_size": pdf.stat().st_size,
                "parsed": parsed,
                "proposed_filename": proposed,
                "status": "needs_duplicate_check",
            }
            errors.pop(pdf.name, None)
            print(
                f"OK {index}/{len(files)} {parsed.get('doc_type')} "
                f"plate={parsed.get('license_plate')} end={parsed.get('coverage_end')} "
                f"name={proposed}",
                flush=True,
            )
        except Exception as exc:
            errors[pdf.name] = str(exc)[:300]
            print(f"ERROR {index}/{len(files)} {pdf.name}: {exc}", flush=True)
        atomic_json(args.output, state)

    state["summary"] = {"total": len(files), "parsed": len(records), "errors": len(errors)}
    atomic_json(args.output, state)
    print(json.dumps(state["summary"], ensure_ascii=False), flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
