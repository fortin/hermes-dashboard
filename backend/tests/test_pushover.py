import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.schemas import Briefing
from app.services import briefing_push, pushover


def _settings(**overrides):
    base = dict(
        pushover_user_key="u" * 30,
        pushover_api_key="t" * 30,
        briefing_note_path="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _now(**overrides):
    data = {
        "timezone": "UTC",
        "date": "2026-09-17",
        "weekday": "Thursday",
        "time_of_day": "morning",
        "hour": 8,
    }
    data.update(overrides)
    return data


def _briefing(**overrides) -> Briefing:
    data = dict(
        summary="Focus on the quarterly proposal before lunch.",
        generated_at="2026-09-17T08:05:00+07:00",
        source="hermes",
        suggested_task_ids=["bbb"],
    )
    data.update(overrides)
    return Briefing(**data)


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"status": 1, "request": "req-1"}
        self.text = text or json.dumps(self._payload)

    def json(self):
        return self._payload


def _patch_client(response: FakeResponse):
    client = AsyncMock()
    client.post = AsyncMock(return_value=response)
    cm = AsyncMock()
    cm.__aenter__.return_value = client
    cm.__aexit__.return_value = False
    return client, patch("app.services.pushover.httpx.AsyncClient", return_value=cm)


class FingerprintTests(unittest.TestCase):
    def test_same_after_markdown(self):
        self.assertTrue(
            briefing_push.substantively_same(
                "Focus on the quarterly proposal before lunch.",
                "**Focus on the quarterly proposal before lunch.**",
            )
        )

    def test_different_task_is_substantive(self):
        self.assertFalse(
            briefing_push.substantively_same(
                "Focus on the quarterly proposal before lunch.",
                "Then file expenses.",
            )
        )
    def test_skips_early_morning(self):
        self.assertIsNone(briefing_push.slot_for(_now(hour=6)))

    def test_morning_after_seven(self):
        self.assertEqual(briefing_push.slot_for(_now(hour=7)), "2026-09-17:morning")

    def test_afternoon(self):
        self.assertEqual(
            briefing_push.slot_for(_now(time_of_day="afternoon", hour=13)),
            "2026-09-17:afternoon",
        )

    def test_evening(self):
        self.assertEqual(
            briefing_push.slot_for(_now(time_of_day="evening", hour=18)),
            "2026-09-17:evening",
        )

    def test_skips_night(self):
        self.assertIsNone(briefing_push.slot_for(_now(time_of_day="night", hour=22)))


class ClipTests(unittest.TestCase):
    def test_passes_short_text(self):
        self.assertEqual(pushover.clip("hello", 1024), "hello")

    def test_truncates_to_limit(self):
        text = "x" * 1030
        clipped = pushover.clip(text, 1024)
        self.assertEqual(len(clipped), 1024)
        self.assertTrue(clipped.endswith("…"))


class SendMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pushover.reset_state()

    async def test_skips_when_keys_missing(self):
        with patch("app.services.pushover.get_settings", return_value=_settings(
            pushover_user_key="", pushover_api_key=""
        )):
            self.assertFalse(await pushover.send_message("Title", "Body"))

    async def test_posts_required_fields(self):
        client, patched = _patch_client(FakeResponse())
        with patch("app.services.pushover.get_settings", return_value=_settings()), patched:
            sent = await pushover.send_message("Hermes Briefing", "Hello")
        self.assertTrue(sent)
        kwargs = client.post.await_args.kwargs
        self.assertEqual(client.post.await_args.args[0], pushover.MESSAGES_URL)
        self.assertEqual(kwargs["data"]["token"], "t" * 30)
        self.assertEqual(kwargs["data"]["user"], "u" * 30)
        self.assertEqual(kwargs["data"]["message"], "Hello")
        self.assertEqual(kwargs["data"]["title"], "Hermes Briefing")

    async def test_treats_non_one_status_as_failure(self):
        _, patched = _patch_client(
            FakeResponse(
                status_code=400,
                payload={"status": 0, "errors": ["user identifier is invalid"], "user": "invalid"},
            )
        )
        with patch("app.services.pushover.get_settings", return_value=_settings()), patched:
            self.assertFalse(await pushover.send_message("T", "M"))

    async def test_halts_after_4xx(self):
        client, patched = _patch_client(FakeResponse(status_code=400, payload={"status": 0}))
        with patch("app.services.pushover.get_settings", return_value=_settings()), patched:
            self.assertFalse(await pushover.send_message("T", "M"))
            self.assertFalse(await pushover.send_message("T", "again"))
        self.assertEqual(client.post.await_count, 1)


class PublishBriefingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        pushover.reset_state()
        briefing_push.reset_state()
        self._tmp = tempfile.TemporaryDirectory()
        briefing_push._fingerprint_path_override = Path(self._tmp.name) / "fp.txt"
        briefing_push._context_fingerprint_path_override = (
            Path(self._tmp.name) / "ctx.txt"
        )

    def tearDown(self):
        briefing_push._fingerprint_path_override = None
        briefing_push._context_fingerprint_path_override = None
        briefing_push.reset_state()
        self._tmp.cleanup()

    async def test_sends_on_every_update(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            first = await briefing_push.publish_briefing(_briefing())
            second = await briefing_push.publish_briefing(
                _briefing(summary="Then file expenses.")
            )
        self.assertTrue(first)
        self.assertTrue(second)
        self.assertEqual(send.await_count, 2)
        title, message = send.await_args.args
        self.assertIn("Thursday", title)
        self.assertIn("morning", title)
        self.assertIn("expenses", message)

    async def test_skips_push_when_unchanged(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            first = await briefing_push.publish_briefing(_briefing())
            second = await briefing_push.publish_briefing(
                _briefing(summary="**Focus on the quarterly proposal before lunch.**")
            )
        self.assertTrue(first)
        self.assertTrue(second)
        send.assert_awaited_once()

    async def test_skips_push_when_context_fingerprint_matches(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            first = await briefing_push.publish_briefing(
                _briefing(context_fingerprint="abc123", source="apple")
            )
            second = await briefing_push.publish_briefing(
                _briefing(
                    summary="Completely different Apple wording about the same day.",
                    context_fingerprint="abc123",
                    source="apple",
                )
            )
        self.assertTrue(first)
        self.assertTrue(second)
        send.assert_awaited_once()

    async def test_pushes_when_context_fingerprint_changes(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            await briefing_push.publish_briefing(
                _briefing(
                    summary="Focus on the quarterly proposal before lunch.",
                    context_fingerprint="abc123",
                    source="apple",
                )
            )
            await briefing_push.publish_briefing(
                _briefing(
                    summary="Focus on the quarterly proposal before lunch.",
                    context_fingerprint="def456",
                    source="apple",
                )
            )
        self.assertEqual(send.await_count, 2)

    async def test_skips_push_when_reordered(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            await briefing_push.publish_briefing(_briefing())
            await briefing_push.publish_briefing(
                _briefing(summary="Before lunch, focus on the quarterly proposal.")
            )
        send.assert_awaited_once()

    async def test_still_writes_note_when_push_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Hermes Briefing.md"
            with (
                patch(
                    "app.services.briefing_push.get_settings",
                    return_value=_settings(briefing_note_path=str(note)),
                ),
                patch("app.services.briefing_push.local_now_context", return_value=_now()),
                patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
            ):
                await briefing_push.publish_briefing(_briefing())
                await briefing_push.publish_briefing(
                    _briefing(summary="Focus on the quarterly proposal before lunch.")
                )
            send.assert_awaited_once()
            self.assertEqual(
                note.read_text(encoding="utf-8"),
                "Focus on the quarterly proposal before lunch.\n",
            )

    async def test_sends_at_night(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch(
                "app.services.briefing_push.local_now_context",
                return_value=_now(time_of_day="night", hour=22),
            ),
            patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
        ):
            sent = await briefing_push.publish_briefing(_briefing())
        self.assertTrue(sent)
        send.assert_awaited_once()

    async def test_skips_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Hermes Briefing.md"
            note.write_text("keep me\n", encoding="utf-8")
            with (
                patch(
                    "app.services.briefing_push.get_settings",
                    return_value=_settings(briefing_note_path=str(note)),
                ),
                patch("app.services.briefing_push.local_now_context", return_value=_now()),
                patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
            ):
                sent = await briefing_push.publish_briefing(_briefing(source="fallback"))
            self.assertFalse(sent)
            send.assert_not_awaited()
            self.assertEqual(note.read_text(encoding="utf-8"), "keep me\n")

    async def test_skips_interrupt_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Hermes Briefing.md"
            note.write_text("keep me\n", encoding="utf-8")
            with (
                patch(
                    "app.services.briefing_push.get_settings",
                    return_value=_settings(briefing_note_path=str(note)),
                ),
                patch("app.services.briefing_push.local_now_context", return_value=_now()),
                patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)) as send,
            ):
                sent = await briefing_push.publish_briefing(
                    _briefing(
                        summary="Operation interrupted: waiting for model response (39.9s elapsed)."
                    )
                )
            self.assertFalse(sent)
            send.assert_not_awaited()
            self.assertEqual(note.read_text(encoding="utf-8"), "keep me\n")

    async def test_writes_note(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Hermes Briefing.md"
            with (
                patch(
                    "app.services.briefing_push.get_settings",
                    return_value=_settings(briefing_note_path=str(note)),
                ),
                patch("app.services.briefing_push.local_now_context", return_value=_now()),
                patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)),
            ):
                await briefing_push.publish_briefing(_briefing())
            self.assertEqual(
                note.read_text(encoding="utf-8"),
                "Focus on the quarterly proposal before lunch.\n",
            )

    async def test_overwrites_note_on_each_update(self):
        with tempfile.TemporaryDirectory() as tmp:
            note = Path(tmp) / "Hermes Briefing.md"
            note.write_text("old\n", encoding="utf-8")
            with (
                patch(
                    "app.services.briefing_push.get_settings",
                    return_value=_settings(briefing_note_path=str(note)),
                ),
                patch("app.services.briefing_push.local_now_context", return_value=_now()),
                patch("app.services.pushover.send_message", new=AsyncMock(return_value=True)),
            ):
                await briefing_push.publish_briefing(_briefing(summary="New briefing."))
            self.assertEqual(note.read_text(encoding="utf-8"), "New briefing.\n")

    async def test_push_due_generates_during_day(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch("app.services.briefing_push.local_now_context", return_value=_now()),
            patch("app.services.hermes.peek_briefing", return_value=None),
            patch(
                "app.services.briefing_push.briefing_context",
                new=AsyncMock(return_value={"calendar": [], "tasks": []}),
            ),
            patch(
                "app.services.hermes.generate_briefing",
                new=AsyncMock(return_value=_briefing()),
            ) as generate,
        ):
            await briefing_push.push_due_briefing()
        generate.assert_awaited_once()
        self.assertFalse(generate.await_args.kwargs.get("force", False))

    async def test_push_due_skips_night_when_nextday_cached(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch(
                "app.services.briefing_push.local_now_context",
                return_value=_now(time_of_day="night", hour=22),
            ),
            patch(
                "app.services.briefing_push.briefing_context",
                new=AsyncMock(return_value={"calendar": [], "tasks": []}),
            ),
            patch("app.services.hermes.peek_briefing", return_value=_briefing(horizon="tomorrow")),
            patch(
                "app.services.hermes.generate_briefing",
                new=AsyncMock(return_value=_briefing()),
            ) as generate,
        ):
            await briefing_push.push_due_briefing()
        generate.assert_not_awaited()

    async def test_push_due_generates_nextday_at_night_when_missing(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch(
                "app.services.briefing_push.local_now_context",
                return_value=_now(time_of_day="night", hour=22),
            ),
            patch("app.services.hermes.peek_briefing", return_value=None),
            patch(
                "app.services.briefing_push.briefing_context",
                new=AsyncMock(return_value={"calendar": [], "tasks": []}),
            ),
            patch(
                "app.services.hermes.generate_briefing",
                new=AsyncMock(return_value=_briefing(horizon="tomorrow")),
            ) as generate,
        ):
            await briefing_push.push_due_briefing()
        generate.assert_awaited_once()

    async def test_push_due_keeps_today_when_on_deck_has_tasks_after_17(self):
        with (
            patch("app.services.briefing_push.get_settings", return_value=_settings()),
            patch(
                "app.services.briefing_push.local_now_context",
                return_value=_now(time_of_day="evening", hour=17, minute=15),
            ),
            patch(
                "app.services.briefing_push.briefing_context",
                new=AsyncMock(
                    return_value={
                        "calendar": [],
                        "tasks": [{"id": "aaa", "name": "File expenses"}],
                    }
                ),
            ),
            patch(
                "app.services.hermes.peek_briefing",
                return_value=_briefing(horizon="tomorrow"),
            ),
            patch(
                "app.services.hermes.generate_briefing",
                new=AsyncMock(return_value=_briefing(horizon="today")),
            ) as generate,
        ):
            await briefing_push.push_due_briefing()
        generate.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
