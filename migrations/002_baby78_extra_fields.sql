-- Add Baby78 extra fields so detail page can show full data
-- All nullable — backward compatible

ALTER TABLE insurance_policies
  ADD COLUMN IF NOT EXISTS email           text,
  ADD COLUMN IF NOT EXISTS prefix          text,
  ADD COLUMN IF NOT EXISTS keyby           text,
  ADD COLUMN IF NOT EXISTS seat            integer,
  ADD COLUMN IF NOT EXISTS cc              integer,
  ADD COLUMN IF NOT EXISTS weight          integer,
  ADD COLUMN IF NOT EXISTS equipment       text,
  ADD COLUMN IF NOT EXISTS car_code        text,
  ADD COLUMN IF NOT EXISTS app_prb         text,
  ADD COLUMN IF NOT EXISTS app_next        text,
  ADD COLUMN IF NOT EXISTS date_insurance  date,
  ADD COLUMN IF NOT EXISTS date_car_tax    date,
  ADD COLUMN IF NOT EXISTS date_sent       date,
  ADD COLUMN IF NOT EXISTS driver1_name    text,
  ADD COLUMN IF NOT EXISTS driver1_birth   date,
  ADD COLUMN IF NOT EXISTS driver2_name    text,
  ADD COLUMN IF NOT EXISTS driver2_birth   date,
  ADD COLUMN IF NOT EXISTS theft           numeric,
  ADD COLUMN IF NOT EXISTS deductible      numeric;
