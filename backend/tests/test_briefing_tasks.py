import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.models.schemas import AgentReply, Briefing
from app.services import hermes
from app.services.hermes import (
    _briefing_prompt,
    day_is_done,
    describe_http_error,
    enrich_calendar_for_prompt,
    parse_briefing_reply,
    pin_suggested_tasks,
)


TASKS = [
    {"id": "aaa", "name": "File expenses"},
    {"id": "bbb", "name": "Draft Kikodo proposal"},
    {"id": "ccc", "name": "Call dentist"},
    {"id": "ddd", "name": "Inbox zero"},
]

NOW_MORNING = {
    "timezone": "Asia/Bangkok",
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
            '{"summary":"Focus.","suggested_task_ids":["Draft Kikodo proposal"]}',
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
            "Highest-value work: Draft Kikodo proposal, then File expenses.",
            TASKS,
        )
        self.assertIn("Draft Kikodo proposal", summary)
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
                        "Best use of the morning: **Draft Kikodo proposal** "
                        "(planned 07:00), which sets up the afternoon meeting."
                    ),
                    "suggested_task_ids": ["bbb"],
                }
            ),
            TASKS,
        )
        self.assertIn("Lunch is at 13:00", summary)
        self.assertIn("**Draft Kikodo proposal**", summary)
        self.assertIn("which sets up the afternoon meeting", summary)
        self.assertEqual(ids, ["bbb"])

    def test_recovers_summary_when_json_has_raw_newlines(self):
        raw = (
            '{\n'
            '  "summary": "**Thursday**\n'
            '\n'
            'Best use of the morning: Draft Kikodo proposal, which sets up the meeting.",\n'
            '  "suggested_task_ids": ["bbb"]\n'
            '}'
        )
        summary, ids = parse_briefing_reply(raw, TASKS)
        self.assertTrue(summary.startswith("**Thursday**"))
        self.assertIn("Draft Kikodo proposal", summary)
        self.assertIn("which sets up the meeting", summary)
        self.assertNotIn("suggested_task_ids", summary)
        self.assertEqual(ids, ["bbb"])


class BriefingPromptFormatTests(unittest.TestCase):
    def test_today_prompt_asks_for_short_prose_paragraphs(self):
        prompt = _briefing_prompt(
            NOW_MORNING,
            [{"title": "Lunch", "start": "2026-09-17T13:00:00+07:00"}],
            [{"id": "bbb", "name": "Draft Kikodo proposal"}],
            tomorrow=None,
            tomorrow_cal=None,
        )
        self.assertIn("short markdown paragraphs", prompt)
        self.assertIn("full sentences, with reasons", prompt)
        self.assertIn("not a labelled inventory", prompt)
        self.assertIn("Do not use labelled section headings", prompt)
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
                [{"id": "bbb", "name": "Draft Kikodo proposal"}],
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
        self.assertFalse(hermes.is_failed_model_reply("Focus on the Kikodo proposal."))


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


class HolidayCalendarFilterTests(unittest.TestCase):
    def test_drops_named_holiday_calendars(self):
        events = [
            {
                "title": "Rosh Hashanah",
                "start": "2026-09-19T00:00:00+07:00",
                "end": "2026-09-20T00:00:00+07:00",
                "all_day": True,
                "calendar": "Jewish Holidays",
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
                    "title": "Yom Kippur",
                    "start": "2026-09-19T00:00:00+07:00",
                    "end": "2026-09-20T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "iCloud / Jewish Holidays",
                }
            ]
        )
        self.assertEqual(kept, [])

    def test_drops_erev_once_calendar_title_is_resolved(self):
        kept = enrich_calendar_for_prompt(
            [
                {
                    "title": "Erev Yom Kippur",
                    "start": "2026-09-20T00:00:00+07:00",
                    "end": "2026-09-21T00:00:00+07:00",
                    "all_day": True,
                    "calendar": "Jewish Holidays",
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

    def tearDown(self):
        self._now_patch.stop()
        hermes._last_good_path_override = None
        hermes._nextday_path_override = None
        hermes._hydrated_last_good = False
        hermes._hydrated_nextday = False
        hermes._briefing_cache = None
        hermes._nextday_cache = None
        self._tmp.cleanup()

    def test_peek_ignores_today_briefing_in_the_evening(self):
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
        self.assertIsNone(hermes.peek_briefing())

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
                hermes.generate_briefing({"calendar": [], "tasks": []}),
                hermes.generate_briefing({"calendar": [], "tasks": []}),
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
            briefing = await hermes.generate_briefing({"calendar": [], "tasks": []})
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
                {"calendar": [], "tasks": []},
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
                {"calendar": [], "tasks": []},
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
                {"calendar": [], "tasks": []},
                force=True,
            )
            second = await hermes.generate_briefing(
                {"calendar": [], "tasks": []},
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
            await hermes.generate_briefing({"calendar": [], "tasks": []})
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
            "tasks": TASKS,
            "tasks_tomorrow": [
                {
                    "id": "bbb",
                    "name": "Draft Kikodo proposal",
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
        self.assertIn("Draft Kikodo proposal", prompts[0])
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
                    "tasks": TASKS,
                    "tasks_tomorrow": [
                        {"id": "bbb", "name": "Draft Kikodo proposal"}
                    ],
                },
                force=True,
            )
        self.assertEqual(briefing.source, "fallback")
        self.assertNotIn("On Deck", briefing.summary)
        spawn.assert_not_called()


if __name__ == "__main__":
    unittest.main()
