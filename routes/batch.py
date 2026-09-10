"""
batch.py — อัปโหลดกรมธรรม์แบบกอง (โยนหลายไฟล์ทีเดียว)

Flow:  POST /batch/extract  → อัปไฟล์เข้า staging + OCR อ่าน + จัดประเภท + จับคู่
       GET  /batch/{id}     → ดึงผลกลับมาแสดงหน้า review
       POST /batch/{id}/commit → บันทึกเฉพาะรายการที่คนยืนยันแล้วลง DB จริง

ไฟล์ที่อัปจะพักไว้ใน staging (ดิสก์ชั่วคราว) ยังไม่แตะฐานข้อมูลจริง
จนกว่าจะเรียก /commit — ตัวอ่านช่วยเตรียมข้อมูล แต่ผู้ใช้ต้องตรวจหลักฐานก่อนบันทึก
"""
from fastapi import APIRouter, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
import os, json, uuid, asyncio, tempfile, shutil, hashlib, traceback, time
from datetime import datetime, timezone

from services.local_pdf_parser import parse_pdf_image_locally
from services.supabase_shim import create_client
from services import doc_pairing
from routes.upload import (
    _upload_pdf_to_storage, _make_display_filename, ALLOWED_COLUMNS,
    INT_FIELDS, FLOAT_FIELDS, DATE_FIELDS, _clean_thai_number, _normalize_date,
)

router = APIRouter()
_executor = ThreadPoolExecutor(max_workers=1)

STAGING_ROOT = os.path.abspath(os.getenv("BATCH_STAGING_ROOT", os.path.join(os.path.dirname(os.path.dirname(__file__)), ".batch-staging")))
# รับได้สูงสุด 20 ไฟล์ต่อกองเป็นค่าเริ่มต้น; ปรับผ่าน environment ได้
try:
    MAX_FILES = max(1, int(os.getenv("BATCH_MAX_FILES", "20")))
    OCR_CONCURRENCY = max(1, int(os.getenv("BATCH_OCR_CONCURRENCY", "1")))
    BATCH_READ_CHUNK_SIZE = max(1, int(os.getenv("BATCH_READ_CHUNK_SIZE", "10")))
except ValueError:
    MAX_FILES, OCR_CONCURRENCY, BATCH_READ_CHUNK_SIZE = 20, 1, 10

try:
    MAX_PDF_BYTES = max(1, int(os.getenv("MAX_PDF_BYTES", str(12 * 1024 * 1024))))
    MAX_BATCH_BYTES = max(1, int(os.getenv("BATCH_MAX_TOTAL_BYTES", str(100 * 1024 * 1024))))
except ValueError:
    MAX_PDF_BYTES, MAX_BATCH_BYTES = 12 * 1024 * 1024, 100 * 1024 * 1024


@lru_cache(maxsize=1)
def get_supabase():
    return create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))


def _atomic_json(path: str, data: dict) -> None:
    temporary = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


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
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _atomic_json(_manifest_path(batch_id), data)


# ── progress (แยกไฟล์เล็ก ให้ client poll ระหว่างอ่าน) ──────────────
def _progress_path(batch_id: str) -> str:
    return os.path.join(_batch_dir(batch_id), "progress.json")


def _write_progress(batch_id: str, **kw) -> None:
    try:
        _atomic_json(_progress_path(batch_id), kw)
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
    """อ่าน PDF ด้วย OCR ในเครื่อง ไม่เรียกบริการ AI ภายนอก."""
    if rec.get("read_complete"):
        return rec
    if rec["same_file_as"]:
        rec["parsed"] = {}
        rec["parse_error"] = f"ไฟล์ซ้ำกับ {rec['same_file_as']} — ข้ามการอ่าน"
        return rec
    try:
        with open(os.path.join(bdir, f"{rec['file_id']}.pdf"), "rb") as fh:
            blob = fh.read()
        parsed = parse_pdf_image_locally(blob, filename=rec["orig_filename"])
        parsed.pop("preview", None)
    except Exception:
        rec["parsed"] = {}
        rec["parse_error"] = "อ่าน PDF ไม่สำเร็จ กรุณาตรวจไฟล์และตัวอ่าน OCR ในเครื่อง แล้วลองใหม่"
        return rec
    if parsed is None or parsed.get("parse_engine") == "local_ocr_failed":
        rec["parsed"] = parsed or {}
        rec["parse_error"] = "อ่านไม่สำเร็จ กรุณาลองใหม่หรือกรอกข้อมูลจากต้นฉบับ"
    else:
        rec["parsed"] = parsed
        rec["parsed"]["requires_review"] = True
        rec.pop("parse_error", None)
    return rec


