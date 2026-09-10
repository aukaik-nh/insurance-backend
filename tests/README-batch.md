# PDF batch import operations

Use the PDF management screen: Import -> Review -> History. Select up to 20 PDFs (12 MB each, 100 MB total). Extraction does not create policy rows. Review the source images and approve the selected records before committing.

## Runtime

- Run one application worker against each staging directory. Multi-worker/distributed leasing is not implemented.
- Set BATCH_STAGING_ROOT to a persistent mounted directory in production. The default .batch-staging directory survives process restarts but not an ephemeral hosting instance replacement.
- Set ENABLE_LOCAL_OCR_FALLBACK=true on a worker with the pinned Tesseract models installed and sufficient RAM. Without it, batch extraction uses the configured Gemini service.
- BATCH_MAX_FILES defaults to 20. BATCH_MAX_TOTAL_BYTES defaults to 100 MB.
- Each completed file is checkpointed. Startup resumes manifests without final results, skipping completed reads. Failed extraction remains available for manual review; it is not silently saved.

## Data guarantees and limits

Matching rejects conflicting VINs or coverage years. Matching with unknown years, OCR review flags, conflicting plates, or ambiguous candidates requires review. A test covers 20 synthetic records / 10 expected pairs; this is not an OCR accuracy benchmark.

Each policy and its PRB attachment is inserted within one PostgreSQL transaction. A transaction-scoped advisory lock plus an existing-policy check prevents duplicate policy/company insertion through this batch endpoint. It does not impose a database-wide unique constraint on other writers. The batch is committed pair by pair; successful pairs remain if another pair fails.

PDF uploads precede the database transaction. R2 and PostgreSQL do not share a transaction; a failed database insertion can leave an unreferenced uploaded object. No production records were created by the automated tests.

Current extraction uses native text / local OCR plus optional Gemini review. Thai names and addresses can still require correction. No 100% accuracy claim is made. Evaluate a pinned cloud OCR provider on representative, manually labelled PDFs before replacing the existing reader.
