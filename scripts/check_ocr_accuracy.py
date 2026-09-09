"""Benchmark a local PDF against human-checked truth; never contacts a service.

Usage: python scripts/check_ocr_accuracy.py document.pdf --expected truth.json
       --output output/ocr_accuracy/report.json [--compare-legacy]
Truth and generated reports can contain personal data. Do not commit them.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from services.local_pdf_parser import parse_pdf_image_locally, _parse_text, _ocr_text


def score(parsed, expected):
    items = []
    for key, correct in expected.items():
        actual = parsed.get(key)
        status = "withheld" if actual in (None, "") else "correct" if actual == correct else "incorrect"
        items.append({"field": key, "expected": correct, "actual": actual, "status": status})
    return {"counts": {s: sum(item["status"] == s for item in items) for s in ("correct", "incorrect", "withheld")}, "fields": items}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("--expected", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare-legacy", action="store_true")
    args = parser.parse_args()
    blob = args.pdf.read_bytes()
    expected = json.loads(args.expected.read_text(encoding="utf-8"))
    report = {"input_sha256": hashlib.sha256(blob).hexdigest(), "truth_fields": len(expected),
              "python_executable": sys.executable, "python_version": sys.version.split()[0]}
    start = time.monotonic()
    parsed = parse_pdf_image_locally(blob, args.pdf.name)
    if parsed.get("parse_error"):
        raise RuntimeError(parsed["parse_error"])
    report["current"] = {**score(parsed, expected), "seconds": round(time.monotonic()-start, 2), "layout": parsed.get("layout")}
    if args.compare_legacy:
        start = time.monotonic()
        legacy = _parse_text(_ocr_text(blob, max_pages_override=1), "legacy_benchmark")
        report["legacy"] = {**score(legacy, expected), "seconds": round(time.monotonic()-start, 2)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    # Include cropped-source evidence locally for an audit, not in the report.
    import base64
    for key, item in parsed.get("field_evidence", {}).items():
        (args.output.parent / f"{key}.png").write_bytes(base64.b64decode(item["source_image_url"].split(",", 1)[1]))
    (args.output.parent / "read-text.txt").write_text(parsed["raw_text"], encoding="utf-8")
    for mode in ("current", "legacy"):
        if mode in report:
            print(mode, report[mode]["counts"], report[mode]["seconds"], "seconds")
    if report["current"]["counts"]["incorrect"]:
        raise SystemExit("FAIL: at least one automatic field is incorrect")


if __name__ == "__main__":
    main()
