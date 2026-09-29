import unittest
from unittest.mock import AsyncMock, patch

from app.models.schemas import AgentReply
from app.services import email_triage
from app.services.hermes import HermesUnavailable, _APPLE_PROMPT_MAX


class FallbackReasonTests(unittest.TestCase):
    def test_offline_connect_error(self):
        with patch.object(email_triage, "hermes_busy", return_value=False):
            reason = email_triage._fallback_reason(
                HermesUnavailable(
                    "Cannot reach Hermes API at http://127.0.0.1:8642/v1 "
                    "after 0.0s (ConnectError: All connection attempts failed)"
                )
            )
        self.assertIn("offline", reason)

    def test_busy_lock(self):
        with patch.object(email_triage, "hermes_busy", return_value=True):
            reason = email_triage._fallback_reason()
        self.assertIn("busy", reason)

    def test_generic_unavailable(self):
        with patch.object(email_triage, "hermes_busy", return_value=False):
            reason = email_triage._fallback_reason(HermesUnavailable("429"))
        self.assertIn("unavailable", reason)


class AppleEmailTriageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        email_triage._triage_cache = None

    def tearDown(self):
        email_triage._triage_cache = None

    def test_one_message_prompt_stays_under_the_limit(self):
        prompt = email_triage.apple_email_prompt(
            {
                "id": "1",
                "account": "gmail",
                "from": "Ada <ada@example.com>",
                "subject": "Contract",
                "date": "2026-09-24",
                "snippet": "please review " * 400,
            }
        )
        self.assertLessEqual(len(prompt), _APPLE_PROMPT_MAX)
        self.assertIn("Contract", prompt)
        self.assertIn("gmail", prompt)

    async def test_triage_uses_one_call_per_message(self):
        seen: list[str] = []

        async def fake_apple(message: str) -> AgentReply:
            seen.append(message)
            return AgentReply(
                reply='{"priority":"high","disposition":"reply","reason":"needs a yes","needs_reply":true,"draft_reply":"Yes."}',
                model="apple-intelligence",
            )

        envelopes = [
            {
                "id": "1",
                "account": "gmail",
                "from": "Ada <ada@example.com>",
                "subject": "Contract",
                "date": "2026-09-24T01:00:00Z",
                "snippet": "Can you sign this?",
            }
        ]
        with (
            patch.object(email_triage, "list_accounts", AsyncMock(return_value=["gmail"])),
            patch.object(email_triage, "list_actionable", AsyncMock(return_value=envelopes)),
            patch.object(email_triage, "read_snippet", AsyncMock(return_value="Can you sign this?")),
            patch.object(email_triage, "ask_apple", fake_apple),
            patch.object(email_triage, "ask_hermes", AsyncMock()) as hermes,
        ):
            result = await email_triage.triage_inbox(force=True)
        hermes.assert_not_awaited()
        self.assertEqual(len(seen), 1)
        self.assertLessEqual(len(seen[0]), _APPLE_PROMPT_MAX)
        self.assertEqual(result.source, "apple")
        self.assertEqual(result.items[0].draft_reply, "Yes.")
        self.assertEqual(result.items[0].subject, "Contract")

    async def test_apple_failure_falls_back_to_hermes(self):
        async def fake_apple(_message: str) -> AgentReply:
            raise HermesUnavailable("Apple Intelligence failed: too long")

        async def fake_hermes(_message: str, **_kwargs: object) -> AgentReply:
            return AgentReply(
                reply='{"items":[{"id":"1","account":"gmail","priority":"low","disposition":"reference","reason":"fyi","needs_reply":false,"draft_reply":null}]}',
                model="hermes-agent",
            )

        envelopes = [
            {
                "id": "1",
                "account": "gmail",
                "from": "Ada <ada@example.com>",
                "subject": "Notes",
                "date": "2026-09-24T01:00:00Z",
            }
        ]
        with (
            patch.object(email_triage, "list_accounts", AsyncMock(return_value=["gmail"])),
            patch.object(email_triage, "list_actionable", AsyncMock(return_value=envelopes)),
            patch.object(email_triage, "read_snippet", AsyncMock(return_value="")),
            patch.object(email_triage, "ask_apple", fake_apple),
            patch.object(email_triage, "ask_hermes", fake_hermes),
            patch.object(email_triage, "hermes_busy", return_value=False),
        ):
            result = await email_triage.triage_inbox(force=True)
        self.assertEqual(result.source, "hermes")
        self.assertEqual(result.items[0].disposition, "reference")
