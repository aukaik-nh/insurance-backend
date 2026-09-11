import unittest

from fastapi import HTTPException

from routes.upload import _apply_financial_totals


class FinancialTotalsTests(unittest.TestCase):
    def test_calculates_commission_tax_and_collected_for_policy_pair(self):
        data = {
            "net_premium": 11600,
            "stamp_duty": 47,
            "vat": 815.29,
            "total_premium": 12462.29,
            "prepaid_tax_1pct": 116,
            "commission_pct": 15,
            "commission_baht": None,
            "wht_10pct": None,
            "rounding": 0,
        }
        result = _apply_financial_totals(data, 645.21)
        self.assertEqual(result["commission_baht"], 1740)
        self.assertEqual(result["wht_10pct"], 174)
        self.assertEqual(result["collected_amount"], 11425.50)

    def test_rejects_inconsistent_premium_total(self):
        data = {"net_premium": 100, "stamp_duty": 1, "vat": 7.07, "total_premium": 200}
        with self.assertRaises(HTTPException) as error:
            _apply_financial_totals(data)
        self.assertEqual(error.exception.status_code, 422)


if __name__ == "__main__":
    unittest.main()
