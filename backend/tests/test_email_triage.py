import unittest
from unittest.mock import patch

from app.services import email_triage
from app.services.hermes import HermesUnavailable


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
