import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from fastapi import UploadFile, HTTPException
from routes import batch

class BatchSafetyTests(unittest.TestCase):
    def test_local_reader_used_without_cloud(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, '0000.pdf').write_bytes(b'pdf')
            record = dict(file_id='0000', orig_filename='example.pdf', same_file_as=None)
            with patch.dict('os.environ', {'ENABLE_LOCAL_OCR_FALLBACK':'true'}), patch.object(batch, 'gemini_available', return_value=False), patch('services.local_pdf_parser.parse_pdf_image_locally', return_value={'policy_number':'P12345', 'preview':{'image_data_url':'large'}}):
                result = batch._read_one_file(folder, record)
            self.assertEqual(result['parsed']['policy_number'], 'P12345')
            self.assertNotIn('preview', result['parsed'])
            self.assertTrue(result['parsed']['requires_review'])
            self.assertNotIn('parse_error', result)

    def test_invalid_commit_does_not_touch_database(self):
        with patch.object(batch, '_load_manifest', return_value={'files':[{'file_id':'0000'}]}), patch.object(batch, 'get_supabase', side_effect=AssertionError('DB touched')):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(batch.batch_commit('test', {'items':[{'main_file_id':'0000','main':{}}]}))
            self.assertEqual(error.exception.status_code, 422)

    def test_atomic_manifest_is_readable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder, 'manifest.json'))
            batch._atomic_json(path, {'status':'reading'})
            batch._atomic_json(path, {'status':'done'})
            import json
            self.assertEqual(json.loads(Path(path).read_text())['status'], 'done')
            self.assertEqual(len(list(Path(folder).iterdir())),1)

    def test_pair_transaction_rolls_back_on_attachment_failure(self):
        from unittest.mock import MagicMock
        from services import batch_persistence
        conn = MagicMock()
        cur = conn.cursor.return_value.__enter__.return_value
        cur.fetchone.side_effect = [None, {'id':123}]
        def execute(sql, params):
            if 'INSERT INTO "policy_attachments"' in sql:
                raise RuntimeError('attachment insert failed')
        cur.execute.side_effect = execute
        with patch.object(batch_persistence, '_get_conn', return_value=conn), patch.object(batch_persistence, '_put_conn'):
            with self.assertRaises(RuntimeError):
                batch_persistence.insert_policy_pair({'policy_number':'TEST'}, {'doc_type':'prb'})
        conn.rollback.assert_called_once()
        conn.commit.assert_not_called()

    def test_completed_file_is_not_ocr_processed_again(self):
        record = {'read_complete':True, 'parsed':{'policy_number':'TEST'}}
        with patch.object(batch, 'parse_with_gemini', side_effect=AssertionError('reprocessed')):
            self.assertIs(batch._read_one_file('unused', record), record)
