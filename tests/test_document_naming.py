import unittest
from services.document_naming import make_display_filename as name


class NamingTests(unittest.TestCase):
    def test_motor_start_year_not_expiry(self):
        self.assertEqual(name('1กก 1234', 'main', '2026-09-10', '2027-09-10'), '1กก1234-กธ-2569.pdf')

    def test_prb_standalone_and_attachment(self):
        for kind, policy in [('main', 'P'), ('prb', 'M')]:
            self.assertEqual(name('1กก1234', kind, '10/09/2569', policy_type=policy), '1กก1234-พรบ.-2569.pdf')

    def test_missing_or_invalid_start_cannot_use_expiry(self):
        for start in [None, '2026-02-30', '69']:
            self.assertEqual(name('1กก1234', 'main', start, '2027-09-10'), 'รอตรวจข้อมูล.pdf')

    def test_fire_never_uses_contact_address(self):
        self.assertEqual(name(None, 'main', policy_type='FIRE', address='ที่อยู่ติดต่อ'), 'รอตรวจข้อมูล.pdf')
        self.assertEqual(name(None, 'main', policy_type='FIRE', risk_address='123/45 บางแก้ว'), '123-45 บางแก้ว.pdf')

    def test_personal_insurance(self):
        for policy in ['PA', 'TA']:
            self.assertEqual(name(None, 'main', policy_type=policy, name='นาย สมชาย ใจดี'), 'นาย สมชาย ใจดี.pdf')


if __name__ == '__main__':
    unittest.main()
