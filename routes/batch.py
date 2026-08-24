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
from services.pdf_extractor import extract_text_from_pdf
from services.claude_parser import parse_insurance_data
from services.supabase_shim import create_client
from services import doc_pairing
from routes.upload import (
    _upload_pdf_to_storage, _make_display_filename, ALLOWED_COLUMNS,
    INT_FIELDS, FLOAT_FIELDS, DATE_FIELDS, _clean_thai_number, _normalize_date,
)

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=2)

STAGING_ROOT = os.path.join(tempfile.gettempdir(), "insurance_batch_staging")
# 0 = ไม่จำกัดจำนวนไฟล์ต่อกอง; ค่าเริ่มต้น 1 ป้องกันชน quota ขณะอัปหลายไฟล์
try:
    MAX_FILES = max(0, int(os.getenv("BATCH_MAX_FILES", "0")))
    AI_CONCURRENCY = max(1, int(os.getenv("BATCH_AI_CONCURRENCY", "1")))
except ValueError:
    MAX_FILES, AI_CONCURRENCY = 0, 1


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


# ── progress (แยกไฟล์เล็ก ให้ client poll ระหว่างอ่าน) ──────────────
def _progress_path(batch_id: str) -> str:
    return os.path.join(_batch_dir(batch_id), "progress.json")


def _write_progress(batch_id: str, **kw) -> None:
    try:
        with open(_progress_path(batch_id), "w", encoding="utf-8") as fh:
            json.dump(kw, fh, ensure_ascii=False)
    except Exception:
        pass


def _read_progress(batch_id: str) -> dict:
    p = _progress_path(batch_id)
    if not os.path.exists(p):
        return {"status": "unknown", "done": 0, "total": 0, "current": None}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"status": "processing", "done": 0, "total": 0, "current": None}


def _read_one_file(bdir: str, rec: dict) -> dict:
    """AI Vision อ่านภาพจาก PDF; OCR ในเครื่องเป็น fallback เมื่อ API ใช้ไม่ได้."""
    if rec["same_file_as"]:
        rec["parsed"] = {}
        rec["parse_error"] = f"ไฟล์ซ้ำกับ {rec['same_file_as']} — ข้ามการอ่าน"
        return rec
    with open(os.path.join(bdir, f"{rec['file_id']}.pdf"), "rb") as fh:
        blob = fh.read()
    for attempt in range(3):
        try:
            rec["parsed"] = parse_with_gemini(blob, filename=rec["orig_filename"]) or {}
            rec["parsed"]["parse_engine"] = "gemini_vision"
            rec.pop("parse_error", None)
            return rec
        except Exception as error:
            message = str(error)
            if "API_KEY_INVALID" in message or "API key not valid" in message:
                rec["parsed"] = {}
                rec["parse_error"] = "GEMINI_API_KEY ไม่ถูกต้องหรือถูกปิดใช้งาน — กรุณาแก้ไขค่าตั้งค่า AI ก่อนเริ่มกองใหม่"
                return rec
            is_rate_limit = any(token in message.lower() for token in ("429", "resource_exhausted", "quota"))
            if is_rate_limit and attempt < 2:
                time.sleep(15 * (attempt + 1))
                continue

            # ไม่ทิ้งทั้งกองหาก AI key/บริการมีปัญหา: ใส่ข้อมูล OCR สำหรับ review ไว้ให้
            try:
                raw_text = extract_text_from_pdf(blob)
                rec["parsed"] = parse_insurance_data(raw_text, filename=rec["orig_filename"]) or {}
                rec["parsed"].update({"raw_text": raw_text[:12000], "parse_engine": "ocr_fallback"})
                rec["parse_error"] = (
                    "AI ชนโควตา — ใช้ OCR สำรอง กรุณาตรวจทานก่อนบันทึก"
                    if is_rate_limit else f"AI อ่านไม่สำเร็จ — ใช้ OCR สำรอง ({message[:80]})"
                )
            except Exception as fallback_error:
                rec["parsed"] = {}
                rec["parse_error"] = f"อ่านเอกสารไม่สำเร็จ: {str(fallback_error)[:160]}"
            return rec
    return rec


async def _process_batch(batch_id: str, bdir: str, staged: list[dict]) -> None:
    """อ่านทุกไฟล์เบื้องหลัง + อัปเดต progress ทีละไฟล์ → จับคู่ → เขียน manifest"""
    total = len(staged)
    try:
        loop = asyncio.get_event_loop()
        done = 0
        sem = asyncio.Semaphore(AI_CONCURRENCY)

        async def _one(r):
            nonlocal done
            async with sem:
                await loop.run_in_executor(_executor, _read_one_file, bdir, r)
            done += 1
            _write_progress(batch_id, status="processing", done=done, total=total,
                            current=r.get("orig_filename"))

        await asyncio.gather(*[_one(r) for r in staged])

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
        _save_manifest(batch_id, {"batch_id": batch_id, "files": staged, "result": result})
        _write_progress(batch_id, status="done", done=total, total=total, current=None)
    except Exception as e:
        print("[batch-process] ERROR:\n", traceback.format_exc())
        _write_progress(batch_id, status="error", done=0, total=total, current=None, error=str(e)[:200])


# ── 1) อัป + อ่าน + จับคู่ ──────────────────────────────────────────
@router.post("/batch/extract")
async def batch_extract(files: list[UploadFile] = File(...)):
    """รับไฟล์ทีละหลายสิบ/หลายร้อย → AI อ่านทุกไฟล์ → จัดประเภท → จับคู่ กธ↔พรบ
    ยังไม่บันทึกลงฐานข้อมูล"""
    if not files:
        raise HTTPException(status_code=400, detail="ไม่พบไฟล์")
    if not gemini_available():
        raise HTTPException(
            status_code=503,
            detail="ยังไม่ได้ตั้งค่า GEMINI_API_KEY ใน backend — AI จึงไม่สามารถอ่านเอกสารได้",
        )
    if MAX_FILES and len(files) > MAX_FILES:
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

    # ── เริ่มอ่านเบื้องหลัง แล้วให้ client poll /progress ดูความคืบหน้าทีละไฟล์ ──
    _save_manifest(batch_id, {"batch_id": batch_id, "files": staged, "result": None})
    _write_progress(batch_id, status="processing", done=0, total=len(staged), current=None)
    asyncio.create_task(_process_batch(batch_id, bdir, staged))

    return {"success": True, "batch_id": batch_id, "total": len(staged), "status": "processing"}


# ── 1.5) ถามความคืบหน้าระหว่างอ่าน ─────────────────────────────────
@router.get("/batch/{batch_id}/progress")
async def batch_progress(batch_id: str):
    return {"success": True, **_read_progress(batch_id)}


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
