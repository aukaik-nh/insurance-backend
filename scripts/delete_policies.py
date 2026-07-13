"""
delete_policies.py — delete rows from insurance_policies + their R2 files.
Also removes the original pdf_path from batch_import_neon_log.json done list.

Usage:
    python delete_policies.py <id1> [<id2> ...] [--pdf-key-from paths.txt]

  --pdf-key-from FILE : optional list of pdf_paths (one per line) whose full
                        paths to remove from batch_import_neon_log.json "done".
"""
import os, sys, io, json, argparse
from pathlib import Path
from urllib.parse import urlparse, unquote

if sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

BACKEND_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_ROOT))

from dotenv import load_dotenv
load_dotenv(BACKEND_ROOT / ".env")

from services.supabase_shim import create_client

LOG_FILE = BACKEND_ROOT / "scripts" / "batch_import_neon_log.json"


def _r2_key_from_url(url: str) -> str | None:
    """Extract the object key from a public R2 URL.
    R2_PUBLIC_URL is a prefix; strip it, or fall back to the URL path."""
    if not url:
        return None
    base = (os.getenv("R2_PUBLIC_URL") or "").rstrip("/")
    if base and url.startswith(base + "/"):
        return unquote(url[len(base) + 1:])
    # fallback: strip leading '/'
    return unquote(urlparse(url).path.lstrip("/"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+")
    ap.add_argument("--pdf-key-from")
    args = ap.parse_args()

    sb = create_client(None, None)

    # 1) query rows first to get pdf_url
    print(f"looking up {len(args.ids)} rows...")
    rows = []
    for pid in args.ids:
        r = sb.table("insurance_policies").select("id,pdf_url,pdf_filename,policy_number").eq("id", pid).execute()
        if r.data:
            rows.append(r.data[0])
        else:
            print(f"  NOT FOUND {pid}")

    # 2) delete R2 files
    r2_keys = []
    for row in rows:
        k = _r2_key_from_url(row.get("pdf_url") or "")
        if k:
            r2_keys.append(k)

    if r2_keys:
        print(f"deleting {len(r2_keys)} R2 files...")
        try:
            result = sb.storage.from_("policy-pdfs").remove(r2_keys)
            print(f"  R2 removed: {result}")
        except Exception as e:
            print(f"  R2 delete WARN: {e}")

    # 3) delete DB rows
    for row in rows:
        try:
            sb.table("insurance_policies").delete().eq("id", row["id"]).execute()
            print(f"  DB deleted {row['id']} policy={row.get('policy_number') or '?'}")
        except Exception as e:
            print(f"  DB delete FAIL {row['id']}: {e}")

    # 4) update log — remove from "done"
    if args.pdf_key_from:
        keys_to_remove = set(
            Path(args.pdf_key_from).read_text(encoding="utf-8").splitlines()
        )
        keys_to_remove.discard("")
        try:
            log = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log = {"done": [], "failed": [], "skipped": []}
        before = len(log.get("done", []))
        log["done"] = [d for d in log["done"] if d not in keys_to_remove]
        after = len(log["done"])
        LOG_FILE.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"log done: {before} → {after}")

    print("done.")


if __name__ == "__main__":
    main()
