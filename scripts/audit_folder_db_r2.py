"""Compare local PDFs with policy and attachment filenames in the live DB (read-only)."""
import argparse, json, sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")
from services.supabase_shim import create_client

def all_rows(sb, table, cols):
    out=[]
    for start in range(0, 100000, 1000):
        rows=sb.table(table).select(cols).range(start,start+999).execute().data or []
        out.extend(rows)
        if len(rows)<1000: break
    return out

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--folder',required=True); ap.add_argument('--output',default=str(ROOT/'tmp'/'folder_db_audit.json')); a=ap.parse_args()
    sb=create_client()
    policies=all_rows(sb,'insurance_policies','id,policy_number,insured_name,pdf_filename,pdf_url')
    atts=all_rows(sb,'policy_attachments','id,policy_id,doc_type,pdf_filename,pdf_url')
    by_name={}
    for kind,rows in [('policy',policies),('attachment',atts)]:
        for r in rows:
            if r.get('pdf_filename'): by_name.setdefault(r['pdf_filename'],[]).append({'kind':kind,**r})
    result=[]
    for p in sorted(Path(a.folder).glob('*.pdf')):
        hits=by_name.get(p.name,[])
        result.append({'filename':p.name,'size':p.stat().st_size,'exists':bool(hits),'hits':hits})
    Path(a.output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'total':len(result),'existing':sum(x['exists'] for x in result),'missing':sum(not x['exists'] for x in result),'missing_files':[x['filename'] for x in result if not x['exists']]},ensure_ascii=False,indent=2))

if __name__=='__main__': main()

