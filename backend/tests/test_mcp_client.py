import unittest
from unittest.mock import AsyncMock, patch

from app.mcp.client import McpRegistry, is_cancel_scope_noise


class CancelScopeNoiseTests(unittest.TestCase):
    def test_runtime_error_from_anyio(self):
        exc = RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )
        self.assertTrue(is_cancel_scope_noise(exc))

    def test_exception_group_wrapping_runtime_error(self):
        inner = RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )
        grouped = ExceptionGroup("unhandled errors in a TaskGroup", [inner])
        self.assertTrue(is_cancel_scope_noise(grouped))

    def test_unrelated_errors_are_not_noise(self):
        self.assertFalse(is_cancel_scope_noise(ConnectionRefusedError("refused")))
        self.assertFalse(is_cancel_scope_noise(RuntimeError("tool failed")))


class CloseAllTests(unittest.IsolatedAsyncioTestCase):
    async def test_swallows_cancel_scope_without_logging(self):
        registry = McpRegistry()
        handle = AsyncMock()
        handle.close.side_effect = RuntimeError(
            "Attempted to exit cancel scope in a different task than it was entered in"
        )
        registry._servers["omnifocus"] = handle
        with patch("app.mcp.client.logger.exception") as logged:
            await registry.close_all()
        logged.assert_not_called()
        self.assertEqual(registry._servers, {})

    async def test_still_logs_unexpected_close_errors(self):
        registry = McpRegistry()
        handle = AsyncMock()
        handle.close.side_effect = RuntimeError("broken pipe")
        registry._servers["omnifocus"] = handle
        with patch("app.mcp.client.logger.exception") as logged:
            await registry.close_all()
        logged.assert_called_once()
        self.assertEqual(registry._servers, {})
