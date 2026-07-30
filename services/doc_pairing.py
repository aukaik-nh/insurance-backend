"""
doc_pairing.py — จัดประเภทเอกสาร + จับคู่ กธ ↔ พ.ร.บ. สำหรับการอัปโหลดแบบกอง

ใช้กับ flow ใหม่: โยน PDF เป็นร้อยไฟล์ทีเดียว → AI อ่านทีละไฟล์ → โมดูลนี้จับคู่ให้
→ คนตรวจในหน้า review → ค่อย commit ลง DB

กุญแจจับคู่ (เรียงตามความน่าเชื่อถือ — พิสูจน์จากไฟล์จริงชุด 2569-07-22):
  1. เลขตัวถัง (chassis_no)  — ไม่ซ้ำ ไม่เปลี่ยน อยู่บนทั้ง กธ และ พ.ร.บ.
  2. ทะเบียน + ปีคุ้มครอง     — ใช้เมื่ออ่านเลขตัวถังไม่ออก
  3. ชื่อผู้เอาประกัน + ปี     — ทางสำรองสุดท้าย

หมายเหตุสำคัญจากข้อมูลจริง: วันคุ้มครองของ กธ กับ พ.ร.บ. ของคันเดียวกัน
"ไม่จำเป็นต้องตรงกัน" (เช่น กธ 3 ก.ค. / พ.ร.บ. 15 ก.ค.) → เทียบระดับ "ปี" ไม่ใช่วันเป๊ะ
"""
import re
from difflib import SequenceMatcher

from services.filename_matcher_v2 import norm_plate, coverage_year_ad


# ── ชนิดเอกสาร ────────────────────────────────────────────────────
MOTOR_MAIN   = "motor_main"      # ตารางกรมธรรม์ประกันภัยรถยนต์ (กธ)
MOTOR_PRB    = "motor_prb"       # คุ้มครองผู้ประสบภัยจากรถ (พ.ร.บ.)
ENDORSEMENT  = "endorsement"     # สลักหลัง / ยกเลิก (ร.ย.11)
CREDIT_NOTE  = "credit_note"     # ใบลดหนี้ / ใบคืนเบี้ย
FIRE         = "fire"            # อัคคีภัย
SME_PROPERTY = "sme_property"    # ประกันธุรกิจ SME / ทรัพย์สิน
UNKNOWN      = "unknown"

PAIRABLE = {MOTOR_MAIN, MOTOR_PRB}

# ── เกณฑ์คะแนน ────────────────────────────────────────────────────
SCORE_CHASSIS_EXACT = 50
SCORE_PLATE_MATCH   = 30
SCORE_YEAR_MATCH    = 15
SCORE_NAME_SIMILAR  = 10
SCORE_CAR_MATCH     = 5
PENALTY_CHASSIS_DIFF = -40   # มีเลขตัวถังทั้งคู่แต่คนละเลข = คนละคันแน่นอน

THRESHOLD_AUTO   = 50        # >= จับอัตโนมัติ
THRESHOLD_REVIEW = 30        # >= ให้คนยืนยัน, ต่ำกว่านี้ = กำพร้า


# ── normalize ─────────────────────────────────────────────────────
def norm_chassis(s: str | None) -> str:
    """เลขตัวถัง: ตัดช่องว่าง/ขีด แล้ว uppercase"""
    if not s:
        return ""
    return re.sub(r"[\s\-]", "", str(s)).upper()


def norm_name(s: str | None) -> str:
    """ชื่อ: ตัดคำนำหน้า + ช่องว่าง เพื่อเทียบแบบหลวม"""
    if not s:
        return ""
    s = str(s)
    for prefix in ("บริษัท", "หจก.", "ห้างหุ้นส่วนจำกัด", "นางสาว", "นาง", "นาย",
                   "คุณ", "นพ.", "พญ.", "ดร."):
        s = s.replace(prefix, "")
    return re.sub(r"\s+", "", s).lower()


