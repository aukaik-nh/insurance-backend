"""Prepare a resumable, human-verified import package from the ChatGPT match manifest.

The deployed backend is used only as a PDF data extractor. Pairing is never taken
from that service: main/PRB relationships and critical identifiers come from the
ChatGPT-reviewed manifest. No PDF is uploaded to R2 and no database row is written
by this script.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "output" / "chatgpt_verified_match_manifest.json"
DEFAULT_OUTPUT = ROOT / "output" / "chatgpt_verified_extracted_data.json"
DEFAULT_API = "https://insurance-backend-c2s2.onrender.com/api"


def load_json(path: Path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def login(session: requests.Session, api: str, username: str, password: str) -> str:
    response = session.post(
        f"{api}/auth/login",
        json={"username": username, "password": password, "remember": False},
        timeout=90,
    )
    response.raise_for_status()
    token = response.json().get("token")
    if not token:
        raise RuntimeError("Backend login succeeded without a token")
    return token


def preview(
    session: requests.Session, api: str, token: str, pdf_path: Path, retries: int = 3
) -> dict:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with pdf_path.open("rb") as handle:
                response = session.post(
                    f"{api}/preview-pdf",
                    headers={"Authorization": f"Bearer {token}"},
                    files={"file": (pdf_path.name, handle, "application/pdf")},
                    timeout=240,
                )
            response.raise_for_status()
            body = response.json()
            if not body.get("success"):
                raise RuntimeError(str(body)[:300])
            return body.get("parsed") or {}
        except Exception as exc:  # network/free-tier wakeups are retriable
            last_error = exc
            if attempt < retries:
                time.sleep(3 * attempt)
    raise RuntimeError(f"preview failed: {last_error}")


def expected_files(manifest: dict, kind: str) -> list[dict]:
    rows: list[dict] = []
    if kind in {"main", "all"}:
        for pair in manifest.get("exact_pairs", []):
            rows.append(
                {
                    "role": "main",
                    "file": pair["main_file"],
                    "expected_policy_number": pair["main_policy_number"],
                    "expected_chassis_no": pair["chassis_no"],
                    "pair_id": pair["pair_id"],
                }
            )
        for item in manifest.get("standalone_policies", []):
            rows.append(
                {
                    "role": "standalone",
                    "file": item["file"],
                    "expected_policy_number": item["policy_number"],
                    "expected_doc_type": item["doc_type"],
                    "note": item.get("note"),
                }
            )
    if kind in {"prb", "all"}:
        for pair in manifest.get("exact_pairs", []):
            rows.append(
                {
                    "role": "prb",
                    "file": pair["prb_file"],
                    "expected_policy_number": pair["prb_policy_number"],
                    "expected_chassis_no": pair["chassis_no"],
                    "pair_id": pair["pair_id"],
                }
            )
    return rows


def normalize_identifier(value) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--api", default=os.getenv("IMPORT_API_URL", DEFAULT_API))
    parser.add_argument("--kind", choices=("main", "prb", "all"), default="all")
    args = parser.parse_args()

    username = os.getenv("IMPORT_API_USERNAME", "").strip()
    password = os.getenv("IMPORT_API_PASSWORD", "")
    if not username or not password:
        raise SystemExit("Set IMPORT_API_USERNAME and IMPORT_API_PASSWORD for this process")

    manifest = load_json(args.manifest, None)
    if not manifest:
        raise SystemExit(f"Manifest not found: {args.manifest}")
    source = Path(manifest["source_folder"])
    state = load_json(args.output, {"version": 1, "records": {}, "errors": {}})
    records = state.setdefault("records", {})
    errors = state.setdefault("errors", {})

    session = requests.Session()
    token = login(session, args.api.rstrip("/"), username, password)
    queue = expected_files(manifest, args.kind)
    print(f"PREPARE count={len(queue)} resume={len(records)}", flush=True)

    for index, item in enumerate(queue, 1):
        filename = item["file"]
        if filename in records:
            print(f"SKIP {index}/{len(queue)} {filename}", flush=True)
            continue
        pdf_path = source / filename
        if not pdf_path.is_file():
            errors[filename] = "source file not found"
            atomic_json(args.output, state)
            print(f"ERROR {index}/{len(queue)} missing {filename}", flush=True)
            continue
        try:
            parsed = preview(session, args.api.rstrip("/"), token, pdf_path)
            expected_policy = item.get("expected_policy_number")
            expected_chassis = item.get("expected_chassis_no")
            policy_ok = normalize_identifier(parsed.get("policy_number")) == normalize_identifier(expected_policy)
            chassis_ok = not expected_chassis or (
                normalize_identifier(parsed.get("chassis_no")) == normalize_identifier(expected_chassis)
            )
            records[filename] = {
                **item,
                "source_size": pdf_path.stat().st_size,
                "parsed": parsed,
                "checks": {
                    "policy_number_exact": policy_ok,
                    "chassis_no_exact": chassis_ok,
                },
                "ready_for_human_review": policy_ok and chassis_ok,
            }
            errors.pop(filename, None)
            atomic_json(args.output, state)
            print(
                f"OK {index}/{len(queue)} policy={parsed.get('policy_number')} "
                f"policy_match={policy_ok} chassis_match={chassis_ok} file={filename}",
                flush=True,
            )
        except Exception as exc:
            errors[filename] = str(exc)[:500]
            atomic_json(args.output, state)
            print(f"ERROR {index}/{len(queue)} {filename}: {exc}", flush=True)

    state["summary"] = {
        "records": len(records),
        "errors": len(errors),
        "ready": sum(bool(row.get("ready_for_human_review")) for row in records.values()),
    }
    atomic_json(args.output, state)
    print(json.dumps(state["summary"], ensure_ascii=False), flush=True)
    return 0 if not errors else 2


if __name__ == "__main__":
    sys.exit(main())

