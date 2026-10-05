import unittest
from datetime import datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.localtime import configure_timezone
from app.mcp.client import _normalize_tool_result
from app.services import fantastical

TZ = ZoneInfo("Asia/Bangkok")


class _BangkokTzMixin:
    def setUp(self):
        configure_timezone("Asia/Bangkok")
        super().setUp()


class ParseWhenTests(_BangkokTzMixin, unittest.TestCase):
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


HOLIDAY_ID = "c0baabd4fa006a6ab7bbedb269624e44801bf7d5"
CALENDARS = {HOLIDAY_ID: "Public Holidays"}


class NormalizeEventTests(_BangkokTzMixin, unittest.TestCase):
    def test_resolves_calendar_title_from_id(self):
        event = fantastical._normalize_event(
            {
                "id": f"{HOLIDAY_ID};cal-20260920-821e8c6f",
                "title": "Public Holiday Eve",
                "startDate": "2026-09-20T00:00:00+07:00",
                "endDate": "2026-09-21T00:00:00+07:00",
                "calendarId": HOLIDAY_ID,
            },
            CALENDARS,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.calendar, "Public Holidays")
        self.assertTrue(event.all_day)

    def test_resolves_calendar_from_event_id_prefix(self):
        event = fantastical._normalize_event(
            {
                "id": f"{HOLIDAY_ID};cal-20260920-821e8c6f",
                "title": "Public Holiday Eve",
                "startDate": "2026-09-20T00:00:00+07:00",
                "endDate": "2026-09-21T00:00:00+07:00",
            },
            CALENDARS,
        )
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.calendar, "Public Holidays")

    def test_titles_from_calendars_maps_id_to_title(self):
        titles = fantastical._titles_from_calendars(
            [
                {"id": HOLIDAY_ID, "title": "Public Holidays"},
                {"id": "x", "title": "Family"},
            ]
        )
        self.assertEqual(titles[HOLIDAY_ID], "Public Holidays")
        events = fantastical._events_from_raw(
            {
                "timezone": "UTC",
                "items": [
                    {
                        "id": f"{HOLIDAY_ID};cal-1",
                        "title": "Public Holiday Eve",
                        "startDate": "2026-09-20T00:00:00+07:00",
                        "endDate": "2026-09-21T00:00:00+07:00",
                        "calendarId": HOLIDAY_ID,
                    }
                ],
            },
            titles,
        )
        self.assertEqual(events[0].calendar, "Public Holidays")


class _ToolResult:
    def __init__(self, content, structured=None, is_error=False):
        self.content = content
        self.structuredContent = structured
        self.isError = is_error


class NormalizeToolResultTests(unittest.TestCase):
    def test_connection_failure_text_is_an_error_even_with_empty_items(self):
        result = _ToolResult(
            content=[type("B", (), {"text": "Lost the connection to Fantastical"})()],
            structured={"items": []},
        )
        with self.assertRaises(RuntimeError):
            _normalize_tool_result(result)

    def test_normal_empty_items_stay_empty(self):
        result = _ToolResult(
            content=[type("B", (), {"text": "No results found."})()],
            structured={"items": []},
        )
        self.assertEqual(_normalize_tool_result(result), {"items": []})


class QueryResilientTests(unittest.IsolatedAsyncioTestCase):
    async def test_retries_when_the_first_read_is_empty(self):
        calls = {"n": 0}

        async def locked(when):
            calls["n"] += 1
            self.assertEqual(when, "September 23 2026")
            if calls["n"] == 1:
                return []
            return ["Lunch"]

        reset = AsyncMock()
        with (
            patch.object(fantastical, "_query_locked", side_effect=locked),
            patch.object(fantastical, "_reset_session", reset),
        ):
            events = await fantastical._query_resilient("September 23 2026")
        self.assertEqual(events, ["Lunch"])
        reset.assert_not_awaited()

    async def test_skips_retry_when_events_are_present(self):
        locked = AsyncMock(return_value=["Lunch"])
        with patch.object(fantastical, "_query_locked", locked):
            events = await fantastical._query_resilient("September 23 2026")
        self.assertEqual(events, ["Lunch"])
        locked.assert_awaited_once()

    async def test_resets_session_when_the_query_fails(self):
        locked = AsyncMock(side_effect=[RuntimeError("xpc"), ["Lunch"]])
        reset = AsyncMock()
        with (
            patch.object(fantastical, "_query_locked", locked),
            patch.object(fantastical, "_reset_session", reset),
        ):
            events = await fantastical._query_resilient("September 23 2026")
        self.assertEqual(events, ["Lunch"])
        reset.assert_awaited_once()

    async def test_confirmed_empty_day_stays_empty_without_reconnect(self):
        locked = AsyncMock(return_value=[])
        reset = AsyncMock()
        with (
            patch.object(fantastical, "_query_locked", locked),
            patch.object(fantastical, "_reset_session", reset),
        ):
            events = await fantastical._query_resilient("September 23 2026")
        self.assertEqual(events, [])
        self.assertEqual(locked.await_count, 2)
        reset.assert_not_awaited()

    async def test_resets_when_the_retry_loses_the_connection(self):
        locked = AsyncMock(side_effect=[[], RuntimeError("lost"), ["Caroline class"]])
        reset = AsyncMock()
        with (
            patch.object(fantastical, "_query_locked", locked),
            patch.object(fantastical, "_reset_session", reset),
        ):
            events = await fantastical._query_resilient("September 23 2026")
        self.assertEqual(events, ["Caroline class"])
        reset.assert_awaited_once()


class GetTodayPhraseTests(_BangkokTzMixin, unittest.IsolatedAsyncioTestCase):
    async def test_asks_for_the_concrete_local_date(self):
        captured: dict[str, str] = {}

        async def query(when):
            captured["when"] = when
            return []

        class Fixed(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 9, 23, 11, 30, tzinfo=tz)

        fantastical._today_cache = None
        try:
            with (
                patch.object(fantastical, "_query_when", side_effect=query),
                patch.object(fantastical, "datetime", Fixed),
            ):
                await fantastical.get_today()
        finally:
            fantastical._today_cache = None
        self.assertEqual(captured["when"], "September 23 2026")
