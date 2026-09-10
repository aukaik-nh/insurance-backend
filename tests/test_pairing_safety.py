import unittest
from services.doc_pairing import classify, pair_documents, score_pair

class PairingSafetyTests(unittest.TestCase):
    def docs(self):
        base = dict(chassis_no='MRHGM2620CP408631', license_plate='1กก8803', coverage_start='2026-04-09', insured_name='นาย ทดสอบ')
        return dict(base, doc_type='motor_main', file_id='0000'), dict(base, doc_type='motor_prb', file_id='0001')

    def test_same_car_other_year_never_pairs(self):
        main, prb = self.docs()
        prb['coverage_start'] = '2025-04-09'
        self.assertLess(score_pair(main, prb)[0], 0)
        self.assertEqual(pair_documents([main, prb])['pairs'], [])

    def test_ocr_review_never_becomes_auto_pair(self):
        main, prb = self.docs()
        main['requires_review'] = True
        self.assertEqual(pair_documents([main, prb])['pairs'][0]['status'], 'review')

    def test_plate_conflict_requires_review(self):
        main, prb = self.docs()
        prb['license_plate'] = '2กก9999'
        self.assertEqual(pair_documents([main, prb])['pairs'][0]['status'], 'review')

    def test_twenty_documents_pair_by_vehicle_and_year(self):
        records = []
        for i in range(10):
            main, prb = self.docs()
            for record in (main, prb):
                record['chassis_no'] = f'MRHGM2620CP40{i:04d}'
                record['license_plate'] = f'1กก{8800+i}'
                record['file_id'] += str(i)
            records.extend((main, prb))
        result = pair_documents(list(reversed(records)))
        self.assertEqual(len(result['pairs']), 10)
        self.assertTrue(all(pair['main']['chassis_no'] == pair['prb']['chassis_no'] for pair in result['pairs']))

    def test_policy_number_corrects_noisy_prb_classification(self):
        record = {'doc_type': 'motor_main', 'policy_number': 'D0-72-69/004889'}
        self.assertEqual(classify(record), 'motor_prb')

    def test_filename_plate_and_year_proposes_review_pair(self):
        main = {'policy_number': 'D0-70-69/1', 'orig_filename': '1กก8803 กธ.69.pdf'}
        prb = {'policy_number': 'D0-72-69/2', 'orig_filename': '1กก8803 พรบ.69.pdf'}
        result = pair_documents([main, prb])
        self.assertEqual(len(result['pairs']), 1)
        self.assertEqual(result['pairs'][0]['status'], 'review')
