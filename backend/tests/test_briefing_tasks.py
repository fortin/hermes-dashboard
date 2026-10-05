import asyncio
import json
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.models.schemas import AgentReply, Briefing, markdown_paragraphs, strip_leading_date
from app.services import hermes
from app.services.hermes import (
    _briefing_prompt,
    _drop_invented_task_lines,
    day_is_done,
    describe_http_error,
    enrich_calendar_for_prompt,
    omit_finished_events,
    parse_briefing_reply,
    pin_suggested_tasks,
)


TASKS = [
    {"id": "aaa", "name": "File expenses"},
    {"id": "bbb", "name": "Draft quarterly proposal"},
    {"id": "ccc", "name": "Call dentist"},
    {"id": "ddd", "name": "Inbox zero"},
]

STILL_AHEAD = {
    "title": "Lunch",
    "start": "2026-09-17T13:00:00+07:00",
    "end": "2026-09-17T14:00:00+07:00",
}

NOW_MORNING = {
    "timezone": "UTC",
    "iso": "2026-09-17T08:05+07:00",
    "date": "2026-09-17",
    "weekday": "Thursday",
    "human": "Thursday 17 September 2026, 08:05",
    "time_of_day": "morning",
    "hour": 8,
    "minute": 5,
    "next_shabbat_starts_on": "2026-09-18",
    "is_shabbat": False,
}

NOW_AFTERNOON = {
    **NOW_MORNING,
    "iso": "2026-09-17T15:11+07:00",
    "human": "Thursday 17 September 2026, 15:11",
    "time_of_day": "afternoon",
    "hour": 15,
    "minute": 11,
}

NOW_EVENING = {
    **NOW_MORNING,
    "iso": "2026-09-17T20:25+07:00",
    "human": "Thursday 17 September 2026, 20:25",
    "time_of_day": "evening",
    "hour": 20,
    "minute": 25,
}


class ParseBriefingReplyTests(unittest.TestCase):
    def test_extracts_ids_from_json(self):
        summary, ids = parse_briefing_reply(
            '{"summary":"Do the proposal first.","suggested_task_ids":["bbb","ccc"]}',
            TASKS,
        )
        self.assertEqual(summary, "Do the proposal first.")
        self.assertEqual(ids, ["bbb", "ccc"])

    def test_accepts_names_instead_of_ids(self):
        summary, ids = parse_briefing_reply(
            '{"summary":"Focus.","suggested_task_ids":["Draft quarterly proposal"]}',
            TASKS,
        )
        self.assertEqual(summary, "Focus.")
        self.assertEqual(ids, ["bbb"])

    def test_drops_unknown_ids(self):
        _, ids = parse_briefing_reply(
            '{"summary":"x","suggested_task_ids":["nope","aaa"]}',
            TASKS,
        )
        self.assertEqual(ids, ["aaa"])

    def test_matches_names_in_prose_when_json_missing(self):
        summary, ids = parse_briefing_reply(
            "Highest-value work: Draft quarterly proposal, then File expenses.",
            TASKS,
        )
        self.assertIn("Draft quarterly proposal", summary)
        self.assertEqual(ids, ["bbb", "aaa"])

    def test_ignores_short_partial_name_hits(self):
        _, ids = parse_briefing_reply(
            "Do not call this a dentist appointment unless it is Call dentist.",
            TASKS,
        )
        self.assertEqual(ids, ["ccc"])

    def test_keeps_multiline_markdown_summary(self):
        summary, ids = parse_briefing_reply(
            json.dumps(
                {
                    "summary": (
                        "**Thursday 17 September**\n\n"
                        "Lunch is at 13:00, then Caroline class 15:00–16:00.\n\n"
                        "Best use of the morning: **Draft quarterly proposal** "
                        "(planned 07:00), which sets up the afternoon meeting."
                    ),
                    "suggested_task_ids": ["bbb"],
                }
            ),
            TASKS,
        )
        self.assertIn("Lunch is at 13:00", summary)
        self.assertIn("**Draft quarterly proposal**", summary)
        self.assertIn("which sets up the afternoon meeting", summary)
        self.assertEqual(ids, ["bbb"])

    def test_recovers_summary_when_json_has_raw_newlines(self):
        raw = (
            '{\n'
            '  "summary": "2026-09-24\n'
            '\n'
            'Best use of the morning: Draft quarterly proposal, which sets up the meeting.",\n'
            '  "suggested_task_ids": ["bbb"]\n'
            '}'
        )
        summary, ids = parse_briefing_reply(raw, TASKS)
        self.assertTrue(summary.startswith("Best use of the morning"))
        self.assertNotIn("2026-09-24", summary)
        self.assertIn("Draft quarterly proposal", summary)
        self.assertIn("which sets up the meeting", summary)
        self.assertNotIn("suggested_task_ids", summary)
        self.assertEqual(ids, ["bbb"])

    def test_drops_a_leading_date_line_and_keeps_dates_in_prose(self):
        summary, _ids = parse_briefing_reply(
            '{"summary":"**Thursday 2026-09-24**\\n\\nLunch is at 13:00 on Thursday.",'
            '"suggested_task_ids":[]}',
            TASKS,
        )
        self.assertEqual(summary, "Lunch is at 13:00 on Thursday.")
        self.assertEqual(
            strip_leading_date("Friday\n\nCaroline's class is first."),
            "Caroline's class is first.",
        )
        kept = Briefing(
            summary="Lunch is at 13:00.",
            generated_at="2026-09-24T13:00:00+07:00",
        )
        self.assertEqual(kept.summary, "Lunch is at 13:00.")

    def test_single_newlines_become_paragraphs(self):
        summary, _ids = parse_briefing_reply(
            '{"summary":"Friday starts with Caroline.\\nThen draft the proposal.","suggested_task_ids":[]}',
            TASKS,
        )
        self.assertEqual(summary, "Friday starts with Caroline.\n\nThen draft the proposal.")
        self.assertEqual(
            markdown_paragraphs("Already split.\n\nStay split."),
            "Already split.\n\nStay split.",
        )


