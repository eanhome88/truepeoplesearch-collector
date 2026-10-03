"""号码发生器只产出可分配的美国号码，并跳过已入库的关联号码。"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import phone_plan as plan


class PhonePlanTest(unittest.TestCase):
    def test_skips_service_and_non_us_codes(self):
        self.assertFalse(plan.valid_npa(211))
        self.assertFalse(plan.valid_npa(800))
        self.assertFalse(plan.valid_npa(900))
        self.assertFalse(plan.valid_npa(416))
        self.assertTrue(plan.valid_npa(201))
        self.assertTrue(plan.valid_npa(212))
        self.assertFalse(plan.valid_nxx(211))
        self.assertFalse(plan.valid_nxx(555))
        self.assertTrue(plan.valid_nxx(200))

    def test_first_number_skips_0000(self):
        numbers, cursor = plan.select_batch(
            201, 200, 0, 2,
            blocked=lambda batch: [False] * len(batch),
            cold=lambda _npa, _nxx: False,
        )
        self.assertEqual(numbers, ["2012000001", "2012000002"])
        self.assertEqual(cursor, (201, 200, 3))

    def test_skips_known_numbers_and_dead_prefix(self):
        blocked = {"2012000001", "2012000002"}

        def block(batch):
            return [number in blocked for number in batch]

        def cold(npa, nxx):
            return (npa, nxx) == (201, 211)

        numbers, cursor = plan.select_batch(
            201, 200, 1, 2,
            blocked=block,
            cold=cold,
            max_scan=20,
        )
        self.assertEqual(numbers[0], "2012000003")
        self.assertNotIn("2012110001", numbers)
        self.assertIsNotNone(cursor)

    def test_associated_phones_become_digits(self):
        phones = [
            {"phone_number": "(303) 210-9670"},
            {"phone_number": "(303) 822-8055"},
            {"phone_number": "(303) 210-9670"},
            {"phone_number": "1-720-951-2581"},
        ]
        self.assertEqual(
            plan.associated_digits(phones),
            ["3032109670", "3038228055", "7209512581"],
        )

    def test_phone_lookup_url_round_trip(self):
        url = plan.phone_url("2012000001")
        self.assertEqual(url, "https://www.truepeoplesearch.com/find/phone/2012000001")
        self.assertEqual(plan.phone_digits_from_url(url), "2012000001")
        self.assertEqual(
            plan.phone_digits_from_url("https://www.truepeoplesearch.com/resultphone?phoneno=2012000001"),
            "2012000001",
        )


if __name__ == "__main__":
    unittest.main()
