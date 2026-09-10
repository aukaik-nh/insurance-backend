-- Apply before deploying document naming changes. Existing rows are unchanged.
ALTER TABLE insurance_policies ADD COLUMN IF NOT EXISTS risk_address text;
ALTER TABLE insurance_policies ADD COLUMN IF NOT EXISTS original_filename text;
