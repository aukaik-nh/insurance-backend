"""Idempotently import the manually verified missing PDFs to live DB + R2."""
import os, sys, uuid
from pathlib import Path
from dotenv import load_dotenv

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); load_dotenv(ROOT/'.env')
load_dotenv(Path(r'C:\Users\Administrator\Downloads\insurance-backend.env'), override=True)
from services.supabase_shim import create_client
import requests

FOLDER=Path(r'C:\Users\Administrator\Downloads\งาน\renamed-pdfs-ocr')
GROUPS=[
  {
    'policy': {'policy_number':'D0-72-69/010160','company_code':'TMSTH','policy_type':'P',
      'insured_name':'นาย พิชิต สืบใหม่','insured_address':'115/9 ม.8 ต.หนองขาว อ.ท่าม่วง จ.กาญจนบุรี 71110',
      'license_plate':'กว 1092 กจ','chassis_no':'MR0HA3FSX00026861','car_make':'TOYOTA','car_model':'FORTUNER'},
    'files': [
      ('นาย พิชิค สืบใหม - 25690821065952678.pdf','other','ใบคืนเบี้ยประกันภัย'),
      ('นาย พิชิต สืบใหม่ - 25690821065616379.pdf','endorsement','สลักหลังยกเลิกกรมธรรม์ 69-003224-001'),
    ]
  },
  {
    'policy': {'policy_number':'D0-70-69/022848','company_code':'TMSTH','policy_type':'M',
      'insured_name':'บริษัท แพลนเน็ต คอมมิวนิเคชั่น เอเชีย จำกัด (มหาชน)',
      'insured_address':'157 ซอยรามอินทรา 34 ถนนรามอินทรา แขวงท่าแร้ง เขตบางเขน กรุงเทพมหานคร 10230',
      'license_plate':'ตน 2313 กท','chassis_no':'JTFHS02P000048533','car_make':'TOYOTA','car_model':'HIACE','car_year':2006,
      'coverage_start':'2026-07-03','coverage_end':'2027-07-03'},
    'files': [('บริษัท แพลนเน็ต คอมมิวนิเคชัน เอเชีย จํากัด (มหาชน - 25690821065605608.pdf','endorsement','สลักหลังการยกเลิกการจดทะเบียน 69-008616-001')]
  }
]

def main():
  sb=create_client()
  api='https://insurance-backend-c2s2.onrender.com/api'
  login=requests.post(api+'/auth/login',json={'username':os.getenv('APP_USERNAME'),'password':os.getenv('APP_PASSWORD'),'remember':False},timeout=90)
  login.raise_for_status(); token=login.json()['token']; headers={'Authorization':f'Bearer {token}'}
  created=uploaded=skipped=0
  for group in GROUPS:
    pol=group['policy']; found=sb.table('insurance_policies').select('id').eq('policy_number',pol['policy_number']).execute().data
    if found: pid=found[0]['id']
    else:
      row={**pol,'manually_edited':True}
      pid=sb.table('insurance_policies').insert(row).execute().data[0]['id']; created+=1
    for filename,doc_type,label in group['files']:
      exists=sb.table('policy_attachments').select('id').eq('policy_id',pid).eq('pdf_filename',filename).execute().data
      if exists: skipped+=1; continue
      path=FOLDER/filename
      with path.open('rb') as fh:
        res=requests.post(api+f'/policies/{pid}/attachments',headers=headers,
          data={'doc_type':doc_type,'label':label,'note':'Codex ตรวจจากเอกสารต้นฉบับ'},
          files={'file':(filename,fh,'application/pdf')},timeout=180)
      res.raise_for_status()
      uploaded+=1
  print({'created_policies':created,'uploaded_attachments':uploaded,'skipped_existing':skipped})

if __name__=='__main__': main()
