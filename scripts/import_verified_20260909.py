"""Import the visually verified 2026-09-09 PDF folders.

The script is intentionally specific to this reviewed batch.  It uploads renewal
notices and previously missing companion documents as attachments, creates two
minimal parent rows whose old policies are absent from the database, and applies
only high-confidence motor sum-insured corrections confirmed from the schedules.

Dry-run is the default.  Pass --commit to write to R2 and Neon.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

import boto3
import psycopg2
import psycopg2.extras
from botocore.client import Config
from dotenv import load_dotenv


ROOT = Path(__file__).resolve().parents[1]
WORK = ROOT.parent
FIRST = WORK / "2569-08-21-renamed"
SECOND = WORK / "renamed-pdfs-ocr"
REPORT = ROOT / "output" / "import-20260909" / "commit-report.json"

RENEWAL_POLICY_NUMBERS = [
    "D0-70-68/034882", "D0-70-68/031107", "D0-70-68/030547", "D0-70-68/030543",
    "D0-70-68/033313", "D0-70-68/031060", "D0-70-68/033787", "D0-70-68/031641",
    "D0-70-68/030545", "D0-70-68/031970", "D0-70-68/032772", "D0-70-68/033790",
    "D0-70-68/033855", "D0-70-68/031059", "D0-70-68/031570", "D0-70-68/031571",
    "D0-70-68/031967", "D0-70-68/032087", "D0-70-68/034878", "D0-70-68/034154",
    "D0-70-68/033785", "D0-70-68/030548", "D0-70-68/031574", "D0-70-68/033856",
    "D0-70-68/032085", "D0-70-68/032078", "D0-70-68/032086", "D0-70-68/032082",
    "D0-70-68/032074", "D0-70-68/032076", "D0-70-68/030321", "D0-70-68/031572",
    "D0-70-68/030283", "D0-70-68/031573", "D0-70-68/032067", "D0-70-68/034863",
    "D0-70-68/033880", "D0-70-68/033897", "D0-70-68/033974", "D0-70-68/033918",
    "D0-70-68/033923", "D0-70-68/033930", "D0-70-68/033930", "D0-70-68/033933",
    "D0-70-68/033968", "D0-70-68/033951", "D0-70-68/033861", "D0-70-68/033873",
    "D0-70-68/033878", "D0-70-68/033926",
]

# Exact fields read from the two renewal notices whose old motor rows are absent.
MISSING_PARENTS = {
    "D0-70-68/031107": {
        "company_code": "TMSTH", "insured_name": "นาง พรรคศุลี โตเพ็ง",
        "license_plate": "ชจ 1159 กท", "chassis_no": "WDB2010182G001808",
        "license_province": "กรุงเทพมหานคร",
        "car_make": "MERCEDES-BENZ", "car_model": "190E", "car_year": 1994,
        "coverage_end": "2026-11-10", "policy_type": "M",
    },
    "D0-70-68/033790": {
        "company_code": "TMSTH", "insured_name": "นางสาว เปรมปภัสร์ พึ่งภพ",
        "license_plate": "กอ 2642 นบ", "chassis_no": "L80-8400046",
        "license_province": "นนทบุรี",
        "car_make": "DAIHATSU", "car_model": "MIRA", "car_year": 1998,
        "coverage_end": "2026-11-11", "policy_type": "M",
    },
    "D0-18-69/001162": {
        "company_code": "TMSTH", "policy_type": "FIRE", "sum_insured": 2500000,
    },
}

# Files not already present in R2/DB from renamed-pdfs-ocr.  Index 55 is a
# structurally empty zero-page PDF and is deliberately held outside this list.
EXTRA_ATTACHMENTS = [
    (86, "D0-72-69/010160", "other", "ใบคืนเบี้ยและสลักหลัง พ.ร.บ. 69-003224-001",
     "เอกสาร 4 หน้า: ใบคืนเบี้ยและสลักหลังยกเลิก พ.ร.บ.; ตรวจจากภาพทุกหน้า",
     -593.13, -3.00, -41.52, -634.65),
    (88, "D0-72-69/010160", "endorsement", "สลักหลัง พ.ร.บ. 69-003224-001",
     "สำเนาสลักหลังของรายการคืนเบี้ยเดียวกับเอกสาร 4 หน้า; ไม่ลงยอดซ้ำ",
     None, None, None, None),
    (92, "D0-70-69/024767", "other", "สำเนากรมธรรม์เพิ่มเติม D0-70-69/024767",
     "กรมธรรม์ปี 2569 เลขและเลขตัวถังตรงกับรายการหลัก; เก็บเป็นสำเนาแนบ",
     None, None, None, None),
    (102, "D0-70-69/023976", "other", "สำเนากรมธรรม์เพิ่มเติม D0-70-69/023976",
     "กรมธรรม์ปี 2569 เลขและเลขตัวถังตรงกับรายการหลัก; เก็บเป็นสำเนาแนบ",
     None, None, None, None),
    (106, "D0-36-69/001173", "other", "สำเนากรมธรรม์เพิ่มเติม D0-36-69/001173",
     "กรมธรรม์ทรัพย์สินปี 2569 เลขกรมธรรม์ตรงกับรายการหลัก; เก็บเป็นสำเนาแนบ",
     None, None, None, None),
    (107, "D0-70-69/022848", "endorsement", "สลักหลัง 69-008616-001",
     "สลักหลังปี 2569 ระบุกรมธรรม์แม่ D0-70-69/022848 ชัดเจน",
     None, None, None, None),
]

# The schedule explicitly distinguishes car price from own-damage cover.  These
# are the rows where the importer stored car price in sum_insured.
SUM_INSURED_CORRECTIONS = {
    "D0-70-69/023503": 200000, "D0-70-69/023506": 50000,
    "D0-70-69/024589": 50000, "D0-70-69/025200": 100000,
    "D0-70-69/023990": 100000, "D0-70-69/023991": 100000,
    "D0-70-69/023992": 200000,
    "D0-70-69/024948": 480000, "D0-70-69/024631": 650000,
    "D0-70-69/024917": 100000, "D0-70-69/023969": 190000,
    "D0-70-69/023515": 260000, "726-01331-78374": 340000,
}


def norm(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def load_inventory() -> list[dict]:
    path = ROOT / "output" / "import-20260909" / "inventory.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if len(rows) != 118:
        raise RuntimeError(f"expected 118 inventory rows, found {len(rows)}")
    return rows


def db_url() -> str:
    value = os.getenv("NEON_URL") or os.getenv("DATABASE_URL")
    if not value:
        raise RuntimeError("NEON_URL/DATABASE_URL is missing")
    return value


def storage_client():
    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY"],
        aws_secret_access_key=os.environ["R2_SECRET_KEY"],
        config=Config(signature_version="s3v4"), region_name="auto",
    )


def upload_pdf(s3, path: Path, expected_sha: str) -> tuple[str, int]:
    content = path.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha:
        raise RuntimeError(f"source changed after review: {path}")
    clean_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", path.name).strip()
    key = f"policies/verified-20260909/{digest[:16]}_{clean_name}"
    bucket = os.getenv("R2_BUCKET", "insurance-pdfs")
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except Exception:
        s3.put_object(Bucket=bucket, Key=key, Body=content, ContentType="application/pdf")
    public = os.environ["R2_PUBLIC_URL"].rstrip("/")
    return f"{public}/{quote(key, safe='/')}", len(content)


def find_policy(cur, number: str):
    cur.execute(
        "select * from insurance_policies where regexp_replace(upper(policy_number), '[^A-Z0-9]', '', 'g')=%s order by created_at desc",
        (norm(number),),
    )
    rows = cur.fetchall()
    if len(rows) != 1:
        raise RuntimeError(f"expected one policy for {number}, found {len(rows)}")
    return rows[0]


def ensure_missing_parent(cur, number: str, values: dict, commit: bool):
    cur.execute(
        "select * from insurance_policies where regexp_replace(upper(policy_number), '[^A-Z0-9]', '', 'g')=%s",
        (norm(number),),
    )
    rows = cur.fetchall()
    if rows:
        if len(rows) != 1:
            raise RuntimeError(f"ambiguous parent {number}")
        return rows[0], False
    if not commit:
        return {"id": f"DRY-{norm(number)}", "policy_number": number, **values}, True
    payload = {
        "policy_number": number, **values, "manually_edited": True,
        "keyby": "Codex verified PDF import 2026-09-09",
        "notes": "สร้างรายการอ้างอิงจากใบแจ้งเตือนต่ออายุ; ไม่บันทึกเบี้ยเสนอเป็นเบี้ยกรมธรรม์เดิม",
    }
    columns = list(payload)
    cur.execute(
        f"insert into insurance_policies ({','.join(columns)}) values ({','.join(['%s'] * len(columns))}) returning *",
        [payload[column] for column in columns],
    )
    return cur.fetchone(), True


def attachment_exists(cur, policy_id: str, filename: str) -> bool:
    cur.execute(
        "select 1 from policy_attachments where policy_id=%s and pdf_filename=%s limit 1",
        (policy_id, filename),
    )
    return cur.fetchone() is not None


def add_attachment(cur, parent: dict, item: dict, commit: bool, uploaded=None):
    filename = Path(item["path"]).name
    if not str(parent["id"]).startswith("DRY-") and attachment_exists(cur, parent["id"], filename):
        return "existing"
    if not commit:
        return "planned"
    url, size = uploaded
    cur.execute(
        """insert into policy_attachments
           (policy_id,doc_type,label,pdf_url,pdf_filename,pdf_size,note,
            net_premium,stamp_duty,vat,total_premium,coverage_start,coverage_end)
           values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (parent["id"], item["doc_type"], item["label"], url, filename, size,
         item["note"], item.get("net_premium"), item.get("stamp_duty"),
         item.get("vat"), item.get("total_premium"), item.get("coverage_start"),
         item.get("coverage_end")),
    )
    return "inserted"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env")
    inventory = load_inventory()
    first_files = sorted(FIRST.glob("*.pdf"), key=lambda p: p.name)
    if len(first_files) != 50 or len(RENEWAL_POLICY_NUMBERS) != 50:
        raise RuntimeError("renewal file/order count changed")

    report = {
        "mode": "commit" if args.commit else "dry-run", "attachments": [],
        "created_parents": [], "corrections": [], "held": [], "recovered": [], "errors": [],
    }
    conn = psycopg2.connect(db_url(), connect_timeout=20)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            parents = {}
            for number, values in MISSING_PARENTS.items():
                parents[number], created = ensure_missing_parent(cur, number, values, args.commit)
                if created:
                    report["created_parents"].append(number)

            for index, (path, number) in enumerate(zip(first_files, RENEWAL_POLICY_NUMBERS)):
                if number in parents:
                    parent = parents[number]
                elif index == 13:
                    # The old motor row is absent; same person, plate and 2568
                    # coverage are present uniquely as this compulsory policy.
                    parent = find_policy(cur, "D0-72-68/019485")
                else:
                    parent = find_policy(cur, number)
                report["attachments"].append({
                    "index": index, "path": str(path), "sha256": inventory[index]["sha256"],
                    "parent": parent["policy_number"], "doc_type": "other",
                    "label": f"ใบแจ้งเตือนต่ออายุปี 2569 ({number})",
                    "note": "จับคู่ด้วยเลขกรมธรรม์เดิมและปีคุ้มครอง; ไม่ใช้เบี้ยเสนอทับข้อมูลกรมธรรม์",
                    "coverage_start": None, "coverage_end": None,
                })

            for index, parent_no, dtype, label, note, net, stamp, vat, total in EXTRA_ATTACHMENTS:
                row = inventory[index]
                report["attachments"].append({
                    "index": index, "path": row["path"], "sha256": row["sha256"],
                    "parent": parent_no, "doc_type": dtype, "label": label, "note": note,
                    "net_premium": net, "stamp_duty": stamp, "vat": vat,
                    "total_premium": total, "coverage_start": None, "coverage_end": None,
                })

            recovered = ROOT / "output" / "pdf" / "กู้คืน-ดรวมกันทุกภัยแล้วไม่เกิน-25690821065236706.pdf"
            recovered_sha = hashlib.sha256(recovered.read_bytes()).hexdigest()
            report["attachments"].append({
                "index": 55, "path": str(recovered), "sha256": recovered_sha,
                "parent": "D0-18-69/001162", "doc_type": "other",
                "label": "เอกสารแนบท้ายกรมธรรม์อัคคีภัย D0-18-69/001162 (กู้คืนจากไฟล์เสีย)",
                "note": "กู้ภาพหน้าแรกที่สมบูรณ์จาก PDF ต้นฉบับซึ่งขาด xref/trailer; ไม่พบหน้าถัดไปที่สมบูรณ์",
                "coverage_start": None, "coverage_end": None,
            })
            report["recovered"].append({
                "index": 55, "source_file": inventory[55]["filename"],
                "recovered_file": str(recovered), "parent": "D0-18-69/001162",
            })

            # Resolve every target before any upload/write.
            resolved = []
            for item in report["attachments"]:
                parent = parents.get(item["parent"])
                if parent is None:
                    parent = find_policy(cur, item["parent"])
                item["parent_id"] = parent["id"]
                state = add_attachment(cur, parent, item, False)
                item["state_before"] = state
                resolved.append((item, parent))

            for number, value in SUM_INSURED_CORRECTIONS.items():
                policy = find_policy(cur, number)
                before = policy.get("sum_insured")
                own_before = policy.get("own_damage")
                report["corrections"].append({
                    "policy_number": number, "sum_insured_before": str(before),
                    "sum_insured_after": value, "own_damage_before": str(own_before),
                    "own_damage_after": value,
                })

            if not args.commit:
                conn.rollback()
                print(json.dumps({
                    "attachments_planned": sum(x[0]["state_before"] == "planned" for x in resolved),
                    "attachments_existing": sum(x[0]["state_before"] == "existing" for x in resolved),
                    "parents_to_create": len(report["created_parents"]),
                    "corrections": len(report["corrections"]), "held": len(report["held"]),
                }, ensure_ascii=False, indent=2))
                return 0

            s3 = storage_client()
            for item, parent in resolved:
                if item["state_before"] == "existing":
                    item["result"] = "existing"
                    continue
                uploaded = upload_pdf(s3, Path(item["path"]), item["sha256"])
                item["result"] = add_attachment(cur, parent, item, True, uploaded)

            for correction in report["corrections"]:
                cur.execute(
                    """update insurance_policies set sum_insured=%s, own_damage=%s,
                       manually_edited=true, keyby=%s
                       where regexp_replace(upper(policy_number), '[^A-Z0-9]', '', 'g')=%s""",
                    (correction["sum_insured_after"], correction["own_damage_after"],
                     "Codex visual verification 2026-09-09", norm(correction["policy_number"])),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"correction affected {cur.rowcount}: {correction['policy_number']}")
            for number, values in MISSING_PARENTS.items():
                cur.execute(
                    """update insurance_policies set license_province=%s
                       where regexp_replace(upper(policy_number), '[^A-Z0-9]', '', 'g')=%s
                         and keyby='Codex verified PDF import 2026-09-09'""",
                    (values.get("license_province"), norm(number)),
                )
                if cur.rowcount != 1:
                    raise RuntimeError(f"stub province update affected {cur.rowcount}: {number}")
            conn.commit()
    except Exception as exc:
        conn.rollback()
        report["errors"].append(f"{type(exc).__name__}: {exc}")
        raise
    finally:
        conn.close()
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({
        "attachments_inserted": sum(x.get("result") == "inserted" for x in report["attachments"]),
        "attachments_existing": sum(x.get("result") == "existing" for x in report["attachments"]),
        "parents_created": len(report["created_parents"]),
        "corrections": len(report["corrections"]), "held": len(report["held"]),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