def _name_similarity(a: str | None, b: str | None) -> float:
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _car_key(rec: dict) -> str:
    """ยี่ห้อ+รุ่น แบบหลวม ๆ ไว้ช่วยแก้ tie"""
    parts = [rec.get("car_make") or "", rec.get("car_model") or "", rec.get("car") or ""]
    return re.sub(r"\s+", "", " ".join(parts)).upper()


def _year(rec: dict) -> int | None:
    """ปีคุ้มครอง (ค.ศ.) — รองรับทั้ง พ.ศ. และ ค.ศ."""
    return coverage_year_ad(rec)


# ── จัดประเภท (fallback เมื่อ AI ไม่ได้บอก doc_type มา) ──────────────
_TITLE_RULES = [
    (MOTOR_PRB,    ("ผู้ประสบภัยจากรถ", "คุ้มครองผู้ประสบภัย")),
    (MOTOR_MAIN,   ("ประกันภัยรถยนต์", "MOTOR INSURANCE SCHEDULE", "Safety 4U")),
    (ENDORSEMENT,  ("สลักหลัง", "ยกเลิกกรมธรรม์", "ร.ย.11", "ร.ย. 11")),
    (CREDIT_NOTE,  ("ใบลดหนี้", "ใบคืนเบี้ย", "CREDIT NOTE", "CREDIT-NOTE")),
    (SME_PROPERTY, ("สรรพธุรกิจ", "SME INSURANCE")),
    (FIRE,         ("อัคคีภัย", "FIRE INSURANCE")),
]


def classify(rec: dict) -> str:
    """คืน doc_type — ถ้า AI ส่ง doc_type มาแล้วใช้เลย ไม่งั้นเดาจากหัวเอกสาร/เลขกรมธรรม์"""
    given = (rec.get("doc_type") or "").strip()
    if given:
        return given

    haystack = " ".join(str(rec.get(k) or "") for k in
                        ("title", "raw_text", "doc_title", "policy_type"))
    for doc_type, keywords in _TITLE_RULES:
        if any(kw in haystack for kw in keywords):
            return doc_type

    # เดาจากรูปแบบเลขกรมธรรม์ (Tokio Marine): D0-70=รถยนต์, D0-72=พ.ร.บ.,
    # D0-10=อัคคีภัย, D0-11=SME
    pol = str(rec.get("policy_number") or "")
    for prefix, doc_type in (("-72-", MOTOR_PRB), ("-70-", MOTOR_MAIN),
                             ("-10-", FIRE), ("-11-", SME_PROPERTY)):
        if prefix in pol:
            return doc_type

    return UNKNOWN


# ── ให้คะแนนคู่ ───────────────────────────────────────────────────
def score_pair(main: dict, prb: dict) -> tuple[int, list[str]]:
    """คืน (คะแนน, เหตุผล) — ยิ่งสูงยิ่งมั่นใจว่าเป็นรถคันเดียวกัน"""
    score, why = 0, []

    cm, cp = norm_chassis(main.get("chassis_no")), norm_chassis(prb.get("chassis_no"))
    if cm and cp:
        if cm == cp:
            score += SCORE_CHASSIS_EXACT
            why.append("เลขตัวถังตรง")
        else:
            score += PENALTY_CHASSIS_DIFF
            why.append("เลขตัวถังไม่ตรง")

    pm, pp = norm_plate(main.get("license_plate")), norm_plate(prb.get("license_plate"))
    if pm and pp and pm == pp:
        score += SCORE_PLATE_MATCH
        why.append("ทะเบียนตรง")

    ym, yp = _year(main), _year(prb)
    if ym and yp and ym == yp:
        score += SCORE_YEAR_MATCH
        why.append("ปีคุ้มครองตรง")

    if _name_similarity(main.get("insured_name"), prb.get("insured_name")) >= 0.85:
        score += SCORE_NAME_SIMILAR
        why.append("ชื่อผู้เอาประกันตรง")

    km, kp = _car_key(main), _car_key(prb)
    if km and kp and (km in kp or kp in km):
        score += SCORE_CAR_MATCH
        why.append("รุ่นรถตรง")

    return score, why