async def _process_batch(batch_id: str, bdir: str, staged: list[dict]) -> None:
    """อ่านทุกไฟล์เป็นรอบเล็ก ๆ → จับคู่ทั้งกองหลังอ่านครบ → เขียน manifest.

    การแบ่งรอบมีไว้ควบคุม RAM/โควตาบน Render Free เท่านั้น ไม่ได้จับคู่ทีละรอบ
    จึงยังจับคู่เอกสารที่ผู้ใช้อัปคนละรอบ (เช่น 10 + 10 + 10) ได้ครบถ้วน.
    """
    total = len(staged)
    try:
        loop = asyncio.get_event_loop()
        done = 0
        sem = asyncio.Semaphore(OCR_CONCURRENCY)
        total_chunks = max(1, (total + BATCH_READ_CHUNK_SIZE - 1) // BATCH_READ_CHUNK_SIZE)

        async def _one(r, chunk_no):
            nonlocal done
            # แจ้งชื่อไฟล์ทันทีที่เริ่มอ่าน OCR — ตัวอ่าน อาจใช้เวลาหลายสิบวินาทีกับ
            # ไฟล์แรก แต่ผู้ใช้จะเห็นว่างานกำลังดำเนินอยู่ ไม่ใช่ค้างที่ 0/ทั้งหมด
            _write_progress(batch_id, status="processing", done=done, total=total,
                            current=r.get("orig_filename"), phase="reading",
                            chunk=chunk_no, chunk_total=total_chunks,
                            chunk_size=BATCH_READ_CHUNK_SIZE)
            async with sem:
                await loop.run_in_executor(_executor, _read_one_file, bdir, r)
            r["read_complete"] = True
            manifest = _load_manifest(batch_id)
            manifest["files"] = staged
            _save_manifest(batch_id, manifest)
            done += 1
            _write_progress(batch_id, status="processing", done=done, total=total,
                            current=r.get("orig_filename"), phase="completed", chunk=chunk_no,
                            chunk_total=total_chunks, chunk_size=BATCH_READ_CHUNK_SIZE)

        for start in range(0, total, BATCH_READ_CHUNK_SIZE):
            chunk_no = start // BATCH_READ_CHUNK_SIZE + 1
            chunk = staged[start:start + BATCH_READ_CHUNK_SIZE]
            await asyncio.gather(*[_one(r, chunk_no) for r in chunk])

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
        manifest = _load_manifest(batch_id)
        manifest.update(files=staged, result=result)
        _save_manifest(batch_id, manifest)
        _write_progress(batch_id, status="done", done=total, total=total, current=None,
                        chunk=total_chunks, chunk_total=total_chunks, chunk_size=BATCH_READ_CHUNK_SIZE)
    except Exception as e:
        print("[batch-process] ERROR:\n", traceback.format_exc())
        _write_progress(batch_id, status="error", done=0, total=total, current=None, error=str(e)[:200])


@router.on_event("startup")
async def resume_staged_batches():
    """Resume unfinished work from the configured persistent staging volume."""
    if not os.path.isdir(STAGING_ROOT):
        return
    for batch_id in os.listdir(STAGING_ROOT):
        try:
            manifest = _load_manifest(batch_id)
            if manifest.get("result") is None and manifest.get("files"):
                asyncio.create_task(_process_batch(batch_id, _batch_dir(batch_id), manifest["files"]))
        except (HTTPException, OSError, ValueError):
            continue


# ── 1) อัป + อ่าน + จับคู่ ──────────────────────────────────────────
@router.post("/batch/extract")
async def batch_extract(files: list[UploadFile] = File(...)):
    """รับไฟล์ทีละสูงสุด 20 ไฟล์ → OCR อ่านทุกไฟล์ → จัดประเภท → จับคู่ กธ↔พรบ
    ยังไม่บันทึกลงฐานข้อมูล"""
    if not files:
        raise HTTPException(status_code=400, detail="ไม่พบไฟล์")
    if MAX_FILES and len(files) > MAX_FILES:
        raise HTTPException(status_code=400,
                            detail=f"อัปได้สูงสุด {MAX_FILES} ไฟล์ต่อครั้ง (ส่งมา {len(files)})")
    known_sizes = [f.size for f in files if f.size is not None]
    if any(size > MAX_PDF_BYTES for size in known_sizes):
        raise HTTPException(status_code=413, detail="มีไฟล์ PDF ใหญ่เกินกำหนด 12 MB กรุณาลดขนาดไฟล์ก่อนอัปโหลด")
    if known_sizes and sum(known_sizes) > MAX_BATCH_BYTES:
        raise HTTPException(status_code=413, detail="ขนาดรวมของกองไฟล์เกิน 100 MB กรุณาแบ่งอัปโหลดเป็นหลายกอง")

    batch_id = uuid.uuid4().hex[:12]
    bdir = _batch_dir(batch_id)
    os.makedirs(bdir, exist_ok=True)

    # ── เก็บไฟล์เข้า staging ก่อน (พร้อม hash กันไฟล์ซ้ำ) ──
    staged, seen_hashes = [], {}
    total_bytes = 0
    for i, f in enumerate(files):
        if not (f.filename or "").lower().endswith(".pdf"):
            continue
        data = await f.read(MAX_PDF_BYTES + 1)
        total_bytes += len(data)
        if total_bytes > MAX_BATCH_BYTES:
            raise HTTPException(status_code=413, detail="ขนาดรวมของชุดเกินกำหนด")
        if len(data) > MAX_PDF_BYTES:
            raise HTTPException(status_code=413, detail=f"ไฟล์ {f.filename} ใหญ่เกินกำหนด 12 MB")
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
    now = datetime.now(timezone.utc).isoformat()
    _save_manifest(batch_id, {"batch_id": batch_id, "created_at": now,
                              "files": staged, "result": None})
    _write_progress(batch_id, status="processing", done=0, total=len(staged), current=None,
                    chunk=0, chunk_total=max(1, (len(staged) + BATCH_READ_CHUNK_SIZE - 1) // BATCH_READ_CHUNK_SIZE),
                    chunk_size=BATCH_READ_CHUNK_SIZE)
    asyncio.create_task(_process_batch(batch_id, bdir, staged))

    return {"success": True, "batch_id": batch_id, "total": len(staged), "status": "processing"}


# ── 1.5) ถามความคืบหน้าระหว่างอ่าน ─────────────────────────────────
@router.get("/batch/history")
async def batch_history():
    """รายการกองล่าสุดบน instance นี้ โดยไม่ส่งข้อมูล OCR ทั้งหมดกลับไป."""
    if not os.path.isdir(STAGING_ROOT):
        return {"success": True, "items": []}
    items = []
    for entry in os.scandir(STAGING_ROOT):
        if not entry.is_dir():
            continue
        try:
            manifest = _load_manifest(entry.name)
            progress = _read_progress(entry.name)
            result = manifest.get("result") or {}
            summary = result.get("summary") or {}
            committed = result.get("committed")
            status = "committed" if committed else progress.get("status", "processing")
            if status == "done":
                status = "review"
            items.append({
                "batch_id": entry.name,
                "created_at": manifest.get("created_at"),
                "updated_at": manifest.get("updated_at"),
                "total": len(manifest.get("files") or []),
                "status": status,
                "progress": {"done": progress.get("done", 0), "total": progress.get("total", 0)},
                "summary": summary,
                "committed": committed,
            })
        except Exception:
            continue
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return {"success": True, "items": items[:50]}


@router.get("/batch/{batch_id}/progress")
async def batch_progress(batch_id: str):
    return {"success": True, **_read_progress(batch_id)}


# ── 2) ดึงผลกลับมาแสดง ─────────────────────────────────────────────
@router.get("/batch/{batch_id}")
async def batch_get(batch_id: str):
    m = _load_manifest(batch_id)
    return {"success": True, "batch_id": batch_id, **m["result"]}


@router.get("/batch/{batch_id}/files/{file_id}/pdf")
async def batch_preview_pdf(batch_id: str, file_id: str):
    """ส่ง PDF จาก staging สำหรับตรวจผล OCR ก่อน commit.

    ไฟล์ยังอยู่เฉพาะ temporary batch directory และ endpoint จะอนุญาตเฉพาะ
    file_id ที่ระบุไว้ใน manifest ของกองนั้น จึงไม่สามารถใช้ path traversal
    เพื่ออ่านไฟล์อื่นบน server ได้.
    """
    manifest = _load_manifest(batch_id)
    staged = next((item for item in manifest.get("files", []) if item.get("file_id") == file_id), None)
    if not staged:
        raise HTTPException(status_code=404, detail="ไม่พบไฟล์ในกองนี้")

    safe_id = os.path.basename(file_id)
    path = os.path.join(_batch_dir(batch_id), f"{safe_id}.pdf")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="ไฟล์ชั่วคราวหมดอายุหรือถูกล้างแล้ว")

    filename = os.path.basename(staged.get("orig_filename") or f"{safe_id}.pdf")
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


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


SUPPORT_DOCUMENT_TYPES = {
    doc_pairing.MOTOR_PRB,
    doc_pairing.RENEWAL_NOTICE,
    doc_pairing.ENDORSEMENT,
    doc_pairing.CREDIT_NOTE,
    doc_pairing.INVOICE,
    doc_pairing.RECEIPT,
    doc_pairing.UNKNOWN,
}


def _coverage_year(value):
    text = str(value or "")
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else None


def _find_parent_policy(supabase, record: dict, document_type: str):
    """Return one unambiguous parent policy; never choose between ties."""
    columns = "id, created_at, policy_number, company_code, license_plate, chassis_no, policy_type, coverage_start"
    if document_type != doc_pairing.MOTOR_PRB and record.get("policy_number"):
        query = supabase.table("insurance_policies").select(columns).eq(
            "policy_number", record["policy_number"]
        )
        if record.get("company_code"):
            query = query.eq("company_code", record["company_code"])
        rows = query.order("created_at", desc=True).range(0, 1).execute().data or []
        return rows[0] if len(rows) == 1 else None

    if document_type != doc_pairing.MOTOR_PRB:
        return None
    query = supabase.table("insurance_policies").select(columns).in_("policy_type", ["M", "STY"])
    chassis = doc_pairing.norm_chassis(record.get("chassis_no"))
    plate = doc_pairing.norm_plate(record.get("license_plate"))
    if chassis:
        query = query.raw_filter(
            "REPLACE(REPLACE(UPPER(chassis_no), ' ', ''), '-', '') = %s", [chassis]
        )
    elif plate:
        query = query.raw_filter(
            "REPLACE(REPLACE(license_plate, ' ', ''), '-', '') = %s", [plate]
        )
    else:
        return None
    rows = query.order("created_at", desc=True).range(0, 20).execute().data or []
    year = _coverage_year(record.get("coverage_start"))
    same_year = [row for row in rows if not year or _coverage_year(row.get("coverage_start")) == year]
    return same_year[0] if len(same_year) == 1 else None


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

    # Validate the entire selection before any storage or database mutation.
    staged_ids = {record["file_id"] for record in m["files"]}
    selected_ids = set()
    for item in items:
        for key in ("main_file_id", "prb_file_id"):
            file_id = item.get(key)
            if key == "prb_file_id" and not file_id:
                continue
            if file_id not in staged_ids or file_id in selected_ids:
                raise HTTPException(status_code=400, detail="ไฟล์ไม่อยู่ในชุดนี้หรือถูกเลือกซ้ำ")
            selected_ids.add(file_id)
        raw_main = item.get("main") or {}
        main = _clean_for_db(raw_main)
        document_type = raw_main.get("doc_type") or doc_pairing.UNKNOWN
        if document_type in SUPPORT_DOCUMENT_TYPES:
            if "doc_type" not in raw_main:
                raise HTTPException(status_code=422, detail="กรุณาระบุประเภทเอกสารก่อนบันทึก")
            if document_type == doc_pairing.RENEWAL_NOTICE and not all(main.get(key) for key in ("policy_number", "coverage_start", "coverage_end")):
                raise HTTPException(status_code=422, detail="หนังสือแจ้งเตือนต่ออายุต้องมีเลขกรมธรรม์อ้างอิงและช่วงคุ้มครองใหม่")
            if not item.get("main_file_id"):
                raise HTTPException(status_code=422, detail="เอกสารประกอบต้องมีไฟล์ PDF")
            if main.get("coverage_start") and main.get("coverage_end") and main["coverage_start"] >= main["coverage_end"]:
                raise HTTPException(status_code=422, detail="วันสิ้นสุดต้องอยู่หลังวันเริ่มคุ้มครอง")
            continue
        if not all(main.get(key) for key in ("policy_number", "insured_name", "coverage_start", "coverage_end")):
            raise HTTPException(status_code=422, detail="ตรวจเลขกรมธรรม์ ชื่อ และวันคุ้มครองให้ครบก่อนบันทึก")
        if main["coverage_start"] >= main["coverage_end"]:
            raise HTTPException(status_code=422, detail="วันสิ้นสุดต้องอยู่หลังวันเริ่มคุ้มครอง")
        prb = _clean_for_db(item.get("prb") or {})
        if prb and doc_pairing.score_pair(main, prb)[0] < 0:
            raise HTTPException(status_code=422, detail="กธ. และ พ.ร.บ. มีเลขตัวถังหรือปีคุ้มครองขัดกัน")

    supabase = get_supabase()
    loop = asyncio.get_event_loop()
    created, attached, inbox, failed = [], [], [], []

    for it in items:
        raw_main = it.get("main") or {}
        main = _clean_for_db(raw_main)
        if not main:
            failed.append({"reason": "ข้อมูลกรมธรรม์ว่าง", "item": it})
            continue
        main["manually_edited"] = True
        source = next((f for f in m["files"] if f["file_id"] == it.get("main_file_id")), {})
        main["original_filename"] = source.get("orig_filename")
        try:
            document_type = raw_main.get("doc_type") or doc_pairing.UNKNOWN
            if document_type in SUPPORT_DOCUMENT_TYPES:
                parent = _find_parent_policy(supabase, main, document_type)
                mf = it.get("main_file_id")
                if document_type in {doc_pairing.MOTOR_PRB, doc_pairing.RENEWAL_NOTICE} and parent:
                    generated_name = _make_display_filename(
                        plate=main.get("license_plate") or parent.get("license_plate"),
                        doc_type="prb" if document_type == doc_pairing.MOTOR_PRB else "renewal_notice",
                        coverage_start=main.get("coverage_start"),
                        coverage_end=main.get("coverage_end"),
                        policy_type=parent.get("policy_type"),
                    )
                    fname = generated_name if generated_name != "รอตรวจข้อมูล.pdf" else (source.get("orig_filename") or generated_name)
                else:
                    fname = source.get("orig_filename") or f"{document_type}.pdf"
                with open(os.path.join(bdir, f"{os.path.basename(mf)}.pdf"), "rb") as fh:
                    blob = fh.read()
                url = await loop.run_in_executor(
                    _executor, lambda: _upload_pdf_to_storage(supabase, blob, fname))
                attachment_type = "prb" if document_type == doc_pairing.MOTOR_PRB else document_type
                labels = {
                    "prb": "พ.ร.บ.",
                    "renewal_notice": "หนังสือแจ้งเตือนต่ออายุ",
                    "endorsement": "สลักหลัง",
                    "credit_note": "ใบลดหนี้ / ใบคืนเบี้ย",
                    "invoice": "ใบแจ้งหนี้",
                    "receipt": "ใบเสร็จรับเงิน",
                }
                if parent and document_type != doc_pairing.UNKNOWN:
                    attachment = {
                        "policy_id": parent["id"],
                        "doc_type": attachment_type,
                        "label": labels.get(attachment_type, "เอกสารประกอบ"),
                        "note": f"อ้างอิงเลขกรมธรรม์ {main.get('policy_number') or parent.get('policy_number')}",
                        "pdf_url": url,
                        "pdf_filename": fname,
                        "pdf_size": len(blob),
                        "coverage_start": main.get("coverage_start"),
                        "coverage_end": main.get("coverage_end"),
                    }
                    for key in ("net_premium", "stamp_duty", "vat", "total_premium"):
                        if main.get(key) is not None:
                            attachment[key] = main[key]
                    saved = supabase.table("policy_attachments").insert(attachment).execute()
                    attached.append({
                        "policy_id": parent["id"],
                        "attachment_id": saved.data[0]["id"] if saved.data else None,
                        "document_type": attachment_type,
                    })
                else:
                    inbox_payload = {
                        "document_type": document_type,
                        "status": "needs_review",
                        "reference_policy_number": main.get("policy_number"),
                        "insured_name": main.get("insured_name"),
                        "license_plate": main.get("license_plate"),
                        "coverage_start": main.get("coverage_start"),
                        "coverage_end": main.get("coverage_end"),
                        "extracted_data": raw_main,
                        "pdf_url": url,
                        "pdf_filename": fname,
                        "pdf_size": len(blob),
                        "original_filename": source.get("orig_filename"),
                    }
                    saved = supabase.table("document_inbox").insert(inbox_payload).execute()
                    inbox.append({
                        "document_id": saved.data[0]["id"] if saved.data else None,
                        "document_type": document_type,
                    })
                continue

            # อัปไฟล์ กธ ขึ้น storage (ถ้ามี)
            mf = it.get("main_file_id")
            if mf:
                fname = _make_display_filename(
                    plate=main.get("license_plate"), doc_type="main",
                    coverage_start=main.get("coverage_start"),
                    coverage_end=main.get("coverage_end"),
                    policy_type=main.get("policy_type"),
                    risk_address=main.get("risk_address"), name=main.get("insured_name"))
                if fname == "รอตรวจข้อมูล.pdf":
                    raise ValueError("ข้อมูลตั้งชื่อไฟล์ไม่ครบ กรุณาตรวจทะเบียน วันเริ่มคุ้มครอง หรือสถานที่เอาประกัน")
                with open(os.path.join(bdir, f"{os.path.basename(mf)}.pdf"), "rb") as fh:
                    blob = fh.read()
                url = await loop.run_in_executor(
                    _executor, lambda: _upload_pdf_to_storage(supabase, blob, fname))
                main.update({"pdf_url": url, "pdf_filename": fname, "pdf_size": len(blob)})

            att = None

            # แนบ พ.ร.บ.
            prb = _clean_for_db(it.get("prb") or {})
            pf = it.get("prb_file_id")
            if prb or pf:
                att = {"doc_type": "prb", "label": "พ.ร.บ."}
                for key in ("net_premium", "stamp_duty", "vat", "total_premium",
                            "coverage_start", "coverage_end"):
                    if prb.get(key) is not None:
                        att[key] = prb[key]
                if pf:
                    pname = _make_display_filename(
                        plate=(prb.get("license_plate") or main.get("license_plate")),
                        doc_type="prb", coverage_start=prb.get("coverage_start"),
                        coverage_end=prb.get("coverage_end"))
                    with open(os.path.join(bdir, f"{os.path.basename(pf)}.pdf"), "rb") as fh:
                        pblob = fh.read()
                    purl = await loop.run_in_executor(
                        _executor, lambda: _upload_pdf_to_storage(supabase, pblob, pname))
                    att.update({"pdf_url": purl, "pdf_filename": pname,
                                "pdf_size": len(pblob)})
            from services.batch_persistence import insert_policy_pair
            policy_id = await loop.run_in_executor(_executor, insert_policy_pair, main, att)

            created.append({"policy_id": policy_id,
                            "license_plate": main.get("license_plate")})
        except Exception as e:
            print("[batch-commit] ERROR:\n", traceback.format_exc())
            failed.append({"reason": str(e)[:200],
                           "license_plate": main.get("license_plate")})

    m["result"]["committed"] = {
        "created": len(created), "attached": len(attached),
        "inbox": len(inbox), "failed": len(failed),
    }
    _save_manifest(batch_id, m)
    return {"success": True, "created": created, "attached": attached, "inbox": inbox, "failed": failed,
            "summary": {"created": len(created), "attached": len(attached),
                        "inbox": len(inbox), "failed": len(failed)}}


# ── 4) ล้าง staging ────────────────────────────────────────────────
@router.delete("/batch/{batch_id}")
async def batch_discard(batch_id: str):
    shutil.rmtree(_batch_dir(batch_id), ignore_errors=True)
    return {"success": True}
