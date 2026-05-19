-- เอกสารแนบหลายไฟล์ต่อกรมธรรม์ (พ.ร.บ. / สลักหลัง / อื่นๆ)
-- รัน 1 ครั้งใน Supabase SQL editor

create table if not exists policy_attachments (
  id           uuid primary key default gen_random_uuid(),
  policy_id    uuid not null references insurance_policies(id) on delete cascade,
  doc_type     text not null check (doc_type in ('main','prb','endorsement','other')),
  label        text,                       -- ป้ายกำกับเพิ่ม เช่น "พ.ร.บ. ปี 2569", "เปลี่ยนชื่อผู้ขับ"
  pdf_url      text,
  pdf_filename text,
  pdf_size     integer,
  note         text,
  created_at   timestamptz default now()
);

create index if not exists policy_attachments_policy_id_idx
  on policy_attachments(policy_id);

create index if not exists policy_attachments_doc_type_idx
  on policy_attachments(policy_id, doc_type);
