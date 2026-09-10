create table if not exists document_inbox (
  id                      uuid primary key default gen_random_uuid(),
  created_at              timestamptz default now(),
  document_type           text not null default 'unknown',
  status                  text not null default 'needs_review',
  reference_policy_number text,
  matched_policy_id       uuid references insurance_policies(id) on delete set null,
  insured_name            text,
  license_plate           text,
  coverage_start          date,
  coverage_end            date,
  extracted_data          jsonb not null default '{}'::jsonb,
  pdf_url                 text not null,
  pdf_filename            text,
  pdf_size                integer,
  original_filename       text,
  note                    text
);

create index if not exists document_inbox_status_idx
  on document_inbox(status, created_at desc);
create index if not exists document_inbox_policy_ref_idx
  on document_inbox(reference_policy_number);