# ── จับคู่ทั้งกอง ──────────────────────────────────────────────────
def pair_documents(records: list[dict]) -> dict:
    """จับคู่ กธ ↔ พ.ร.บ. ทั้งกอง (greedy by score — คู่คะแนนสูงสุดได้จับก่อน)

    records: list ของ dict ที่ AI อ่านมา อย่างน้อยควรมี
             doc_type, chassis_no, license_plate, insured_name, coverage_start
             และ key ประจำไฟล์ (ใช้ 'idx' หรือ 'file_id')

    return: {
      "pairs":        [{main, prb, score, status, reasons}],
      "orphan_main":  [rec, ...],   # กธ ที่ยังไม่มี พ.ร.บ.
      "orphan_prb":   [rec, ...],   # พ.ร.บ. ที่ยังไม่มี กธ
      "others":       [rec, ...],   # เอกสารชนิดอื่น (สลักหลัง/ใบลดหนี้/อัคคีภัย/SME)
      "summary":      {...}
    }
    """
    for r in records:
        r["doc_type"] = classify(r)

    mains  = [r for r in records if r["doc_type"] == MOTOR_MAIN]
    prbs   = [r for r in records if r["doc_type"] == MOTOR_PRB]
    others = [r for r in records if r["doc_type"] not in PAIRABLE]

    # คิดคะแนนทุกคู่ที่เป็นไปได้ แล้วเรียงจากมากไปน้อย
    candidates = []
    for m in mains:
        for p in prbs:
            score, why = score_pair(m, p)
            if score >= THRESHOLD_REVIEW:
                candidates.append((score, why, m, p))
    candidates.sort(key=lambda c: c[0], reverse=True)

    used_main, used_prb, pairs = set(), set(), []
    for score, why, m, p in candidates:
        mk, pk = id(m), id(p)
        if mk in used_main or pk in used_prb:
            continue          # ตัวใดตัวหนึ่งถูกจับไปแล้ว
        used_main.add(mk)
        used_prb.add(pk)
        pairs.append({
            "main":    m,
            "prb":     p,
            "score":   score,
            "reasons": why,
            "status":  "auto" if score >= THRESHOLD_AUTO else "review",
        })

    orphan_main = [m for m in mains if id(m) not in used_main]
    orphan_prb  = [p for p in prbs  if id(p) not in used_prb]

    return {
        "pairs":       pairs,
        "orphan_main": orphan_main,
        "orphan_prb":  orphan_prb,
        "others":      others,
        "summary": {
            "total":        len(records),
            "pairs":        len(pairs),
            "auto":         sum(1 for x in pairs if x["status"] == "auto"),
            "need_review":  sum(1 for x in pairs if x["status"] == "review"),
            "orphan_main":  len(orphan_main),
            "orphan_prb":   len(orphan_prb),
            "others":       len(others),
        },
    }


# ── กันไฟล์ซ้ำ ────────────────────────────────────────────────────
def dedupe(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """รวมสำเนาซ้ำ (เลขกรมธรรม์ + ชนิด + ยอดรวม ตรงกัน = ใบเดียวกัน)
    เจอจริงในชุดตัวอย่าง: ใบลดหนี้ใบเดียวกันถูกสแกนมา 3 สำเนา
    return (unique, duplicates)"""
    seen, unique, dups = {}, [], []
    for r in records:
        key = (str(r.get("policy_number") or "").strip(),
               classify(r),
               str(r.get("total_premium") or ""))
        if key in seen and key[0]:
            r["duplicate_of"] = seen[key].get("idx") or seen[key].get("file_id")
            dups.append(r)
        else:
            seen[key] = r
            unique.append(r)
    return unique, dups
