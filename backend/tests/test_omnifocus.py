import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services import omnifocus


class GetTomorrowActionsTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_named_perspective(self):
        raw = {
            "data": {
                "items": [
                    {
                        "id": "a",
                        "name": "Write proposal",
                        "dueDate": "2026-09-18",
                        "estimatedMinutes": 45,
                    },
                    {"id": "b", "name": "Review deck", "plannedDate": "2026-09-18"},
                ]
            }
        }
        settings = SimpleNamespace(omnifocus_tomorrow_perspective="Tomorrow > 5 minutes")
        with (
            patch.object(omnifocus, "get_settings", return_value=settings),
            patch.object(omnifocus, "_call", new=AsyncMock(return_value=raw)) as call,
        ):
            tasks = await omnifocus.get_tomorrow_actions()
        call.assert_awaited_once_with(
            "query_tasks",
            {
                "source": "custom",
                "perspective_name": "Tomorrow > 5 minutes",
                "hide_completed": True,
                "limit": 80,
                "output": "compact",
                "sort_by": "library",
            },
        )
        self.assertEqual([t.name for t in tasks], ["Write proposal", "Review deck"])
        self.assertEqual(tasks[0].estimated_minutes, 45)
