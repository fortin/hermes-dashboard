import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from app.services import fantastical

TZ = ZoneInfo("Asia/Bangkok")


class ParseWhenTests(unittest.TestCase):
    def test_single_local_day_is_month_day_year(self):
        start = datetime(2026, 9, 18, tzinfo=TZ)
        end = datetime(2026, 9, 19, tzinfo=TZ)
        self.assertEqual(fantastical._parse_when(start, end), "September 18 2026")

    def test_multi_day_range_matches_fantastical_phrase(self):
        start = datetime(2026, 7, 7, tzinfo=TZ)
        end = datetime(2026, 7, 11, tzinfo=TZ)
        self.assertEqual(
            fantastical._parse_when(start, end),
            "July 7 to July 10 2026",
        )
