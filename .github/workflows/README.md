# GitHub Actions — Backups

## weekly-backup.yml

Dump `insurance_policies` + `policy_attachments` from Neon → upload to R2 `insurance-backups` bucket.

Runs every Sunday 03:00 (Asia/Bangkok). Manual trigger from Actions tab.

### Required repository secrets

Settings → Secrets and variables → Actions → New repository secret:

| Secret | Value |
|---|---|
| `NEON_URL` | `postgresql://neondb_owner:...@ep-...-pooler.ap-southeast-1.aws.neon.tech/neondb?sslmode=require` |
| `R2_ENDPOINT` | `https://<account>.r2.cloudflarestorage.com` |
| `R2_BACKUP_BUCKET` | `insurance-backups` |
| `R2_BACKUP_ACCESS_KEY` | from Cloudflare R2 API token (scoped to `insurance-backups` bucket — Object Read & Write) |
| `R2_BACKUP_SECRET_KEY` | (paired with above) |

> Create a **separate** R2 API token for backups — don't reuse `insurance-backend` token. If backup credentials leak, you only lose write access to backups, not your live PDFs.

### Test manually

GitHub repo → Actions tab → "Weekly DB Backup" → **Run workflow** → main

### Restore from a backup

```bash
# download from R2 (using rclone or aws cli)
aws s3 cp s3://insurance-backups/insurance_backup_20260601_200000.sql.gz . \
  --endpoint-url https://<account>.r2.cloudflarestorage.com

# restore into Neon (or any Postgres)
gunzip -c insurance_backup_20260601_200000.sql.gz | psql "$NEON_URL"
```

### Retention

Backups older than **90 days** are auto-deleted in the same workflow run. Adjust the `CUTOFF` line in the "Prune" step if you want longer retention.
