"""Direct, resumable import of the visually verified August 2026 PDF set.

This bypasses the frontend.  Originals are uploaded through the production
backend's storage-only endpoint (R2), then rows are written to Neon directly.
Dry-run is the default; pass --commit to mutate external systems.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import psycopg2
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(r"C:\Users\Administrator\Downloads\งาน\renamed-pdfs-ocr")
MANIFEST = ROOT / "output" / "chatgpt_verified_match_manifest.json"
EXTRACTED = ROOT / "output" / "chatgpt_verified_extracted_data.json"
REPORT = ROOT / "output" / "chatgpt_verified_direct_import_report.json"
API = os.getenv("IMPORT_API_URL", "https://insurance-backend-c2s2.onrender.com/api").rstrip("/")

ALLOWED = {
    "policy_number", "company_code", "insured_name", "insured_address", "license_plate",
    "license_province", "chassis_no", "car_make", "car_model", "car_year",
    "coverage_start", "coverage_end", "net_premium", "stamp_duty", "vat",
    "total_premium", "third_party_per_person", "third_party_per_accident",
    "own_damage", "broker_name", "broker_license", "phone", "notes", "app_number",
    "policy_type", "new_renew", "sum_insured", "date_cancel", "agent_code",
    "manually_edited", "keyby", "pdf_url", "pdf_filename", "pdf_size",
}


def rec(policy, file, name, address, start, end, *, kind="M", chassis=None, plate=None,
        province=None, make=None, model=None, year=None, insured=None, net=None, stamp=None,
        vat=None, total=None, company="TMSTH", renew="N", notes=None, date_cancel=None):
    return {
        "policy_number": policy, "file": file, "company_code": company,
        "insured_name": name, "insured_address": address, "coverage_start": start,
        "coverage_end": end, "policy_type": kind, "new_renew": renew,
        "chassis_no": chassis, "license_plate": plate, "license_province": province,
        "car_make": make, "car_model": model, "car_year": year,
        "sum_insured": insured, "net_premium": net, "stamp_duty": stamp,
        "vat": vat, "total_premium": total, "notes": notes, "date_cancel": date_cancel,
    }


MANUAL = [
    rec("D0-70-69/023992", "บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จํากัด(มหาชน - 25690821065541373.pdf", "บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จำกัด (มหาชน)", "157 ซอยรามอินทรา 34 ถนนรามอินทรา แขวงท่าแร้ง เขตบางเขน กรุงเทพมหานคร 10230", "2026-08-26", "2027-08-26", chassis="MR0CB8CCX00295623", plate="1ฒน 9802 กท", province="กรุงเทพมหานคร", make="TOYOTA", model="HILUX REVO", year=2017, insured=270000, net=13021, stamp=53, vat=915.18, total=13989.18),
    rec("D0-70-69/023990", "บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จํากัด(มหาชน) ๊ - 25690821065526442.pdf", "บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จำกัด (มหาชน)", "157 ซอยรามอินทรา 34 ถนนรามอินทรา แขวงท่าแร้ง เขตบางเขน กรุงเทพมหานคร 10230", "2026-08-27", "2027-08-27", chassis="MHYGDN71T00407249", plate="1ฒพ 4390 กท", province="กรุงเทพมหานคร", make="SUZUKI", model="CARRY", year=2015, insured=123000, net=6050, stamp=25, vat=425.25, total=6500.25),
    rec("D0-70-69/023500", "ดร. สืบสกุล พิภพมงคล.pdf", "ดร. สืบสกุล ภิภพมงคล (โครงการรัฐสภาอาคาร 4)", "721/1 ซอยพหลโยธิน 1 แขวงสามเสนใน เขตพญาไท กรุงเทพมหานคร 10400", "2026-08-16", "2027-08-16", chassis="W0L0TGF752H030979", plate="จม 2066 กท", province="กรุงเทพมหานคร", make="CHEVROLET", model="ZAFIRA", year=2002, net=1861, stamp=8, vat=130.83, total=1999.83),
    rec("D0-10-69/000249", "นาง กาญจนา บุนนาค - 25690821065141840.pdf", "นาง กาญจนา บุนนาค", "27 ซอยศูนย์วิจัย 5 ถนนเพชรบุรีตัดใหม่ แขวงบางกะปิ เขตห้วยขวาง กรุงเทพมหานคร 10310", "2026-08-18", "2027-08-18", kind="FIRE", insured=500000, net=600, stamp=3, vat=42.21, total=645.21),
    rec("D0-70-69/024948", "นาง ยุคล เมาะตระกูล.pdf", "นาง ยุคล เมาะตระกูล", "55/11 หมู่ 8 อำเภอเชียรใหญ่ จังหวัดนครศรีธรรมราช 80340", "2026-08-09", "2027-08-09", chassis="PPIKFA37AJM003022", plate="7กต 4502 กท", province="กรุงเทพมหานคร", make="MAZDA", model="CX-5", year=2018, insured=480000, net=15823.85, stamp=64, vat=1112.15, total=17000),
    rec("D0-10-69/000254", "นางสาว วรรณิยา ศรีอังกูร - 25690821064454722.pdf", "นางสาว วรรณิยา ศรีอังกูร", "27 ซอยศูนย์วิจัย 5 ถนนเพชรบุรีตัดใหม่ แขวงบางกะปิ เขตห้วยขวาง กรุงเทพมหานคร 10310", "2026-08-18", "2027-08-18", kind="FIRE", insured=500000, net=600, stamp=3, vat=42.21, total=645.21),
    rec("D0-36-69/001209", "นาย กรีธา ศิริต้นคิกร - 25690821064137583.pdf", "นาย กรีธา ศิริต้นคิกร", "41/7, 41/34 จรัญสนิทวงศ์ ซอย 7 ถนนจรัญสนิทวงศ์ แขวงวัดท่าพระ เขตบางกอกใหญ่ กรุงเทพมหานคร 10600", "2026-08-28", "2027-08-28", kind="ASSET", insured=1500000, net=2885, stamp=12, vat=202.79, total=3099.79),
    rec("D0-11-69/001007", "นาย ธนัท พรชัยกิจโกศล .pdf", "นาย ธนัท พรชัยกิจโกศล", "62/349 หมู่บ้านธนากร ถนนหทัยราษฎร์ ตำบลวัดชลอ อำเภอบางกรวย จังหวัดนนทบุรี 11130", "2026-08-10", "2027-08-10", kind="ASSET", insured=800000, net=1500, stamp=6, vat=105.42, total=1611.42),
    rec("D0-70-69/024917", "นาย พิชิต สับใหม่ - 25690821064617283.pdf", "นาย พิชิต สืบใหม่", "93 หมู่ 8 ตำบลหนองขาว อำเภอท่าม่วง จังหวัดกาญจนบุรี 71110", "2026-08-28", "2027-08-28", chassis="3T-111792", plate="ปน 3416 กท", province="กรุงเทพมหานคร", make="ISUZU", model="D-MAX", year=2002, insured=100000, net=6051, stamp=25, vat=425.32, total=6501.32),
    rec("D0-36-69/001220", "นาย สมบูรณ์ ถาวรเกษม - 25690821065258384.pdf", "นาย สมบูรณ์ ถาวรเกษม", "9/41 ซอยสุขาภิบาล 5 หมู่บ้านพฤกษาวิลเลจ 8 (ซอย 2/6) ถนนสุขาภิบาล 5 แขวงออเงิน เขตสายไหม กรุงเทพมหานคร", "2026-08-19", "2027-08-19", kind="ASSET", insured=2000000, net=3000, stamp=12, vat=210.84, total=3222.84),
    rec("D0-70-69/023969", "นาย สมบูรณ์ พุ่มเล็ก - 25690821065515230.pdf", "นาย สมบูรณ์ พุ่มเล็ก", "95 ซอยศูนย์การค้าแฮปปี้แลนด์ 1 ถนนลาดพร้าว แขวงคลองจั่น เขตบางกะปิ กรุงเทพมหานคร 10240", "2026-08-03", "2027-08-03", chassis="MR053HY9305345506", plate="มว 7153 กท", province="กรุงเทพมหานคร", make="TOYOTA", model="VIOS", year=2012, insured=190000, net=10416, stamp=42, vat=732.06, total=11190.06, date_cancel="2026-08-03", notes="ยกเลิกโดยสลักหลัง 69-009918-001 มีผล 3 ส.ค. 2569"),
    rec("D0-10-69/000215", "นายทวีศักดิ์ ลังการ์พินธุ์ - 25690821070057013.pdf", "นายทวีศักดิ์ ลังการ์พินธุ์", "594/138-9 ถนนอโศก-ดินแดง แขวงดินแดง เขตดินแดง กรุงเทพมหานคร 10400", "2026-07-09", "2027-07-09", kind="FIRE", insured=3000000, net=3000, stamp=12, vat=210.84, total=3222.84),
    rec("D0-18-69/001171", "นายวีระชัย อาชวนิมิตรกุล - 25690821064314586.pdf", "นาย วีระชัย อาชวนิมิตรกุล", "295/10 ซอยกิ่งเพชร ถนนเพชรบุรี แขวงถนนเพชรบุรี เขตราชเทวี กรุงเทพมหานคร 10400", "2026-08-24", "2027-08-24", kind="FIRE", insured=700000, net=828, stamp=4, vat=58.24, total=890.24),
    rec("D0-70-69/022880", "บริษัท รีไลแอนซ พลาสเคม จํากัด - 25690821065733392.pdf", "บริษัท รีไลแอนซ พลาสเคม จำกัด", "79 ถนนเจริญราษฎร์ แขวงบางโคล่ เขตบางคอแหลม กรุงเทพมหานคร 10120", "2026-08-16", "2027-08-16", chassis="MLHKC1787D5302263", plate="1กผ 2259 กท", province="กรุงเทพมหานคร", make="HONDA", model="CBR150", year=2013, net=1219, stamp=5, vat=85.68, total=1309.68),
    rec("D0-36-69/001173", "บริษัท ฯหรา ต่ออายุ - 25690821064230321.pdf", "นางสาว ชุดาพร ศรีกุลปิยรัตน์", "599/322 โครงการบ้านกลางกรุง The Royal Vienna รัชวิภา ซอยรัชดา 29 ถนนรัชดาภิเษก แขวงลาดยาว เขตจตุจักร กรุงเทพมหานคร", "2026-08-23", "2027-08-23", kind="ASSET", insured=1500000, net=1500, stamp=6, vat=105.42, total=1611.42),
    rec("726-01331-78374", "พันตำรวจโทสมเกียรติ อุปกรณ์ศิริการ.pdf", "พันตำรวจโทสมเกียรติ อุปกรณ์ศิริการ", "40/12 ตำบลแพรกศรีราชา อำเภอสรรคบุรี จังหวัดชัยนาท 17140", "2026-07-19", "2027-07-19", company="BKI", chassis="MRHRM2830FP101541", plate="5กฏ-5472 กท", province="กรุงเทพมหานคร", make="HONDA", model="CR-V 2.0 E", year=2013, insured=390000, net=13655.28, stamp=55, vat=959.72, total=14670),
    rec("D0-10-69/000255", "หจก.วินัยยานยนต์ - 25690821064426999.pdf", "หจก.วินัยยานยนต์ และ/หรือ นายวินัย การเวก", "162 ซอยอ่อนนุช 78 ถนนอ่อนนุช แขวงประเวศ เขตประเวศ กรุงเทพมหานคร 10250", "2026-08-19", "2027-08-19", kind="FIRE", insured=10000000, net=27600, stamp=111, vat=1939.77, total=29650.77),
]

OVERRIDES = {
 "D0-70-69/023873": {"insured_name":"บริษัท ภูธารา เซอร์วิส จำกัด และ/หรือ คุณณรงค์ ชาญประไพ","insured_address":"299/187 หมู่ 5 ซอยร่มเกล้า 38 แขวงคลองสามประเวศ เขตลาดกระบัง กรุงเทพมหานคร 10520","license_plate":"3ฒย 878 กท"},
 "D0-70-69/023503": {"insured_name":"หจก. ไทยเจริญอีควิปเมนท์ และ/หรือ คุณธวัชชัย อาชวนิมิตรกุล","license_plate":"3ฒผ 6763 กท"},
 "D0-70-69/023999": {"license_plate":"ฎฉ 4116 กท"},
 "D0-70-69/025154": {"license_plate":"ฉอ 5689 กท"},
 "D0-70-69/023492": {"insured_name":"นางสาว จิรสุดา พุ่มพวง"},
 "D0-70-69/025200": {"insured_name":"บริษัท เอ็ม.เค.เอส.แมชชีนเนอรี่ จำกัด (โครงการ 3)"},
 "D0-70-69/023991": {"license_plate":"1ฒพ 4391 กท","sum_insured":123000},
}


def build_records(manifest, extracted):
    parsed_by_file = {k: v["parsed"] for k, v in extracted["records"].items()}
    records = []
    for pair in manifest["exact_pairs"][:19]:
        data = dict(parsed_by_file[pair["main_file"]])
        data.update(policy_number=pair["main_policy_number"], chassis_no=pair["chassis_no"], file=pair["main_file"], policy_type="M", new_renew="N")
        data.update(OVERRIDES.get(pair["main_policy_number"], {}))
        records.append(data)
    stand = parsed_by_file["ดราชสิงขร - 25690821065203793.pdf"].copy()
    stand.update(file="ดราชสิงขร - 25690821065203793.pdf", policy_type="FIRE", new_renew="R")
    records.append(stand)
    records.extend(MANUAL)
    assert len(records) == 37
    return records


def login(session):
    user = os.getenv("IMPORT_API_USERNAME") or os.getenv("APP_USERNAME")
    password = os.getenv("IMPORT_API_PASSWORD") or os.getenv("APP_PASSWORD")
    if not user or not password:
        raise RuntimeError("APP_USERNAME/APP_PASSWORD are not configured")
    r = session.post(f"{API}/auth/login", json={"username": user, "password": password}, timeout=120)
    r.raise_for_status()
    token = r.json().get("access_token") or r.json().get("token")
    if not token: raise RuntimeError("login returned no token")
    session.headers["Authorization"] = f"Bearer {token}"


def upload(session, path):
    for attempt in range(5):
        try:
            with path.open("rb") as f:
                r = session.post(f"{API}/upload-pdf-only", files={"file": (path.name, f, "application/pdf")}, timeout=300)
            if r.status_code < 500:
                r.raise_for_status(); return r.json()
        except requests.RequestException:
            if attempt == 4: raise
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"upload failed: {path.name}")


def find_policy(cur, number):
    cur.execute("select id,pdf_url from insurance_policies where upper(replace(policy_number,' ',''))=upper(replace(%s,' ','')) order by created_at desc limit 1", (number,))
    return cur.fetchone()


def insert_policy(cur, data):
    clean = {k:v for k,v in data.items() if k in ALLOWED and v is not None}
    cols = list(clean); vals = [clean[k] for k in cols]
    cur.execute(f"insert into insurance_policies ({','.join(cols)}) values ({','.join(['%s']*len(cols))}) returning id", vals)
    return cur.fetchone()[0]


def attachment_exists(cur, policy_id, file):
    cur.execute("select 1 from policy_attachments where policy_id=%s and pdf_filename=%s limit 1", (policy_id, file))
    return cur.fetchone() is not None


def add_attachment(cur, policy_id, doc_type, label, source_file, uploaded, note=None):
    cur.execute("insert into policy_attachments(policy_id,doc_type,label,pdf_url,pdf_filename,pdf_size,note) values(%s,%s,%s,%s,%s,%s,%s)",
                (policy_id,doc_type,label,uploaded["pdf_url"],source_file,uploaded["pdf_size"],note))


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--commit", action="store_true"); args=ap.parse_args()
    load_dotenv(ROOT / ".env")
    manifest=json.loads(MANIFEST.read_text(encoding="utf-8")); extracted=json.loads(EXTRACTED.read_text(encoding="utf-8"))
    records=build_records(manifest, extracted)
    source_names={p.name for p in SOURCE.glob("*.pdf")}
    missing=[r["file"] for r in records if r["file"] not in source_names]
    assert not missing, missing
    report={"mode":"commit" if args.commit else "dry-run","planned_policies":37,"planned_prb":22,"inserted":[],"existing":[],"attachments":[],"held":[],"errors":[]}
    if not args.commit:
        print(json.dumps({"policies":len(records),"prb":len(manifest["exact_pairs"]),"extra_attachments":3,"held":3,"duplicates":3,"invalid":1},ensure_ascii=False,indent=2)); return
    session=requests.Session(); login(session)
    conn=psycopg2.connect(os.getenv("NEON_URL") or os.getenv("DATABASE_URL")); conn.autocommit=False; cur=conn.cursor()
    policy_ids={}
    try:
        for i,data in enumerate(records,1):
            number=data["policy_number"]; existing=find_policy(cur,number)
            if existing:
                policy_ids[number]=existing[0]; report["existing"].append(number); print(f"[{i}/37] EXISTS {number}"); continue
            print(f"[{i}/37] UPLOAD {number}")
            up=upload(session,SOURCE/data["file"])
            row=dict(data); row.update(pdf_url=up["pdf_url"],pdf_filename=data["file"],pdf_size=up["pdf_size"],manually_edited=True,keyby="ChatGPT verified direct import 2026-08-28")
            row.pop("file",None); row.pop("doc_type",None)
            pid=insert_policy(cur,row); conn.commit(); policy_ids[number]=pid; report["inserted"].append(number)
        for i,pair in enumerate(manifest["exact_pairs"],1):
            pid=policy_ids.get(pair["main_policy_number"]) or (find_policy(cur,pair["main_policy_number"]) or [None])[0]
            if not pid: raise RuntimeError(f"missing parent {pair['main_policy_number']}")
            if attachment_exists(cur,pid,pair["prb_file"]): continue
            print(f"[PRB {i}/22] {pair['prb_policy_number']}")
            up=upload(session,SOURCE/pair["prb_file"])
            add_attachment(cur,pid,"prb",f"พ.ร.บ. {pair['prb_policy_number']}",pair["prb_file"],up,f"จับคู่ด้วยเลขตัวถัง {pair['chassis_no']} ตรงกัน")
            conn.commit(); report["attachments"].append(pair["prb_policy_number"])
        extras=[
          ("D0-70-69/023976","other","ใบเพิ่มหนี้/เอกสารประกอบ","นาย พิชญูตม - 25690821065452939.pdf"),
          ("D0-70-69/023969","endorsement","สลักหลังยกเลิก 69-009918-001","นายสมบูรณ พุมเล็ก - 25690821065000167.pdf"),
          ("D0-70-69/022845","endorsement","สลักหลัง 69-008617-001","บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จํากัด (มหาชน - 25690821065609644.pdf"),
        ]
        for parent,dtype,label,file in extras:
            found=find_policy(cur,parent)
            if not found: report["held"].append({"file":file,"reason":f"ไม่พบ parent {parent}"}); continue
            if attachment_exists(cur,found[0],file): continue
            print(f"[EXTRA] {label}")
            up=upload(session,SOURCE/file); add_attachment(cur,found[0],dtype,label,file,up,"ตรวจชนิดเอกสารด้วยภาพ PDF")
            conn.commit(); report["attachments"].append(label)
        for item in manifest["attachments_requiring_parent_lookup"][:2]: report["held"].append(item)
        report["held"].append(manifest["invalid"][0])
    except Exception as e:
        conn.rollback(); report["errors"].append(str(e)); raise
    finally:
        cur.close(); conn.close(); REPORT.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:len(v) if isinstance(v,list) else v for k,v in report.items()},ensure_ascii=False,indent=2))


if __name__ == "__main__": main()

