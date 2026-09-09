"""AI-read a folder of scanned insurance PDFs into a resumable local manifest.

Read-only: this script does not write to the database or R2.
"""
import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from services.gemini_parser import parse_with_gemini


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--folder", required=True)
    parser.add_argument("--output", default=str(ROOT / "tmp" / "named_pdf_analysis.json"))
    args = parser.parse_args()

    folder = Path(args.folder)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        rows = json.loads(output.read_text(encoding="utf-8"))
    else:
        rows = []
    done = {r["filename"] for r in rows if r.get("status") == "ok"}

    pdfs = sorted(folder.glob("*.pdf"))
    for idx, path in enumerate(pdfs, 1):
        if path.name in done:
            print(f"[{idx}/{len(pdfs)}] SKIP {path.name}", flush=True)
            continue
        print(f"[{idx}/{len(pdfs)}] READ {path.name}", flush=True)
        try:
            parsed = parse_with_gemini(path.read_bytes(), filename=path.name)
            row = {"filename": path.name, "path": str(path), "size": path.stat().st_size,
                   "status": "ok", "parsed": parsed}
        except Exception as exc:
            row = {"filename": path.name, "path": str(path), "size": path.stat().st_size,
                   "status": "error", "error": str(exc)[:500]}
        rows = [r for r in rows if r.get("filename") != path.name]
        rows.append(row)
        output.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"DONE {len(rows)} -> {output}")


if __name__ == "__main__":
    main()

