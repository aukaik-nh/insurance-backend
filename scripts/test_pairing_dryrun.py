"""ทดสอบ doc_pairing กับข้อมูลจริงที่อ่านจากไฟล์สแกนชุด 2569-07-22

รันจาก root ของ backend:  .venv/Scripts/python.exe scripts/test_pairing_dryrun.py <rows_dir>
"""
import json, sys, glob, os, re

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from services.doc_pairing import pair_documents, dedupe, MOTOR_MAIN, MOTOR_PRB  # noqa: E402

DOC_MAP = {
    "กธ": MOTOR_MAIN,
    "พรบ": MOTOR_PRB,
    "ยกเลิก(ร.ย.11)": "endorsement",
    "ใบลดหนี้": "credit_note",
    "ใบคืนเบี้ย": "credit_note",
    "อัคคีภัย": "fire",
    "ประกัน SME/ทรัพย์สิน": "sme_property",
}


def be_year(thai_date: str | None) -> str | None:
    """'3 ก.ค. 2569' -> '2569-01-01' (พอสำหรับดึงปี)"""
    if not thai_date:
        return None
    m = re.search(r"(\d{4})", str(thai_date))
    return f"{m.group(1)}-01-01" if m else None


def load(rows_dir: str) -> list[dict]:
    recs = []
    for path in sorted(glob.glob(os.path.join(rows_dir, "*.json"))):
        with open(path, encoding="utf-8") as fh:
            for r in json.load(fh):
                recs.append({
                    "idx":            r["idx"],
                    "doc_type":       DOC_MAP.get(r.get("doc"), "unknown"),
                    "policy_number":  r.get("policy"),
                    "insured_name":   r.get("name"),
                    "license_plate":  r.get("plate"),
                    "chassis_no":     r.get("chassis"),
                    "car":            r.get("car"),
                    "coverage_start": be_year(r.get("start")),
                    "total_premium":  r.get("total"),
                })
    return recs


def main() -> None:
    rows_dir = sys.argv[1] if len(sys.argv) > 1 else "rows"
    records = load(rows_dir)
    print(f"โหลดมา {len(records)} เรคคอร์ด\n")

    unique, dups = dedupe(records)
    if dups:
        print(f"[dedupe] เจอสำเนาซ้ำ {len(dups)} ใบ:")
        for d in dups:
            print(f"   ไฟล์ {d['idx']} ซ้ำกับไฟล์ {d['duplicate_of']}  ({d['policy_number']})")
        print()

    res = pair_documents(unique)
    s = res["summary"]
    print("=== สรุป ===")
    print(f"  จับคู่ได้      : {s['pairs']}  (อัตโนมัติ {s['auto']} / ต้องยืนยัน {s['need_review']})")
    print(f"  กธ กำพร้า      : {s['orphan_main']}")
    print(f"  พ.ร.บ. กำพร้า  : {s['orphan_prb']}")
    print(f"  เอกสารอื่น     : {s['others']}\n")

    print("=== คู่ที่จับได้ ===")
    for p in res["pairs"]:
        m, b = p["main"], p["prb"]
        print(f"  [{p['status']:6}] {p['score']:3}  กธ#{m['idx']} + พรบ#{b['idx']}  "
              f"{(m.get('license_plate') or '-'):<14} {(m.get('chassis_no') or '-'):<20} "
              f"{(m.get('insured_name') or '')[:28]}")
        print(f"            เหตุผล: {', '.join(p['reasons'])}")

    if res["orphan_main"]:
        print("\n=== กธ ที่ยังไม่มี พ.ร.บ. ===")
        for m in res["orphan_main"]:
            print(f"  #{m['idx']}  {(m.get('license_plate') or '-'):<14} "
                  f"{(m.get('chassis_no') or '-'):<20} {(m.get('insured_name') or '')[:30]}")

    if res["orphan_prb"]:
        print("\n=== พ.ร.บ. ที่ยังไม่มี กธ ===")
        for b in res["orphan_prb"]:
            print(f"  #{b['idx']}  {(b.get('license_plate') or '-'):<14} "
                  f"{(b.get('chassis_no') or '-'):<20} {(b.get('insured_name') or '')[:30]}")

    if res["others"]:
        print("\n=== เอกสารชนิดอื่น (ไม่จับคู่รถ) ===")
        for o in res["others"]:
            print(f"  #{o['idx']}  {o['doc_type']:<14} {o.get('policy_number')}  "
                  f"{(o.get('insured_name') or '')[:30]}")


if __name__ == "__main__":
    main()