class BriefingPromptFormatTests(unittest.TestCase):
    def test_today_prompt_asks_for_short_prose_paragraphs(self):
        prompt = _briefing_prompt(
            NOW_MORNING,
            [{"title": "Lunch", "start": "2026-09-17T13:00:00+07:00"}],
            [{"id": "bbb", "name": "Draft quarterly proposal"}],
            tomorrow=None,
            tomorrow_cal=None,
        )
        self.assertIn("short markdown paragraphs", prompt)
        self.assertIn("full sentences, with reasons", prompt)
        self.assertIn("not a labelled inventory", prompt)
        self.assertIn("Do not open with the date", prompt)
        self.assertIn("Never invent", prompt)
        self.assertIn("Do not use labelled section headings", prompt)
        self.assertNotIn("weekday date", prompt)
        self.assertNotIn("max 120 words, no preamble", prompt)

    def test_tomorrow_prompt_asks_for_the_same_prose(self):
        with patch.object(
            hermes,
            "get_settings",
            return_value=type("S", (), {"omnifocus_tomorrow_perspective": "Forecast"})(),
        ):
            prompt = _briefing_prompt(
                NOW_EVENING,
                [],
                [{"id": "bbb", "name": "Draft quarterly proposal"}],
                tomorrow={
                    "weekday": "Friday",
                    "date": "2026-09-18",
                    "human": "Friday 18 September 2026",
                },
                tomorrow_cal=[{"title": "Caroline class"}],
            )
        self.assertIn("short markdown paragraphs", prompt)
        self.assertIn("full sentences, with reasons", prompt)
        self.assertIn("Forecast", prompt)

    def test_empty_task_list_forbids_inventing_work(self):
        prompt = _briefing_prompt(
            NOW_MORNING,
            [{"title": "Lunch", "start": "2026-09-17T13:00:00+07:00"}],
            [],
            tomorrow=None,
            tomorrow_cal=None,
        )
        self.assertIn("(none)", prompt)
        self.assertIn("The task list is empty", prompt)
        self.assertIn("Do not name, suggest, or invent", prompt)
        self.assertNotIn("other tasks from that list", prompt)
        self.assertNotIn("realistic tasks", prompt)
        self.assertNotIn("[]", prompt.split("On Deck tasks", 1)[-1].split("Produce", 1)[0])

    def test_drops_invented_task_bullets_when_nothing_is_queued(self):
        summary = _drop_invented_task_lines(
            "You are free until the 13:00 lunch.\n\n"
            "Other realistic tasks for this time include:\n\n"
            "- Review Project X Documents: This task will help you stay up-to-date.\n"
            "- Respond to Emails from Inbox 1: Clearing out your Inbox 1 will help.\n"
        )
        self.assertIn("free until the 13:00 lunch", summary)
        self.assertNotIn("Project X", summary)
        self.assertNotIn("Inbox 1", summary)
        self.assertNotIn("realistic tasks", summary.lower())
        prose = _drop_invented_task_lines(
            "You have a lunch meeting from 13:00 to 14:00. After lunch, the "
            "afternoon is free. This time could be ideal for catching up on "
            "emails or focusing on any pending work."
        )
        self.assertIn("afternoon is free", prose)
        self.assertNotIn("emails", prose.lower())
        self.assertNotIn("pending work", prose.lower())

    def test_rewrites_converted_event_time_to_listed_local_start(self):
        events = [
            {"title": "Caroline class", "start_local": "00:00", "end_local": "01:00"},
            {"title": "Caroline class", "start_local": "14:00", "end_local": "15:00"},
            {"title": "Caroline class", "start_local": "23:00", "end_local": "00:00"},
        ]
        summary = hermes.correct_event_clocks(
            "**Caroline class** at 06:00 is the first timed commitment, requiring an early start. "
            "**Clean pre-filters** are scheduled for 07:00.",
            events,
        )
        self.assertIn("**Caroline class** at 00:00 is the first", summary)
        self.assertNotIn("06:00", summary)
        self.assertIn("07:00", summary)
        kept = hermes.correct_event_clocks(
            "**Caroline class** at 14:00 is the afternoon session.",
            events,
        )
        self.assertIn("at 14:00", kept)

    def test_drops_past_events_and_unlisted_on_deck_items(self):
        clock = datetime.fromisoformat("2026-09-24T15:11:00+07:00")
        kept = omit_finished_events(
            [
                {
                    "title": "Lunch",
                    "date": "2026-09-24",
                    "start_local": "13:00",
                    "end_local": "14:00",
                    "all_day": False,
                },
                {
                    "title": "Call",
                    "date": "2026-09-24",
                    "start_local": "16:00",
                    "end_local": "16:30",
                    "all_day": False,
                },
            ],
            clock,
        )
        self.assertEqual([event["title"] for event in kept], ["Call"])
        summary = _drop_invented_task_lines(
            "You have a lunch meeting from 13:00 to 14:00. After that, you're "
            "free until your next task at 15:55.\n\n"
            "In the meantime, consider tackling a few other quick tasks from "
            "your On Deck list:\n\n"
            "* Check and respond to any urgent emails that may have come in.\n"
            "* Review the agenda for your lunch meeting.\n"
            "* If you have a few spare minutes, start brainstorming ideas.\n",
            [],
            set(),
        )
        self.assertNotIn("13:00", summary)
        self.assertNotIn("15:55", summary)
        self.assertNotIn("emails", summary.lower())
        self.assertNotIn("brainstorm", summary.lower())
        self.assertNotIn("On Deck", summary)


class PinSuggestedTasksTests(unittest.TestCase):
    def test_pins_suggested_in_briefing_order(self):
        tasks = [dict(t) for t in TASKS]
        pinned = pin_suggested_tasks(tasks, ["ccc", "aaa"])
        self.assertEqual([t["id"] for t in pinned], ["ccc", "aaa", "bbb", "ddd"])

    def test_leaves_order_when_no_suggestions(self):
        tasks = [dict(t) for t in TASKS]
        self.assertEqual(pin_suggested_tasks(tasks, []), tasks)


class DescribeHttpErrorTests(unittest.TestCase):
    def test_keeps_type_when_message_is_empty(self):
        class Empty(Exception):
            def __str__(self) -> str:
                return ""

        self.assertEqual(describe_http_error(Empty()), "Empty: no message")

    def test_includes_message_when_present(self):
        self.assertEqual(
            describe_http_error(TimeoutError("timed out")),
            "TimeoutError: timed out",
        )


