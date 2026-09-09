import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from services.segmented_ocr import decide, validate_groups, read_document, MONEY_FIELDS, _normalize, _native_text_and_confidence, _read_prb, _read_cancellation, _read_fire, _fire_money_value, _clean_fire_address, _vin_check_digit_valid, _clean_review_prose
from services.local_pdf_parser import parse_pdf_image_locally, is_confident
import pymupdf


def readings(a, b=None, confidence=98):
    return [{"text": t, "min_confidence": confidence} for t in (a, a if b is None else b)]


def evidence():
    result = {field: decide(field, readings(text)) for field, text in zip(MONEY_FIELDS, ("1,000.00", "4.00", "70.28", "1,074.28"))}
    result.update({"coverage_start": decide("coverage_start", readings("3 กรกฎาคม 2569")),
                   "coverage_end": decide("coverage_end", readings("3 กรกฎาคม 2570"))})
    return result


class SegmentedTests(unittest.TestCase):
    def test_native_thai_text_not_rebuilt_from_tsv_glyphs(self):
        native = "บริษัท ทดสอบ จำกัด\nที่อยู่ แขวงท่าแร้ง\n"
        output_bases = []
        def fake_tesseract(**kwargs):
            base = kwargs['output_filename_base']
            output_bases.append(base)
            Path(base+'.txt').write_text(native, encoding='utf-8')
            Path(base+'.tsv').write_text('level\tconf\ttext\n1\t-1\t\n5\t98.4\tบริ\n5\t87.2\tษัท\n', encoding='utf-8')
            self.assertIn('--psm 7', kwargs['config'])
            self.assertIn('tessedit_create_tsv=1', kwargs['config'])
            self.assertEqual(kwargs['lang'], 'tha+eng')
            self.assertEqual(kwargs['timeout'], 4)
        with patch('services.segmented_ocr.pytesseract.pytesseract.run_tesseract', side_effect=fake_tesseract) as run:
            result = _native_text_and_confidence(Image.new('L', (100, 40), 255), 'tha+eng', '--psm 7', 4)
        self.assertEqual(result, {'text': native.strip(), 'min_confidence': 87.2})
        self.assertEqual(run.call_count, 1)
        self.assertFalse(Path(output_bases[0]+'.txt').exists())
        self.assertFalse(Path(output_bases[0]+'.tsv').exists())

    def test_prb_profile_requires_anchors_not_just_table(self):
        rows = list(range(0, 1400, 100))
        columns = [[10, 590] for _ in range(13)]
        for i in (4, 5, 9, 10):
            columns[i] = [10, 100, 200, 300, 400, 500, 590]
        with patch('services.segmented_ocr._read', return_value={'text': 'different policy'}):
            result = _read_prb(Image.new('L', (600, 1400), 255), rows, columns, '', None)
        self.assertIsNone(result)

    def test_prb_profile_sets_correct_document_type(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        with patch('services.segmented_ocr.read_document', return_value={
                'layout': 'tmsth_compulsory_motor_v1', 'field_evidence': {}, 'raw_text': 'review'}):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result['doc_type'], 'motor_prb')

    def test_cancellation_profile_sets_endorsement_type(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        with patch('services.segmented_ocr.read_document', return_value={
                'layout': 'tmsth_cancellation_endorsement_v1', 'field_evidence': {}, 'raw_text': 'review'}):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result['doc_type'], 'endorsement')

    def test_fire_profile_sets_document_and_policy_type(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        with patch('services.segmented_ocr.read_document', return_value={
                'layout': 'tmsth_fire_schedule_v1', 'field_evidence': {}, 'raw_text': 'review'}):
            result = parse_pdf_image_locally(blob)
        self.assertEqual(result['doc_type'], 'fire')
        self.assertEqual(result['policy_type'], 'FIRE')

    def test_fire_profile_requires_anchors_not_just_table(self):
        rows = list(range(200, 2200, 100))
        columns = [[10, 590] for _ in range(19)]
        columns[0] = [10, 300, 590]
        columns[7] = [10, 150, 300, 450, 590]
        columns[18] = list(range(10, 650, 80))
        with patch('services.segmented_ocr._read', return_value={'text': 'different policy'}):
            result = _read_fire(Image.new('L', (600, 2300), 255), rows, columns, '', None)
        self.assertIsNone(result)

    def test_fire_money_accepts_ocr_separator_swap_only_with_two_decimals(self):
        self.assertEqual(_fire_money_value('Net Premium 17.600.00 Baht'), 17600)
        self.assertEqual(_fire_money_value('VAT 1,236.97 Baht'), 1236.97)
        self.assertIsNone(_fire_money_value('VAT 7%'))
        self.assertIsNone(_fire_money_value('1,000.00 or 2,000.00'))

    def test_fire_address_normalises_thai_label_glyphs_without_changing_numbers(self):
        self.assertEqual(
            _clean_fire_address([
                '| 149/224 หมู่ 13 ถนนเพชรเกษม 95',
                'ess ตําบลอ้อมน้อย อําเภอกระทุ่มแบน',
                'จังหวัดสมทรสาคร 74310',
            ]),
            '149/224 หมู่ 13 ถนนเพชรเกษม 95\nตำบลอ้อมน้อย อำเภอกระทุ่มแบน\nจังหวัดสมุทรสาคร 74310',
        )

    def test_two_matching_money_reads(self):
        self.assertEqual(decide("net_premium", readings("1,000.00"))["value"], 1000)

    def test_conflicting_money_not_filled(self):
        self.assertIsNone(decide("net_premium", readings("1,000.00", "7,000.00"))["value"])

    def test_malformed_money_is_not_repaired(self):
        for text in ("1.000.00", "66,00", "1,000.00 or", "1000", "VAT 7%", "1,000.00 4.00"):
            self.assertIsNone(decide("net_premium", readings(text))["value"])

    def test_low_confidence_identifiers_not_filled(self):
        for field, text in (("policy_number", "D0-70-69/123456"), ("license_plate", "คน 1234 กท"), ("chassis_no", "JTFHS02P000012345")):
            self.assertIsNone(decide(field, readings(text, confidence=90))["value"])

    def test_conflicting_identifiers_not_filled(self):
        self.assertIsNone(decide("license_plate", readings("คน 1234 กท", "ตน 1234 กท"))["value"])

    def test_thai_name_always_needs_review(self):
        self.assertIsNone(decide("insured_name", readings("บริษัท ตัวอย่าง จำกัด", confidence=100))["value"])

    def test_whitespace_in_thai_date(self):
        self.assertEqual(_normalize("coverage_start", "3 ก ร ก ฎา ค ม 2569"), "2026-07-03")

    def test_common_thai_month_glyph_error_is_recovered(self):
        self.assertEqual(_normalize("coverage_start", "24 สึงหาคม 2569"), "2026-08-24")

    def test_policy_number_ignores_barcode_edge_artifact(self):
        self.assertEqual(_normalize("policy_number", "-D0-70-69/025154"), "D0-70-69/025154")

    def test_vin_check_digit_selects_the_structurally_valid_read(self):
        self.assertTrue(_vin_check_digit_valid("MR053BK4007001666"))
        self.assertFalse(_vin_check_digit_valid("MR0S3BK4007001666"))

    def test_review_prose_removes_table_labels_and_short_english_noise(self):
        self.assertEqual(
            _clean_review_prose("insured_name", "ชื่อ\nนาง วิไลรัตน์ แซ่เจียม\nne"),
            "นาง วิไลรัตน์ แซ่เจียม",
        )

    def test_valid_premium_group(self):
        data = evidence()
        validate_groups(data)
        self.assertEqual(data["total_premium"]["value"], 1074.28)
        self.assertEqual(data["coverage_start"]["value"], "2026-07-03")

    def test_missing_money_clears_entire_group(self):
        data = evidence()
        data["stamp_duty"]["value"] = None
        validate_groups(data)
        self.assertTrue(all(data[k]["value"] is None for k in MONEY_FIELDS))

    def test_cent_mismatch_clears_entire_group(self):
        data = evidence()
        data["total_premium"]["value"] += .02
        validate_groups(data)
        self.assertTrue(all(data[k]["value"] is None for k in MONEY_FIELDS))

    def test_wrong_date_order_clears_pair(self):
        data = evidence()
        data["coverage_start"]["value"] = "2028-01-01"
        validate_groups(data)
        self.assertIsNone(data["coverage_start"]["value"])
        self.assertIsNone(data["coverage_end"]["value"])

    def test_unsupported_layout_only_returns_text(self):
        with patch("services.segmented_ocr.model_config", return_value=""), \
             patch("services.segmented_ocr._read") as read:
            with Image.new("L", (600, 800), 255) as image:
                result = read_document(image)
        self.assertIsNone(result["layout"])
        self.assertEqual(result["field_evidence"], {})
        self.assertIn("ยังไม่รองรับ", result["raw_text"])
        read.assert_not_called()

    def test_layout_is_not_accepted_without_all_anchors(self):
        rows = list(range(0, 2100, 100))
        columns = [[10, 590] for _ in range(20)]
        columns[7] = list(range(10, 510, 50))
        columns[16] = [10, 150, 300, 450, 590]
        with Image.new("L", (600, 2200), 255) as image, \
             patch("services.segmented_ocr.model_config", return_value=""), \
             patch("services.segmented_ocr.table_geometry", return_value=(image, rows, columns, 0)), \
             patch("services.segmented_ocr._read", return_value={"text": "TMSTH MOTOR INSURANCE"}):
            result = read_document(image)
        self.assertIsNone(result["layout"])
        self.assertFalse(result["field_evidence"])

    def test_preview_does_not_leak_unreviewed_values(self):
        with pymupdf.open() as doc:
            doc.new_page()
            blob = doc.tobytes()
        extracted = {"raw_text": "test", "layout": "tmsth_motor_schedule_v1", "field_evidence": {
            "license_plate": {"status": "review", "value": "คน1234"},
            "net_premium": {"status": "candidate", "value": 1000},
        }}
        with patch("services.segmented_ocr.read_document", return_value=extracted):
            result = parse_pdf_image_locally(blob)
        self.assertIsNone(result["license_plate"])
        self.assertEqual(result["net_premium"], 1000)
        self.assertTrue(result["requires_review"])
        self.assertFalse(is_confident(result))
        self.assertIsNone(result["sum_insured"])


if __name__ == "__main__":
    unittest.main()
