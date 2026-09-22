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


JEWISH_ID = "c0baabd4fa006a6ab7bbedb269624e44801bf7d5"
CALENDARS = {JEWISH_ID: "Jewish Holidays"}


class NormalizeEventTests(unittest.TestCase):
    def test_resolves_calendar_title_from_id(self):
        event = fantastical._normalize_event(
            {
                "id": f"{JEWISH_ID};hebcal-20260920-821e8c6f",
                "title": "Erev Yom Kippur",
                "startDate": "2026-09-20T00:00:00+07:00",
                "endDate": "2026-09-21T00:00:00+07:00",
                "calendarId": JEWISH_ID,
            },
            CALENDARS,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.calendar, "Jewish Holidays")
        self.assertTrue(event.all_day)

    def test_resolves_calendar_from_event_id_prefix(self):
        event = fantastical._normalize_event(
            {
                "id": f"{JEWISH_ID};hebcal-20260920-821e8c6f",
                "title": "Erev Yom Kippur",
                "startDate": "2026-09-20T00:00:00+07:00",
                "endDate": "2026-09-21T00:00:00+07:00",
            },
            CALENDARS,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.calendar, "Jewish Holidays")

    def test_titles_from_calendars_maps_id_to_title(self):
        titles = fantastical._titles_from_calendars(
            [
                {"id": JEWISH_ID, "title": "Jewish Holidays"},
                {"id": "x", "title": "Family"},
            ]
        )
        self.assertEqual(titles[JEWISH_ID], "Jewish Holidays")
        events = fantastical._events_from_raw(
            {
                "timezone": "Asia/Bangkok",
                "items": [
                    {
                        "id": f"{JEWISH_ID};hebcal-1",
                        "title": "Erev Yom Kippur",
                        "startDate": "2026-09-20T00:00:00+07:00",
                        "endDate": "2026-09-21T00:00:00+07:00",
                        "calendarId": JEWISH_ID,
                    }
                ],
            },
            titles,
        )
        self.assertEqual(events[0].calendar, "Jewish Holidays")