INTERRUPT = "Operation interrupted: waiting for model response (39.9s elapsed)."


class FailedModelReplyTests(unittest.TestCase):
    def test_detects_gateway_interrupt(self):
        self.assertTrue(hermes.is_failed_model_reply(INTERRUPT))

    def test_rejects_empty(self):
        self.assertTrue(hermes.is_failed_model_reply("  "))

    def test_accepts_real_briefing(self):
        self.assertFalse(hermes.is_failed_model_reply("Focus on the quarterly proposal."))

    def test_rejects_provider_compute_error(self):
        self.assertTrue(
            hermes.is_failed_model_reply(
                "API call failed after 3 retries: Compute error."
            )
        )


class DelegatedTaskTimeoutTests(unittest.TestCase):
    def test_zero_means_no_read_deadline(self):
        with patch.object(
            hermes,
            "get_settings",
            return_value=type("S", (), {"omnifocus_agent_timeout_seconds": 0})(),
        ):
            timeout = hermes.delegated_task_timeout()
        self.assertIsNone(timeout.read)

    def test_positive_seconds_set_read_deadline(self):
        with patch.object(
            hermes,
            "get_settings",
            return_value=type("S", (), {"omnifocus_agent_timeout_seconds": 43200})(),
        ):
            timeout = hermes.delegated_task_timeout()
        self.assertEqual(timeout.read, 43200.0)


class DayIsDoneTests(unittest.TestCase):
    def test_morning_is_not_done(self):
        self.assertFalse(day_is_done([], NOW_MORNING))

    def test_evening_with_no_remaining_events_is_done(self):
        self.assertTrue(day_is_done([], NOW_EVENING))

    def test_ended_event_does_not_block(self):
        calendar = [
            {
                "title": "Caroline class",
                "start": "2026-09-17T16:00:00+07:00",
                "end": "2026-09-17T17:00:00+07:00",
                "all_day": False,
            }
        ]
        self.assertTrue(day_is_done(calendar, NOW_EVENING))

    def test_upcoming_event_keeps_today(self):
        calendar = [
            {
                "title": "Caroline class",
                "start": "2026-09-17T21:00:00+07:00",
                "end": "2026-09-17T22:00:00+07:00",
                "all_day": False,
            }
        ]
        self.assertFalse(day_is_done(calendar, NOW_EVENING))

    def test_all_day_event_does_not_count(self):
        calendar = [
            {
                "title": "Fast",
                "start": "2026-09-17T00:00:00+07:00",
                "end": "2026-09-18T00:00:00+07:00",
                "all_day": True,
            }
        ]
        self.assertTrue(day_is_done(calendar, NOW_EVENING))


class LookAheadTomorrowTests(unittest.TestCase):
    def test_evening_with_on_deck_stays_today(self):
        self.assertFalse(hermes.look_ahead_tomorrow([], TASKS, NOW_EVENING))

    def test_evening_with_empty_deck_plans_tomorrow(self):
        self.assertTrue(hermes.look_ahead_tomorrow([], [], NOW_EVENING))

    def test_afternoon_with_empty_deck_and_open_event_stays_today(self):
        still_ahead = {
            "title": "Caroline class",
            "start": "2026-09-17T16:00:00+07:00",
            "end": "2026-09-17T17:00:00+07:00",
        }
        self.assertFalse(
            hermes.look_ahead_tomorrow([still_ahead], [], NOW_AFTERNOON)
        )

    def test_afternoon_with_empty_deck_and_no_open_events_plans_tomorrow(self):
        past = {
            "title": "Lunch",
            "start": "2026-09-17T12:00:00+07:00",
            "end": "2026-09-17T13:00:00+07:00",
        }
        self.assertTrue(hermes.look_ahead_tomorrow([past], [], NOW_AFTERNOON))


class HolidayCalendarFilterTests(unittest.TestCase):
    def test_drops_named_holiday_calendars(self):
        events = [
            {
                "title": "Rosh Hashanah",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Public Holidays",
            },
            {
                "title": "Bank Holiday",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Holidays in the United Kingdom",
            },
            {
                "title": "National Day",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Holidays in Switzerland",
            },
            {
                "title": "Fiesta",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Holidays in Spain",
            },
            {
                "title": "Songkran",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Thai Holidays",
            },
            {
                "title": "Observance",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Public Holidays and Observances",
            },
            {
                "title": "Caroline class",
                "start": "2026-09-19T16:00:00+07:00",
                "end": "2026-09-19T17:00:00+07:00",
                "all_day": False,
                "calendar": "Family",
            },
        ]
        kept = enrich_calendar_for_prompt(events)
        self.assertEqual([e["title"] for e in kept], ["Caroline class"])

    def test_drops_uk_calendar_without_the(self):
        kept = enrich_calendar_for_prompt(
            [
                {
                    "title": "Bank Holiday",
                    "start": "2026-09-20T00:00:00+07:00",
                    "end": "2026-09-21T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "Holidays in United Kingdom",
                }
            ]
        )
        self.assertEqual(kept, [])

    def test_drops_us_holidays_suffix(self):
        kept = enrich_calendar_for_prompt(
            [
                {
                    "title": "Independence Day",
                    "start": "2026-09-20T00:00:00+07:00",
                    "end": "2026-09-21T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "US Holidays",
                }
            ]
        )
        self.assertEqual(kept, [])

    def test_drops_account_prefixed_holiday_calendar(self):
        kept = enrich_calendar_for_prompt(
            [
                {
                    "title": "National Day",
                    "start": "2026-09-19T00:00:00+07:00",
                    "end": "2026-09-20T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "iCloud / Public Holidays",
                }
            ]
        )
        self.assertEqual(kept, [])

    def test_drops_holiday_once_calendar_title_is_resolved(self):
        kept = enrich_calendar_for_prompt(
            [
                {
                    "title": "Holiday Eve",
                    "start": "2026-09-20T00:00:00+07:00",
                    "end": "2026-09-21T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "Public Holidays",
                },
                {
                    "title": "Weekly Review",
                    "start": "2026-09-20T19:00:00+07:00",
                    "end": "2026-09-20T20:00:00+07:00",
                    "all_day": False,
                    "calendar": "Personal",
                },
            ]
        )
        self.assertEqual([e["title"] for e in kept], ["Weekly Review"])


class GenerateBriefingCoalesceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        hermes._briefing_cache = None
        hermes._briefing_generation = 0
        hermes._hydrated_last_good = True
        hermes._last_good_path_override = Path(self._tmp.name) / "last-briefing.json"
        hermes._nextday_cache = None
        hermes._hydrated_nextday = True
        hermes._nextday_path_override = Path(self._tmp.name) / "nextday-plan.json"
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_MORNING
        )
        self._now_patch.start()
        # Existing cases cover the Hermes fallback. Apple Intelligence is stubbed off.
        self._apple_patch = patch.object(
            hermes,
            "ask_apple",
            AsyncMock(side_effect=hermes.HermesUnavailable("apple off in test")),
        )
        self._apple_patch.start()

    def tearDown(self):
        self._apple_patch.stop()
        self._now_patch.stop()
        hermes._last_good_path_override = None
        hermes._nextday_path_override = None
        hermes._hydrated_last_good = False
        hermes._hydrated_nextday = False
        hermes._briefing_cache = None
        hermes._nextday_cache = None
        self._tmp.cleanup()

    async def test_empty_deck_previews_tomorrow(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_AFTERNOON
        )
        self._now_patch.start()
        seen: list[str] = []

        async def fake_ask(message, *_a, **_k):
            seen.append(message)
            return AgentReply(
                reply='{"summary":"Friday starts with Caroline, then the proposal.","suggested_task_ids":["bbb"]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [
                        {
                            "title": "Lunch",
                            "start": "2026-09-17T13:00:00+07:00",
                            "end": "2026-09-17T14:00:00+07:00",
                        }
                    ],
                    "calendar_tomorrow": [
                        {
                            "title": "Caroline class",
                            "start": "2026-09-18T09:00:00+07:00",
                            "end": "2026-09-18T10:00:00+07:00",
                        }
                    ],
                    "tasks": [],
                    "tasks_tomorrow": [
                        {"id": "bbb", "name": "Draft quarterly proposal"}
                    ],
                },
                force=True,
            )
        self.assertEqual(len(seen), 1)
        self.assertIn("Caroline class", seen[0])
        self.assertIn("Draft quarterly proposal", seen[0])
        self.assertIn("Tomorrow > 5 minutes", seen[0])
        self.assertNotIn("Lunch", seen[0])
        self.assertEqual(briefing.horizon, "tomorrow")
        self.assertIn("Caroline", briefing.summary)
        self.assertEqual(briefing.suggested_task_ids, ["bbb"])

        ask_again = AsyncMock(
            return_value=AgentReply(
                reply='{"summary":"**File expenses** before the evening winds down.","suggested_task_ids":["aaa"]}',
                model="x",
            )
        )
        with (
            patch.object(hermes, "ask_hermes", ask_again),
            patch.object(hermes, "_spawn_publish"),
        ):
            again = await hermes.generate_briefing(
                {
                    "calendar": [STILL_AHEAD],
                    "tasks": TASKS,
                },
                force=True,
            )
        ask_again.assert_awaited()
        self.assertEqual(again.horizon, "today")
        self.assertIn("File expenses", again.summary)

    async def test_unavailable_calendar_is_not_briefed_as_an_empty_day(self):
        ask = AsyncMock()
        with patch.object(hermes, "ask_hermes", ask):
            briefing = await hermes.generate_briefing({"calendar_unavailable": True})
        ask.assert_not_awaited()
        self.assertEqual(briefing.source, "fallback")
        self.assertIn("couldn't be loaded", briefing.summary)
        self.assertNotIn("nothing", briefing.summary.lower())

    def test_peek_ignores_stale_today_briefing_in_the_evening(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        hermes._briefing_cache = (
            time.monotonic() - hermes._BRIEFING_TTL_S - 1,
            Briefing(
                summary="On Deck: empty — no queued tasks.",
                generated_at="2026-09-17T16:27:00+07:00",
                source="hermes",
                horizon="today",
            ),
        )
        self.assertIsNone(hermes.peek_briefing())

    def test_peek_serves_fresh_today_briefing_in_the_evening(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        hermes._briefing_cache = (
            time.monotonic(),
            Briefing(
                summary="**File expenses** with the time left today.",
                generated_at="2026-09-17T20:30:00+07:00",
                source="hermes",
                horizon="today",
            ),
        )
        peeked = hermes.peek_briefing()
        self.assertIsNotNone(peeked)
        self.assertEqual(peeked.horizon, "today")
        self.assertIn("File expenses", peeked.summary)

    async def test_concurrent_calls_share_one_hermes_run(self):
        calls = 0

        async def fake_ask(*_a, **_k):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return AgentReply(
                reply='{"summary":"Focus on the ad.","suggested_task_ids":[]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            first, second = await asyncio.gather(
                hermes.generate_briefing({"calendar": [STILL_AHEAD], "tasks": []}),
                hermes.generate_briefing({"calendar": [STILL_AHEAD], "tasks": []}),
            )
        self.assertEqual(calls, 1)
        self.assertEqual(first.summary, "Focus on the ad.")
        self.assertEqual(second.summary, first.summary)
        self.assertEqual(hermes.briefing_generation(), 1)

    async def test_interrupt_is_fallback_and_not_published(self):
        async def fake_ask(*_a, **_k):
            return AgentReply(reply=INTERRUPT, model="x")

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish") as spawn,
        ):
            briefing = await hermes.generate_briefing({"calendar": [STILL_AHEAD], "tasks": []})
        self.assertEqual(briefing.source, "fallback")
        self.assertNotIn("Operation interrupted", briefing.summary)
        self.assertNotIn("ConnectError", briefing.summary)
        spawn.assert_not_called()
        self.assertEqual(hermes.briefing_generation(), 0)

    async def test_interrupt_keeps_last_good_briefing(self):
        hermes._briefing_cache = (
            0.0,
            Briefing(
                summary="Focus on the ad.",
                generated_at="2026-09-17T08:05:00+07:00",
                source="hermes",
                suggested_task_ids=[],
            ),
        )
        hermes._briefing_generation = 1

        async def fake_ask(*_a, **_k):
            return AgentReply(
                reply='{"summary":"' + INTERRUPT + '","suggested_task_ids":[]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish") as spawn,
        ):
            briefing = await hermes.generate_briefing(
                {"calendar": [STILL_AHEAD], "tasks": []},
                force=True,
            )
        self.assertEqual(briefing.summary, "Focus on the ad.")
        self.assertEqual(briefing.source, "hermes")
        spawn.assert_not_called()
        self.assertEqual(hermes.briefing_generation(), 1)

    async def test_connect_failure_returns_persisted_briefing(self):
        path = hermes.last_good_path()
        path.write_text(
            Briefing(
                summary="Focus on the ad.",
                generated_at="2026-09-17T08:05:00+07:00",
                source="hermes",
                suggested_task_ids=[],
            ).model_dump_json(),
            encoding="utf-8",
        )
        hermes._hydrated_last_good = False

        async def fake_ask(*_a, **_k):
            raise hermes.HermesUnavailable(
                "Cannot reach Hermes API at http://127.0.0.1:8642/v1 "
                "after 0.0s (ConnectError: All connection attempts failed)"
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish") as spawn,
        ):
            briefing = await hermes.generate_briefing(
                {"calendar": [STILL_AHEAD], "tasks": []},
                force=True,
            )
        self.assertEqual(briefing.summary, "Focus on the ad.")
        self.assertEqual(briefing.source, "hermes")
        spawn.assert_not_called()

    async def test_connect_failure_recaches_last_good(self):
        last = Briefing(
            summary="Focus on the ad.",
            generated_at="2026-09-17T08:05:00+07:00",
            source="hermes",
            suggested_task_ids=[],
        )
        hermes._briefing_cache = (0.0, last)
        calls = 0

        async def fake_ask(*_a, **_k):
            nonlocal calls
            calls += 1
            raise hermes.HermesUnavailable(
                "Cannot reach Hermes API at http://127.0.0.1:8642/v1 "
                "after 0.0s (ConnectError: All connection attempts failed)"
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish") as spawn,
        ):
            first = await hermes.generate_briefing(
                {"calendar": [STILL_AHEAD], "tasks": []},
                force=True,
            )
            second = await hermes.generate_briefing(
                {"calendar": [STILL_AHEAD], "tasks": []},
                force=False,
            )
        self.assertEqual(calls, 1)
        self.assertEqual(first.summary, "Focus on the ad.")
        self.assertEqual(second.summary, first.summary)
        spawn.assert_not_called()

    async def test_peek_serves_stale_last_good_while_hermes_busy(self):
        hermes._briefing_cache = (
            0.0,
            Briefing(
                summary="Focus on the ad.",
                generated_at="2026-09-17T08:05:00+07:00",
                source="hermes",
            ),
        )
        self.assertIsNone(hermes.peek_briefing())
        async with hermes._hermes_lock:
            peeked = hermes.peek_briefing()
        self.assertIsNotNone(peeked)
        self.assertEqual(peeked.summary, "Focus on the ad.")

    async def test_persists_last_good(self):
        async def fake_ask(*_a, **_k):
            return AgentReply(
                reply='{"summary":"Focus on the ad.","suggested_task_ids":[]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            await hermes.generate_briefing({"calendar": [STILL_AHEAD], "tasks": []})
        saved = Briefing.model_validate_json(
            hermes.last_good_path().read_text(encoding="utf-8")
        )
        self.assertEqual(saved.summary, "Focus on the ad.")
        self.assertEqual(saved.source, "hermes")

    async def test_evening_plans_tomorrow_once(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        calls = 0
        prompts: list[str] = []

        async def fake_ask(message, *_a, **_k):
            nonlocal calls
            calls += 1
            prompts.append(message)
            return AgentReply(
                reply='{"summary":"Tomorrow start with Caroline, then the proposal.","suggested_task_ids":["bbb"]}',
                model="x",
            )

        context = {
            "calendar": [],
            "calendar_tomorrow": [
                {
                    "title": "Caroline class",
                    "start": "2026-09-18T09:00:00+07:00",
                    "end": "2026-09-18T10:00:00+07:00",
                    "all_day": False,
                }
            ],
            "tasks": [],
            "tasks_tomorrow": [
                {
                    "id": "bbb",
                    "name": "Draft quarterly proposal",
                    "due": "2026-09-18",
                    "defer": None,
                    "planned": "2026-09-18",
                }
            ],
        }
        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
            patch.object(hermes, "_load_tomorrow_calendar", side_effect=AssertionError("should use context")),
            patch.object(hermes, "_load_tomorrow_tasks", side_effect=AssertionError("should use context")),
        ):
            first = await hermes.generate_briefing(context)
            second = await hermes.generate_briefing(context)
        self.assertEqual(calls, 1)
        self.assertEqual(first.horizon, "tomorrow")
        self.assertEqual(second.summary, first.summary)
        self.assertIn("TOMORROW", prompts[0])
        self.assertIn("Caroline class", prompts[0])
        self.assertIn("Draft quarterly proposal", prompts[0])
        self.assertIn("Tomorrow > 5 minutes", prompts[0])
        self.assertNotIn("File expenses", prompts[0])
        self.assertFalse(hermes.last_good_path().exists())
        saved = json.loads(hermes.nextday_path().read_text(encoding="utf-8"))
        self.assertEqual(saved["kind"], "perspective-v2")
        self.assertEqual(saved["date"], "2026-09-17")
        self.assertEqual(saved["briefing"]["horizon"], "tomorrow")

    async def test_nextday_plan_reloads_from_disk(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        calls = 0

        async def fake_ask(*_a, **_k):
            nonlocal calls
            calls += 1
            return AgentReply(
                reply='{"summary":"Friday morning: Caroline first.","suggested_task_ids":[]}',
                model="x",
            )

        context = {
            "calendar": [],
            "calendar_tomorrow": [],
            "tasks": [],
            "tasks_tomorrow": [],
        }
        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            await hermes.generate_briefing(context)
            hermes._nextday_cache = None
            hermes._hydrated_nextday = False
            hermes._briefing_cache = None
            again = await hermes.generate_briefing(context)
        self.assertEqual(calls, 1)
        self.assertEqual(again.horizon, "tomorrow")
        self.assertIn("Caroline", again.summary)

    async def test_evening_with_on_deck_stays_today(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        prompts: list[str] = []

        async def fake_ask(message, *_a, **_k):
            prompts.append(message)
            return AgentReply(
                reply='{"summary":"**File expenses** before the evening winds down.","suggested_task_ids":["aaa"]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [],
                    "calendar_tomorrow": [
                        {
                            "title": "Caroline class",
                            "start": "2026-09-18T09:00:00+07:00",
                            "end": "2026-09-18T10:00:00+07:00",
                        }
                    ],
                    "tasks": TASKS,
                    "tasks_tomorrow": [
                        {"id": "bbb", "name": "Draft quarterly proposal"}
                    ],
                },
                force=True,
            )
        self.assertEqual(briefing.horizon, "today")
        self.assertIn("File expenses", briefing.summary)
        self.assertEqual(len(prompts), 1)
        self.assertNotIn("TOMORROW", prompts[0])
        self.assertIn("File expenses", prompts[0])
        self.assertNotIn("Caroline class", prompts[0])

    async def test_held_tomorrow_reverts_when_on_deck_has_tasks(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()

        async def fake_tomorrow(*_a, **_k):
            return AgentReply(
                reply='{"summary":"Friday morning: Caroline first.","suggested_task_ids":["bbb"]}',
                model="x",
            )

        empty = {
            "calendar": [],
            "calendar_tomorrow": [
                {
                    "title": "Caroline class",
                    "start": "2026-09-18T09:00:00+07:00",
                    "end": "2026-09-18T10:00:00+07:00",
                }
            ],
            "tasks": [],
            "tasks_tomorrow": [
                {"id": "bbb", "name": "Draft quarterly proposal"}
            ],
        }
        with (
            patch.object(hermes, "ask_hermes", fake_tomorrow),
            patch.object(hermes, "_spawn_publish"),
        ):
            held = await hermes.generate_briefing(empty)
        self.assertEqual(held.horizon, "tomorrow")

        async def fake_today(message, *_a, **_k):
            return AgentReply(
                reply='{"summary":"**File expenses** with the time left today.","suggested_task_ids":["aaa"]}',
                model="x",
            )

        with (
            patch.object(hermes, "ask_hermes", fake_today),
            patch.object(hermes, "_spawn_publish"),
        ):
            # Refresh without force: held tomorrow must not win over On Deck work.
            again = await hermes.generate_briefing(
                {"calendar": [], "tasks": TASKS},
                force=False,
            )
        self.assertEqual(again.horizon, "today")
        self.assertIn("File expenses", again.summary)
        self.assertIsNone(hermes._nextday_for("2026-09-17"))
        self.assertFalse(hermes.nextday_path().exists())
        # SSE must not resurrect tomorrow after a today regen.
        peek = hermes.peek_briefing()
        self.assertIsNotNone(peek)
        self.assertEqual(peek.horizon, "today")

    async def test_evening_failure_does_not_return_today_briefing(self):
        self._now_patch.stop()
        self._now_patch = patch.object(
            hermes, "local_now_context", return_value=NOW_EVENING
        )
        self._now_patch.start()
        hermes._briefing_cache = (
            time.monotonic(),
            Briefing(
                summary="On Deck: empty — no queued tasks.",
                generated_at="2026-09-17T20:27:00+07:00",
                source="hermes",
                horizon="today",
            ),
        )

        async def fake_ask(*_a, **_k):
            raise hermes.HermesUnavailable("Cannot reach Hermes")

        with (
            patch.object(hermes, "ask_hermes", fake_ask),
            patch.object(hermes, "_spawn_publish") as spawn,
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [],
                    "calendar_tomorrow": [],
                    "tasks": [],
                    "tasks_tomorrow": [
                        {"id": "bbb", "name": "Draft quarterly proposal"}
                    ],
                },
                force=True,
            )
        self.assertEqual(briefing.source, "fallback")
        self.assertNotIn("On Deck", briefing.summary)
        spawn.assert_not_called()

    async def test_apple_briefing_skips_hermes_and_is_cached(self):
        seen: list[str] = []

        async def fake_apple(message: str) -> AgentReply:
            seen.append(message)
            return AgentReply(
                reply='{"summary":"Focus on **File expenses**.","suggested_task_ids":["aaa"]}',
                model="apple-intelligence",
            )

        ask = AsyncMock()
        with (
            patch.object(hermes, "ask_apple", fake_apple),
            patch.object(hermes, "ask_hermes", ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [],
                    "tasks": [
                        {"id": "aaa", "name": "File expenses"},
                        {"id": "zzz", "name": "Last task in the long list"},
                    ],
                }
            )
        ask.assert_not_awaited()
        self.assertEqual(len(seen), 1)
        self.assertNotIn("AUTHORITATIVE CLOCK", seen[0])
        self.assertIn("Triage", seen[0])
        self.assertIn("zzz", seen[0])
        self.assertIn("Last task in the long list", seen[0])
        self.assertLessEqual(len(seen[0]), hermes._APPLE_PROMPT_MAX)
        self.assertEqual(briefing.source, "apple")
        self.assertIn("File expenses", briefing.summary)
        saved = Briefing.model_validate_json(
            hermes.last_good_path().read_text(encoding="utf-8")
        )
        self.assertEqual(saved.source, "apple")
        self.assertEqual(hermes.briefing_generation(), 1)

    async def test_vague_apple_briefing_falls_back_to_hermes(self):
        async def fake_apple(_message: str) -> AgentReply:
            return AgentReply(
                reply=(
                    '{"summary":"Completing this task will help you stay updated '
                    'on potential job opportunities.","suggested_task_ids":[]}'
                ),
                model="apple-intelligence",
            )

        async def fake_hermes(*_a, **_k):
            return AgentReply(
                reply='{"summary":"Work on **Review: Jobs scan**.","suggested_task_ids":["aaa"]}',
                model="hermes-agent",
            )

        with (
            patch.object(hermes, "ask_apple", fake_apple),
            patch.object(hermes, "ask_hermes", fake_hermes),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [],
                    "tasks": [{"id": "aaa", "name": "Review: Jobs scan"}],
                }
            )
        self.assertEqual(briefing.source, "hermes")
        self.assertIn("Review: Jobs scan", briefing.summary)

    async def test_inventory_apple_briefing_falls_back_to_hermes(self):
        inventory = (
            "Lunch (13:00-14:00): This is a scheduled meal break and should be taken as planned.\n"
            "Caroline class (15:00-16:00): This is an educational commitment and should be attended as scheduled.\n"
            "Do Chop Builder (08:00): This is a creative project.\n"
            "Do Over 40 Ab Solution (09:00): This is a health commitment.\n"
            "Reply to Attensify (10:00): This is a response to a colleague."
        )

        async def fake_apple(_message: str) -> AgentReply:
            return AgentReply(
                reply=json.dumps({"summary": inventory, "suggested_task_ids": []}),
                model="apple-intelligence",
            )

        async def fake_hermes(*_a, **_k):
            return AgentReply(
                reply='{"summary":"Use the gap before **Caroline class** for **Jobs scan**.","suggested_task_ids":["aaa"]}',
                model="hermes-agent",
            )

        with (
            patch.object(hermes, "ask_apple", fake_apple),
            patch.object(hermes, "ask_hermes", fake_hermes),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing(
                {
                    "calendar": [STILL_AHEAD],
                    "tasks": [
                        {"id": "aaa", "name": "Jobs scan"},
                        {"id": "bbb", "name": "Do Chop Builder"},
                        {"id": "ccc", "name": "Do Over 40 Ab Solution"},
                        {"id": "ddd", "name": "Reply to Attensify"},
                        {"id": "eee", "name": "Watch OmniFocus 4 courses"},
                    ],
                }
            )
        self.assertEqual(briefing.source, "hermes")
        self.assertIn("Jobs scan", briefing.summary)

    def test_apple_briefing_prompt_triages(self):
        prompt = hermes.apple_briefing_prompt(
            NOW_MORNING,
            [],
            [{"id": "aaa", "name": "File expenses", "due": "2026-09-17"}],
        )
        self.assertIn("File expenses", prompt)
        self.assertIn("Triage", prompt)
        self.assertIn("leave", prompt.lower())
        self.assertLessEqual(len(prompt), hermes._APPLE_PROMPT_MAX)

    def test_vague_apple_reply_without_task_name_is_unusable(self):
        self.assertTrue(
            hermes.apple_briefing_unusable(
                "Completing this task will help you stay updated.",
                ["Review: Jobs scan"],
            )
        )
        self.assertFalse(
            hermes.apple_briefing_unusable(
                "Complete **Review: Jobs scan** so you stay updated.",
                ["Review: Jobs scan"],
            )
        )
        named = hermes.name_dangling_opener(
            "This LinkedIn task is time-sensitive as it keeps your network engaged. "
            "Following this, **Consolidate master cheat-sheet** (planned 07:00).",
            [
                "Check LinkedIn message requests folder",
                "Consolidate master cheat-sheet",
            ],
        )
        self.assertTrue(named.startswith("**Check LinkedIn message requests folder.**"))
        self.assertIn("keeps your network engaged", named)
        opener = hermes.name_dangling_opener(
            "This will free up time later for more focused tasks. "
            "This newsletter work will ensure everything is finished before the afternoon.\n\n"
            "With these tasks out of the way, **Publish Issue 38** should be worked on. "
            "This will allow for progress before the drive.",
            ["Clean MBP", "Backup MBP", "Publish Issue 38"],
            all_names=[
                "Clean MBP",
                "Backup MBP",
                "Publish Issue 38",
                "Run The Compliance Unlock newsletter",
            ],
        )
        # First sentence has no task-token overlap → dropped. Second matches newsletter.
        self.assertTrue(
            opener.startswith("**Run The Compliance Unlock newsletter.** This newsletter")
        )
        self.assertIn("With these tasks out of the way, **Publish Issue 38**", opener)
        self.assertNotIn("**Clean MBP.**", opener)
        self.assertNotIn("**Backup MBP.**", opener)
        embassy = hermes.name_dangling_opener(
            "It's crucial and time-sensitive, requiring immediate attention to "
            "ensure it's completed before the embassy closes. The single best "
            "morning task is **Read 'Deep learning' by Goodfellow, Ian**, "
            "planned for 07:00.",
            ["Read 'Deep learning' by Goodfellow, Ian"],
        )
        self.assertTrue(embassy.startswith("The single best morning task"))
        self.assertNotIn("embassy", embassy.lower())
        self.assertIn("Deep learning", embassy)

    def test_inventory_apple_reply_is_unusable(self):
        text = (
            "Lunch (13:00-14:00): This is a scheduled meal break and should be taken as planned.\n"
            "Caroline class (15:00-16:00): This is an educational commitment and should be attended as scheduled.\n"
            "Do Chop Builder (08:00): This is a creative project."
        )
        self.assertTrue(hermes.apple_briefing_unusable(text, ["Do Chop Builder"]))

    async def test_looped_apple_briefing_falls_back_to_hermes(self):
        blob = '{"summary":"Use only listed events.","suggested_task_ids":[]}'

        async def fake_apple(_message: str) -> AgentReply:
            return AgentReply(reply="\n".join([blob] * 4), model="apple-intelligence")

        async def fake_hermes(*_a, **_k):
            return AgentReply(
                reply='{"summary":"Focus on the ad.","suggested_task_ids":[]}',
                model="hermes-agent",
            )

        with (
            patch.object(hermes, "ask_apple", fake_apple),
            patch.object(hermes, "ask_hermes", fake_hermes),
            patch.object(hermes, "_spawn_publish"),
        ):
            briefing = await hermes.generate_briefing({"calendar": [STILL_AHEAD], "tasks": []})
        self.assertEqual(briefing.source, "hermes")
        self.assertEqual(briefing.summary, "Focus on the ad.")

    async def test_same_omnifocus_and_calendar_skips_regen_after_ttl(self):
        calls = 0

        async def fake_ask(*_a, **_k):
            nonlocal calls
            calls += 1
            return AgentReply(
                reply='{"summary":"Focus on the ad.","suggested_task_ids":[]}',
                model="apple-intelligence",
            )

        context = {
            "calendar": [STILL_AHEAD],
            "tasks": [{"id": "aaa", "name": "File expenses"}],
        }
        with (
            patch.object(hermes, "ask_apple", fake_ask),
            patch.object(hermes, "ask_hermes", AsyncMock()) as ask_hermes,
            patch.object(hermes, "_spawn_publish"),
        ):
            first = await hermes.generate_briefing(context)
            # Expire the short TTL; source fingerprint should still serve the cache.
            assert hermes._briefing_cache is not None
            hermes._briefing_cache = (
                time.monotonic() - hermes._BRIEFING_TTL_S - 1,
                hermes._briefing_cache[1],
            )
            second = await hermes.generate_briefing(context)
        ask_hermes.assert_not_awaited()
        self.assertEqual(calls, 1)
        self.assertEqual(first.summary, second.summary)
        self.assertTrue(first.context_fingerprint)
        self.assertEqual(first.context_fingerprint, second.context_fingerprint)

    async def test_changed_omnifocus_triggers_regen_after_ttl(self):
        calls = 0

        async def fake_ask(*_a, **_k):
            nonlocal calls
            calls += 1
            return AgentReply(
                reply=f'{{"summary":"Pass {calls}.","suggested_task_ids":[]}}',
                model="apple-intelligence",
            )

        with (
            patch.object(hermes, "ask_apple", fake_ask),
            patch.object(hermes, "_spawn_publish"),
        ):
            await hermes.generate_briefing(
                {
                    "calendar": [STILL_AHEAD],
                    "tasks": [{"id": "aaa", "name": "File expenses"}],
                }
            )
            assert hermes._briefing_cache is not None
            hermes._briefing_cache = (
                time.monotonic() - hermes._BRIEFING_TTL_S - 1,
                hermes._briefing_cache[1],
            )
            second = await hermes.generate_briefing(
                {
                    "calendar": [STILL_AHEAD],
                    "tasks": [{"id": "bbb", "name": "Draft quarterly proposal"}],
                }
            )
        self.assertEqual(calls, 2)
        self.assertEqual(second.summary, "Pass 2.")


class AskDayTests(unittest.IsolatedAsyncioTestCase):
    def test_prompt_drops_later_tasks_to_fit(self):
        tasks = [
            {"id": f"id{i:02d}", "name": f"Task number {i} with a long descriptive name"}
            for i in range(40)
        ]
        prompt = hermes.apple_ask_prompt(
            "What should I do before lunch?",
            {"now": NOW_MORNING, "calendar": [], "tasks": tasks},
        )
        self.assertLessEqual(len(prompt), hermes._APPLE_PROMPT_MAX)
        self.assertIn("What should I do before lunch?", prompt)
        self.assertIn("id00", prompt)
        self.assertNotIn("id39", prompt)

    async def test_ask_falls_back_to_hermes(self):
        async def fake_apple(_message: str):
            raise hermes.HermesUnavailable("shortcut missing")

        async def fake_hermes(*_a, **_k):
            return AgentReply(reply="From Hermes.", model="hermes-agent")

        with (
            patch.object(hermes, "ask_apple", fake_apple),
            patch.object(hermes, "ask_hermes", fake_hermes),
        ):
            reply = await hermes.ask_day("What is next?", context={"now": NOW_MORNING})
        self.assertEqual(reply.reply, "From Hermes.")
        self.assertEqual(reply.model, "hermes-agent")


class AskAppleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        hermes._apple_model_down_until = 0.0

    def tearDown(self) -> None:
        hermes._apple_model_down_until = 0.0

    async def test_reads_raw_stdout(self):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate = AsyncMock(return_value=(b"# Hello\n", b""))
        spawn = AsyncMock(return_value=proc)
        with (
            patch.object(hermes, "siri_binary", return_value=Path("/usr/bin/siri")),
            patch.object(hermes.asyncio, "create_subprocess_exec", spawn),
        ):
            reply = await hermes.ask_apple("What is on today?")
        self.assertEqual(reply.reply, "# Hello")
        self.assertEqual(reply.model, "apple-intelligence")
        args, kwargs = spawn.await_args
        self.assertEqual(
            args[:4],
            (
                "/usr/bin/siri",
                "--shortcut",
                hermes.get_settings().siri_shortcut,
                "--raw",
            ),
        )
        self.assertEqual(kwargs["env"]["SIRI_RENDER"], "0")
        sent = proc.communicate.await_args.args[0]
        self.assertEqual(sent, b"What is on today?")

    async def test_nonzero_exit_raises(self):
        proc = AsyncMock()
        proc.returncode = 4
        proc.communicate = AsyncMock(return_value=(b"", b"shortcut missing"))
        with (
            patch.object(hermes, "siri_binary", return_value=Path("/usr/bin/siri")),
            patch.object(
                hermes.asyncio,
                "create_subprocess_exec",
                AsyncMock(return_value=proc),
            ),
        ):
            with self.assertRaises(hermes.HermesUnavailable) as caught:
                await hermes.ask_apple("hi")
        self.assertIn("shortcut missing", str(caught.exception))
        self.assertEqual(hermes._apple_model_down_until, 0.0)

    async def test_model_error_skips_the_next_shortcut_run(self):
        proc = AsyncMock()
        proc.returncode = 1
        proc.communicate = AsyncMock(
            return_value=(b"", b"Error: An error occurred when running the model.")
        )
        spawn = AsyncMock(return_value=proc)
        with (
            patch.object(hermes, "siri_binary", return_value=Path("/usr/bin/siri")),
            patch.object(hermes.asyncio, "create_subprocess_exec", spawn),
        ):
            with self.assertRaises(hermes.HermesUnavailable):
                await hermes.ask_apple("hi")
            with self.assertRaises(hermes.HermesUnavailable) as caught:
                await hermes.ask_apple("again")
        self.assertIn("cooling down", str(caught.exception))
        self.assertEqual(spawn.await_count, 1)


if __name__ == "__main__":
    unittest.main()
