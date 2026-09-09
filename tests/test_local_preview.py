"""Local preview contract tests: no database, storage or paid API calls."""
import asyncio
import base64
import io
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException, UploadFile
from PIL import Image
import pymupdf

from routes import upload
from services.local_pdf_parser import parse_pdf_image_locally


class PreviewTests(unittest.TestCase):
    def test_preview_contract(self):
        result = {"raw_text": "example", "parse_engine": "python_tesseract_image",
                  "requires_review": True, "preview": {"image_data_url": "data:image/png;base64,test"}}
        with patch.object(upload, "parse_pdf_image_locally", return_value=result), \
             patch.object(upload, "parse_with_gemini", side_effect=AssertionError("Paid parser called")), \
             patch.object(upload, "get_supabase", side_effect=AssertionError("Database called")):
            response = asyncio.run(upload.preview_pdf_local(UploadFile(filename="test.pdf", file=io.BytesIO(b"pdf"))))
        self.assertFalse(response["used_ai"])
        self.assertTrue(response["requires_review"])
        self.assertIn("image_data_url", response["preview"])
        self.assertNotIn("preview", response["parsed"])

    def test_size_limit_and_file_type(self):
        with patch.object(upload, "MAX_PDF_BYTES", 4):
            for filename, data, status in [("bad.txt", b"x", 400), ("large.pdf", b"12345", 413)]:
                with self.assertRaises(HTTPException) as error:
                    asyncio.run(upload.preview_pdf_local(UploadFile(filename=filename, file=io.BytesIO(data))))
                self.assertEqual(status, error.exception.status_code)

    def test_image_is_returned_and_raster_is_bounded(self):
        with pymupdf.open() as doc:
            doc.new_page(width=2400, height=3200)
            blob = doc.tobytes()
        with patch("services.segmented_ocr.read_document", return_value={"raw_text": "Example OCR text", "layout": None}):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result["raw_text"], "Example OCR text")
        self.assertTrue(result["requires_review"])
        encoded = result["preview"]["image_data_url"].split(",", 1)[1]
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertLessEqual(max(image.size), 3600)
        self.assertIsNone(result["policy_number"])
        self.assertIsNone(result["total_premium"])

    def test_unknown_layout_uses_review_only_full_page_fallback(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        ocr = "Policy No D0-70-69/022848\nPeriod of Insurance 1 กรกฎาคม 2569 to 1 กรกฎาคม 2570"
        with patch("services.segmented_ocr.read_document", return_value={
                "layout": None, "field_evidence": {}, "raw_text": "ยังไม่รองรับรูปแบบเอกสารนี้"}), \
             patch("pytesseract.image_to_string", return_value=ocr) as ocr_mock:
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result["parse_engine"], "python_tesseract_full_page_fallback")
        self.assertEqual(result["policy_number"], "D0-70-69/022848")
        self.assertEqual(result["coverage_start"], "2026-07-01")
        self.assertEqual(result["coverage_end"], "2027-07-01")
        self.assertTrue(result["requires_review"])
        self.assertIn("policy_number", result["field_evidence"])
        self.assertIn("--psm 11", ocr_mock.call_args.kwargs["config"])

    def test_corrupt_document_is_reported(self):
        result = parse_pdf_image_locally(b"not a pdf")
        self.assertEqual(result["parse_engine"], "local_ocr_failed")
        self.assertTrue(result["requires_review"])
        self.assertEqual(result["parse_error_code"], "render_failed")

    def test_missing_ocr_dependency_keeps_preview(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        with patch.dict(sys.modules, {"services.segmented_ocr": None}):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result["parse_error_code"], "ocr_dependency_missing")
        self.assertIn("image_data_url", result["preview"])
        self.assertEqual(result["preview"]["page_count"], 1)
        self.assertIsNone(result["policy_number"])

    def test_ocr_runtime_failure_keeps_preview(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        with patch("services.segmented_ocr.read_document", side_effect=RuntimeError("test failure")):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result["parse_error_code"], "ocr_failed")
        self.assertIn("image_data_url", result["preview"])

    def test_dependency_error_response_is_safe_and_actionable(self):
        result = {"parse_engine": "local_ocr_failed", "parse_error": "private path should not leak",
                  "parse_error_code": "ocr_dependency_missing", "preview": {"image_data_url": "data:image/png;base64,test"}}
        with patch.object(upload, "parse_pdf_image_locally", return_value=result):
            response = asyncio.run(upload.preview_pdf_local(UploadFile(filename="test.pdf", file=io.BytesIO(b"pdf"))))
        self.assertFalse(response["success"])
        self.assertNotIn("private path", response["parsed"]["parse_error"])
        self.assertIn("ติดตั้ง", response["parsed"]["parse_error"])
        self.assertIn("image_data_url", response["preview"])


if __name__ == "__main__":
    unittest.main()
