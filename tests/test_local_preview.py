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
from services.local_pdf_parser import parse_ocr_text, parse_pdf_image_locally


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
        self.assertTrue(any("--psm 11" in call.kwargs["config"] for call in ocr_mock.call_args_list))

    def test_known_layout_supplements_empty_fields_and_prefers_clear_filename_name(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        layout = {
            "layout": "tmsth_electric_motor_schedule_v1",
            "field_evidence": {
                "license_plate": {
                    "status": "review", "value": None, "manual_value": "8กก6384", "text": "8กก6384"
                }
            },
            "raw_text": "ทะเบียนรถ [รอตรวจ]\n8กก6384",
        }
        ocr = (
            "0-70-68/08 1970\n30/11/2026 - 30/11/2027\nRenewal Period Insured\n"
            "HONDA CRV\nMRHRM4830EP 100434\n2014\nYear"
        )
        with patch("services.segmented_ocr.read_document", return_value=layout), \
             patch("pytesseract.image_to_string", return_value=ocr):
            result = parse_pdf_image_locally(blob, "นางสาว สมใจ แซ่อึ้ง.pdf")
        self.assertEqual(result["policy_number"], "D0-70-68/081970")
        self.assertEqual(result["insured_name"], "นางสาว สมใจ แซ่อึ้ง")
        self.assertEqual(result["license_plate"], "8กก6384")
        self.assertEqual(result["chassis_no"], "MRHRM4830EP100434")
        self.assertIn("policy_number", result["review_fields"])
        self.assertEqual(result["field_evidence"]["insured_name"]["source"], "filename")

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

    def test_verified_preview_fills_form_and_keeps_local_evidence(self):
        local = {
            "doc_type": "motor_main", "company_code": "TMSTH", "license_plate": None,
            "insured_name": None, "parse_engine": "python_tesseract_segmented",
            "requires_review": True, "parse_warnings": [], "raw_text": "local text",
            "field_evidence": {
                "license_plate": {"text": "7ฒน100", "manual_value": "7ฒน100", "status": "review"},
            },
            "preview": {"image_data_url": "data:image/png;base64,test"},
        }
        ai = {
            "doc_type": "motor_main", "insured_name": "ดร. สิบสกุล พิพมงคล",
            "license_plate": "วข 2066 กท", "coverage_start": "2026-08-16",
            "coverage_end": "2027-08-16", "chassis_no": "W0L0TGF75H030979",
        }
        with patch.object(upload, "parse_pdf_image_locally", return_value=local), \
             patch.object(upload, "gemini_available", return_value=True), \
             patch.object(upload, "parse_with_gemini", return_value=ai):
            response = asyncio.run(upload.preview_pdf_verified(
                UploadFile(filename="policy.pdf", file=io.BytesIO(b"pdf"))))
        self.assertTrue(response["used_ai"])
        self.assertEqual(response["parsed"]["insured_name"], "ดร. สิบสกุล พิพมงคล")
        self.assertEqual(response["parsed"]["license_plate"], "วข 2066 กท")
        self.assertEqual(response["parsed"]["policy_type"], "M")
        self.assertEqual(response["preview"]["image_data_url"], "data:image/png;base64,test")
        self.assertIn("license_plate", response["parsed"]["review_fields"])

    def test_verified_preview_falls_back_when_ai_is_unavailable(self):
        local = {"policy_number": "D0-70-69/023500", "parse_engine": "native_text",
                 "requires_review": True, "preview": {}}
        with patch.object(upload, "parse_pdf_image_locally", return_value=local), \
             patch.object(upload, "gemini_available", return_value=False), \
             patch.object(upload, "parse_with_gemini", side_effect=AssertionError("AI called")):
            response = asyncio.run(upload.preview_pdf_verified(
                UploadFile(filename="policy.pdf", file=io.BytesIO(b"pdf"))))
        self.assertFalse(response["used_ai"])
        self.assertEqual(response["parsed"]["policy_number"], "D0-70-69/023500")

    def test_policy_number_does_not_accept_expiry_heading(self):
        parsed = parse_ocr_text("Policy No. EXPIRY DATE\n30/11/2569\nPeriod of Insurance")
        self.assertIsNone(parsed["policy_number"])

    def test_policy_number_normalises_ocr_letter_o(self):
        parsed = parse_ocr_text("Policy No. DO-70-68/031970")
        self.assertEqual(parsed["policy_number"], "D0-70-68/031970")

    def test_noisy_renewal_notice_extracts_core_vehicle_fields(self):
        parsed = parse_ocr_text(
            "0-70-68/08 1970\n30/11/2026 - 30/11/2027\nRenewal Period Insured\n"
            "HONDA CR-V\nMRHRM4830EP 100434\nChassis No.\n2014\nYear"
        )
        self.assertEqual(parsed["policy_number"], "D0-70-68/081970")
        self.assertEqual(parsed["coverage_start"], "2026-11-30")
        self.assertEqual(parsed["coverage_end"], "2027-11-30")
        self.assertEqual(parsed["chassis_no"], "MRHRM4830EP100434")
        self.assertEqual(parsed["car_make"], "HONDA")
        self.assertEqual(parsed["car_model"], "CR-V")
        self.assertEqual(parsed["car_year"], "2014")


if __name__ == "__main__":
    unittest.main()
