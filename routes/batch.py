"""
batch.py — อัปโหลดกรมธรรม์แบบกอง (โยนหลายไฟล์ทีเดียว)

Flow:  POST /batch/extract  → อัปไฟล์เข้า staging + AI อ่าน + จัดประเภท + จับคู่
       GET  /batch/{id}     → ดึงผลกลับมาแสดงหน้า review
       POST /batch/{id}/commit → บันทึกเฉพาะรายการที่คนยืนยันแล้วลง DB จริง

ไฟล์ที่อัปจะพักไว้ใน staging (ดิสก์ชั่วคราว) ยังไม่แตะฐานข้อมูลจริง
จนกว่าจะเรียก /commit — ออกแบบตามหลัก "AI เร่งงาน ไม่ใช่ตัดสินใจแทน"
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import os, json, uuid, asyncio, tempfile, shutil, hashlib, traceback, time

from services.gemini_parser import parse_with_gemini, is_available as gemini_available
from services.supabase_shim import create_client
from services import doc_pairing
from routes.upload import (
    _upload_pdf_to_storage, _make_display_filename, ALLOWED_COLUMNS,
    INT_FIELDS, FLOAT_FIELDS, DATE_FIELDS, _clean_thai_number, _normalize_date,
)

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=2)   # คุม concurrency ไม่ให้ชน rate limit ฟรีของ Gemini

STAGING_ROOT = os.path.join(tempfile.gettempdir(), "insurance_batch_staging")
MAX_FILES = 300


@lru_cache(maxsize=1)
def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


# ── staging helpers ────────────────────────────────────────────────
def _batch_dir(batch_id: str) -> str:
    safe = os.path.basename(batch_id)            # กัน path traversal
    return os.path.join(STAGING_ROOT, safe)


def _manifest_path(batch_id: str) -> str:
    return os.path.join(_batch_dir(batch_id), "manifest.json")


def _load_manifest(batch_id: str) -> dict:
    path = _manifest_path(batch_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="ไม่พบกองไฟล์นี้ (อาจหมดอายุแล้ว)")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save_manifest(batch_id: str, data: dict) -> None:
    with open(_manifest_path(batch_id), "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


# ── 1) อัป + อ่าน + จับคู่ ──────────────────────────────────────────
@router.post("/batch/extract")
async def batch_extract(files: list[UploadFile] = File(...)):
    """รับไฟล์ทีละหลายสิบ/หลายร้อย → AI อ่านทุกไฟล์ → จัดประเภท → จับคู่ กธ↔พรบ
    ยังไม่บันทึกลงฐานข้อมูล"""
    if not files:
        raise HTTPException(status_code=400, detail="ไม่พบไฟล์")
    if len(files) > MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"อัปได้สูงสุด {MAX_FILES} ไฟล์ต่อครั้ง (ส่งมา {len(files)})")

    batch_id = uuid.uuid4().hex[:12]
    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)

    # ── เก็บไฟล์เข้า staging ก่อน (พร้อม hash กันไฟล์ซ้ำ) ──
    staged, seen_hashes = [], {}
    for i, f in enumerate(files):
        if not (f.filename or "").lower().endswith(".pdf"):
            continue
        data = await f.read()
        sha = hashlib.sha256(data).hexdigest()
        file_id = f"{i:04d}"
        with open(os.path.join(bdir, f"{file_id}.pdf"), "wb") as fh:
            fh.write(data)
        rec = {
            "file_id":       file_id,
            "orig_filename": f.filename,
            "size":          len(data),
            "sha256":        sha,
            "same_file_as":  seen_hashes.get(sha),   # ไฟล์เดียวกันเป๊ะ (อัปซ้ำ)
        }
        seen_hashes.setdefault(sha, file_id)
        staged.append(rec)

    if not staged:
        shutil.rmtree(bdir, ignore_errors=True)
        raise HTTPException(status_code=400, detail="รองรับเฉพาะไฟล์ PDF")

    # ── ให้ AI อ่าน (ข้ามไฟล์ที่ซ้ำเป๊ะ) ──
    if not gemini_available():
        # ไม่มี key → คืนโครงว่างพร้อมบอกสาเหตุ ให้หน้าเว็บแสดงได้ว่าติดอะไร
        for r in staged:
            r["parsed"] = {}
            r["parse_error"] = "ยังไม่ได้ตั้งค่า GEMINI_API_KEY — AI อ่านไฟล์ไม่ได้"
    else:
        loop = asyncio.get_event_loop()

        def _read_one(rec: dict) -> dict:
            if rec["same_file_as"]:
                rec["parsed"] = {}
                rec["parse_error"] = f"ไฟล์ซ้ำกับ {rec['same_file_as']} — ข้ามการอ่าน"
                return rec
            with open(os.path.join(bdir, f"{rec['file_id']}.pdf"), "rb") as fh:
                blob = fh.read()
            # retry เมื่อชนลิมิต Gemini (429/RESOURCE_EXHAUSTED) — free tier RPM ต่ำ
            for attempt in range(3):
                try:
                    rec["parsed"] = parse_with_gemini(blob, filename=rec["orig_filename"]) or {}
                    rec.pop("parse_error", None)
                    return rec
                except Exception as e:
                    msg = str(e)
                    is_rate = "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower()
                    if is_rate and attempt < 2:
                        time.sleep(10 * (attempt + 1))   # 10s, 20s แล้วลองใหม่
                        continue
                    rec["parsed"] = {}
                    rec["parse_error"] = ("โควตา Gemini หมด/ชนลิมิต (429) — ลองไฟล์น้อยลง หรืออัปเกรด API key"
                                          if is_rate else msg[:200])
                    return rec
            return rec

        await asyncio.gather(*[loop.run_in_executor(_executor, _read_one, r) for r in staged])

    # ── จัดประเภท + จับคู่ ──
    records = []
    for r in staged:
        rec = dict(r.get("parsed") or {})
        rec["file_id"]       = r["file_id"]
        rec["orig_filename"] = r["orig_filename"]
        rec["parse_error"]   = r.get("parse_error")
        rec["same_file_as"]  = r.get("same_file_as")
        records.append(rec)

    unique, dups = doc_pairing.dedupe(records)
    result = doc_pairing.pair_documents(unique)
    result["duplicates"] = dups
    result["summary"]["duplicates"] = len(dups)

    manifest = {"batch_id": batch_id, "files": staged, "result": result}
    _save_manifest(batch_id, manifest)

    return {"success": True, "batch_id": batch_id, **result}


# ── 2) ดึงผลกลับมาแสดง ─────────────────────────────────────────────
@router.get("/batch/{batch_id}")
async def batch_get(batch_id: str):
    m = _load_manifest(batch_id)
    return {"success": True, "batch_id": batch_id, **m["result"]}


# ── 3) commit เฉพาะที่คนยืนยัน ─────────────────────────────────────
def _clean_for_db(data: dict) -> dict:
    """แปลงค่าให้ตรงชนิดคอลัมน์ (ยืมกติกาเดียวกับ /save-policy)"""
    out = {}
    for k, v in (data or {}).items():
        if k not in ALLOWED_COLUMNS:
            continue
        if k in INT_FIELDS:
            try:
                c = _clean_thai_number(str(v)) if v not in (None, "") else ""
                out[k] = int(float(c)) if c not in ("", "None", "null") else None
            except (ValueError, TypeError):
                out[k] = None
        elif k in FLOAT_FIELDS:
            try:
                c = _clean_thai_number(str(v)) if v not in (None, "") else ""
                out[k] = float(c) if c not in ("", "None", "null") else None
            except (ValueError, TypeError):
                out[k] = None
        elif k in DATE_FIELDS:
            out[k] = _normalize_date(str(v).strip() if v else "")
        else:
            out[k] = (str(v).strip() or None) if v not in (None, "", "null") else None
    return out


@router.post("/batch/{batch_id}/commit")
async def batch_commit(batch_id: str, payload: dict):
    """payload = {"items":[{"main":{...}, "prb":{...}|null,
                            "main_file_id":"0000", "prb_file_id":"0001"}]}
    สร้างกรมธรรม์หลักจาก main แล้วแนบ พ.ร.บ. เป็น attachment"""
    m = _load_manifest(batch_id)
    bdir = _batch_dir(batch_id)
    items = payload.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="ไม่มีรายการให้บันทึก")

    supabase = get_supabase()
    loop = asyncio.get_event_loop()
    created, failed = [], []

    for it in items:
        main = _clean_for_db(it.get("main") or {})
        if not main:
            failed.append({"reason": "ข้อมูลกรมธรรม์ว่าง", "item": it})
            continue
        main["manually_edited"] = True
        try:
            # อัปไฟล์ กธ ขึ้น storage (ถ้ามี)
            mf = it.get("main_file_id")
            if mf:
                fname = _make_display_filename(
                    plate=main.get("license_plate"), doc_type="main",
                    coverage_end=main.get("coverage_end"),
                    policy_type=main.get("policy_type"),
                    address=main.get("insured_address"), name=main.get("insured_name"))
                with open(os.path.join(bdir, f"{os.path.basename(mf)}.pdf"), "rb") as fh:
                    blob = fh.read()
                url = await loop.run_in_executor(
                    _executor, lambda: _upload_pdf_to_storage(supabase, blob, fname))
                main.update({"pdf_url": url, "pdf_filename": fname, "pdf_size": len(blob)})

            res = supabase.table("insurance_policies").insert(main).execute()
            policy_id = res.data[0]["id"]

            # แนบ พ.ร.บ.
            prb = _clean_for_db(it.get("prb") or {})
            pf = it.get("prb_file_id")
            if prb or pf:
                att = {"policy_id": policy_id, "doc_type": "prb", "label": "พ.ร.บ."}
                for key in ("net_premium", "stamp_duty", "vat", "total_premium",
                            "coverage_start", "coverage_end"):
                    if prb.get(key) is not None:
                        att[key] = prb[key]
                if pf:
                    pname = _make_display_filename(
                        plate=(prb.get("license_plate") or main.get("license_plate")),
                        doc_type="prb", coverage_end=prb.get("coverage_end"))
                    with open(os.path.join(bdir, f"{os.path.basename(pf)}.pdf"), "rb") as fh:
                        pblob = fh.read()
                    purl = await loop.run_in_executor(
                        _executor, lambda: _upload_pdf_to_storage(supabase, pblob, pname))
                    att.update({"pdf_url": purl, "pdf_filename": pname,
                                "pdf_size": len(pblob)})
                supabase.table("policy_attachments").insert(att).execute()

            created.append({"policy_id": policy_id,
                            "license_plate": main.get("license_plate")})
        except Exception as e:
            print("[batch-commit] ERROR:\n", traceback.format_exc())
            failed.append({"reason": str(e)[:200],
                           "license_plate": main.get("license_plate")})

    m["result"]["committed"] = {"created": len(created), "failed": len(failed)}
    _save_manifest(batch_id, m)
    return {"success": True, "created": created, "failed": failed,
            "summary": {"created": len(created), "failed": len(failed)}}


# ── 4) ล้าง staging ────────────────────────────────────────────────
@router.delete("/batch/{batch_id}")
async def batch_discard(batch_id: str):
    shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)
    return {"success": True}
