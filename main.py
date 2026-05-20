from fastapi import FastAPI, Request, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from routes import upload, policies, invoice, attachments, auth as auth_routes
from routes.auth import require_auth
from services.line_notify import start_scheduler
from dotenv import load_dotenv
import os, psycopg2

load_dotenv()

# ── Auto-migration: สร้างตารางอัตโนมัติถ้ายังไม่มี ───────────────
_INIT_SQL = """
CREATE TABLE IF NOT EXISTS public.insurance_policies (
  id                       uuid DEFAULT gen_random_uuid() PRIMARY KEY,
  created_at               timestamptz DEFAULT now(),
  policy_number            varchar,
  company_code             varchar,
  insured_name             varchar,
  insured_address          text,
  license_plate            varchar,
  chassis_no               varchar,
  car_make                 varchar,
  car_model                varchar,
  car_year                 integer,
  coverage_start           varchar,
  coverage_end             varchar,
  net_premium              numeric,
  stamp_duty               numeric,
  vat                      numeric,
  total_premium            numeric,
  third_party_per_person   numeric,
  third_party_per_accident numeric,
  own_damage               numeric,
  broker_name              varchar,
  broker_license           varchar,
  manually_edited          boolean DEFAULT false,
  pdf_url                  text,
  pdf_filename             varchar,
  pdf_data                 text,
  pdf_size                 integer
);

-- เพิ่ม column ที่ขาดหาย (ปลอดภัยถ้ามีอยู่แล้ว)
ALTER TABLE public.insurance_policies
  ADD COLUMN IF NOT EXISTS phone                varchar,
  ADD COLUMN IF NOT EXISTS notes                text,
  ADD COLUMN IF NOT EXISTS app_number           varchar,
  ADD COLUMN IF NOT EXISTS policy_type          varchar,
  ADD COLUMN IF NOT EXISTS new_renew            varchar,
  ADD COLUMN IF NOT EXISTS agent_code           varchar,
  ADD COLUMN IF NOT EXISTS license_province     varchar,
  ADD COLUMN IF NOT EXISTS sum_insured          numeric,
  ADD COLUMN IF NOT EXISTS date_notify          date,
  ADD COLUMN IF NOT EXISTS date_cancel          date,
  ADD COLUMN IF NOT EXISTS date_policy_receive  date,
  -- ฟิลด์ commission/หัก ณ ที่จ่าย/ปัดเศษ (ตามฟอร์มระบบเก่า)
  ADD COLUMN IF NOT EXISTS prepaid_tax_1pct     numeric,
  ADD COLUMN IF NOT EXISTS commission_pct       numeric,
  ADD COLUMN IF NOT EXISTS commission_baht      numeric,
  ADD COLUMN IF NOT EXISTS wht_10pct            numeric,
  ADD COLUMN IF NOT EXISTS rounding             numeric,
  ADD COLUMN IF NOT EXISTS collected_amount     numeric;

ALTER TABLE public.insurance_policies ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'insurance_policies'
      AND policyname = 'allow_all'
  ) THEN
    EXECUTE 'CREATE POLICY allow_all ON public.insurance_policies
             FOR ALL USING (true) WITH CHECK (true)';
  END IF;
END
$$;

-- เอกสารแนบหลายไฟล์ต่อกรมธรรม์ (พ.ร.บ. / สลักหลัง / อื่นๆ)
CREATE TABLE IF NOT EXISTS public.policy_attachments (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  policy_id     uuid NOT NULL REFERENCES public.insurance_policies(id) ON DELETE CASCADE,
  doc_type      text NOT NULL CHECK (doc_type IN ('main','prb','endorsement','other')),
  label         text,
  pdf_url       text,
  pdf_filename  text,
  pdf_size      integer,
  note          text,
  -- ฟิลด์เบี้ย (ใช้กับ พ.ร.บ. ที่ต่อทุกปี)
  net_premium    numeric,
  stamp_duty     numeric,
  vat            numeric,
  total_premium  numeric,
  coverage_start date,
  coverage_end   date,
  created_at     timestamptz DEFAULT now()
);
-- เผื่อ table เคยสร้างไว้แล้ว ให้เพิ่มคอลัมน์เบี้ยที่ขาด
ALTER TABLE public.policy_attachments
  ADD COLUMN IF NOT EXISTS net_premium    numeric,
  ADD COLUMN IF NOT EXISTS stamp_duty     numeric,
  ADD COLUMN IF NOT EXISTS vat            numeric,
  ADD COLUMN IF NOT EXISTS total_premium  numeric,
  ADD COLUMN IF NOT EXISTS coverage_start date,
  ADD COLUMN IF NOT EXISTS coverage_end   date;

CREATE INDEX IF NOT EXISTS policy_attachments_policy_id_idx
  ON public.policy_attachments(policy_id);
CREATE INDEX IF NOT EXISTS policy_attachments_type_idx
  ON public.policy_attachments(policy_id, doc_type);

ALTER TABLE public.policy_attachments ENABLE ROW LEVEL SECURITY;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_policies
    WHERE schemaname = 'public'
      AND tablename  = 'policy_attachments'
      AND policyname = 'allow_all'
  ) THEN
    EXECUTE 'CREATE POLICY allow_all ON public.policy_attachments
             FOR ALL USING (true) WITH CHECK (true)';
  END IF;
END
$$;
"""

def _run_migrations():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("[migration] DATABASE_URL ไม่พบ — ข้ามการสร้างตาราง")
        return
    try:
        conn = psycopg2.connect(db_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute(_INIT_SQL)
        conn.close()
        print("[migration] ✓ ตรวจสอบ / สร้างตาราง insurance_policies เรียบร้อย")
    except Exception as e:
        print(f"[migration] WARNING: {e}")

_run_migrations()

app = FastAPI(title="Insurance API", version="1.0.0")

@app.on_event("startup")
async def startup():
    start_scheduler()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "https://insuremgr.vercel.app", "https://safetypc.vercel.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global handler — ให้ CORS header ติดมาแม้ 500
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
        headers={"Access-Control-Allow-Origin": "*"},
    )

_auth = [Depends(require_auth)]

app.include_router(auth_routes.router, prefix="/api", tags=["Auth"])
app.include_router(upload.router,      prefix="/api", tags=["Upload"],      dependencies=_auth)
app.include_router(policies.router,    prefix="/api", tags=["Policies"],    dependencies=_auth)
app.include_router(invoice.router,     prefix="/api", tags=["Invoice"],     dependencies=_auth)
app.include_router(attachments.router, prefix="/api", tags=["Attachments"], dependencies=_auth)

@app.get("/")
def root():
    return {"message": "Insurance API is running"}
