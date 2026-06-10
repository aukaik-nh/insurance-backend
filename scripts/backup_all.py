"""
backup_all.py
─────────────────────────────────────────────────────────────────────
Master backup script — รัน backup ครบทุกอย่างใน 1 คำสั่ง:
  1. DB (Neon) → .jsonl.gz
  2. PDFs (R2 + BabyPreechar) → folder ตามประเภทประกัน
  3. (optional) Google Drive upload

USAGE:
    python scripts/backup_all.py
    python scripts/backup_all.py --skip-babypreechar    # ข้าม mirror 4.7GB
    python scripts/backup_all.py --quick                 # DB only

ตั้ง schedule:
    .\scripts\setup_backup_schedule.ps1
"""
import sys, subprocess, argparse, time
from pathlib import Path
from datetime import datetime

if sys.stdout.encoding != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8")


def run(label: str, cmd: list[str]) -> int:
    print(f"\n{'#'*65}")
    print(f"# {label}")
    print(f"{'#'*65}")
    t0 = time.time()
    rc = subprocess.call(cmd)
    print(f"\n→ {label} เสร็จใน {time.time()-t0:.1f}s (rc={rc})")
    return rc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-babypreechar", action="store_true")
    ap.add_argument("--quick", action="store_true", help="DB เท่านั้น")
    args = ap.parse_args()

    py = sys.executable
    scripts = Path(__file__).resolve().parent
    fails = 0

    print(f"\n{'='*65}")
    print(f"  BACKUP ALL — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*65}")

    # 1. DB
    rc = run("1. DB BACKUP (Neon)", [py, str(scripts / "backup_neon.py")])
    fails += (rc != 0)

    if not args.quick:
        # 2. PDFs
        cmd = [py, str(scripts / "backup_pdfs.py")]
        if args.skip_babypreechar:
            cmd.append("--skip-babypreechar")
        rc = run("2. PDF BACKUP (R2 + BabyPreechar)", cmd)
        fails += (rc != 0)

    print(f"\n{'='*65}")
    if fails:
        print(f"  ⚠️  เสร็จแต่มี {fails} ขั้นตอนล้มเหลว")
        sys.exit(1)
    else:
        print(f"  ✅ Backup สำเร็จครบทุกขั้นตอน")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
