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
        settings = SimpleNamespace(
            omnifocus_tomorrow_perspective="Tomorrow > 5 minutes"
        )
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


class GetTaggedTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_queries_tag_in_detailed_mode(self):
        raw = {
            "data": {
                "items": [
                    {
                        "id": "t1",
                        "name": "Add Nubiani",
                        "note": "https://example.com",
                        "tags": [{"id": "g", "name": "🤖 Gladys"}],
                        "projectId": "p1",
                        "projectName": "Places",
                    }
                ]
            }
        }
        with patch.object(omnifocus, "_call", new=AsyncMock(return_value=raw)) as call:
            tasks = await omnifocus.get_tagged_tasks("🤖 Gladys")
        call.assert_awaited_once_with(
            "query_tasks",
            {
                "source": "tag",
                "tag_name": "🤖 Gladys",
                "hide_completed": True,
                "limit": 50,
                "output": "detailed",
                "exact_match": True,
            },
        )
        self.assertEqual(tasks[0].note, "https://example.com")
        self.assertEqual(tasks[0].tags, ["🤖 Gladys"])
        self.assertEqual(tasks[0].project, "Places")
        self.assertEqual(tasks[0].project_id, "p1")


class NormalizeTaskProjectTests(unittest.TestCase):
    def test_captures_project_id_and_name(self):
        task = omnifocus._normalize_task(
            {
                "id": "t1",
                "name": "Add Nubiani",
                "projectId": "p1",
                "projectName": "Places",
            }
        )
        self.assertEqual(task.project, "Places")
        self.assertEqual(task.project_id, "p1")


class CreatedTaskIdTests(unittest.TestCase):
    def test_reads_id_from_wrapped_items(self):
        self.assertEqual(
            omnifocus.created_task_id(
                {
                    "ok": True,
                    "data": {
                        "items": [
                            {
                                "id": "review-1",
                                "name": "Review: Book table",
                                "tags": ["(Waiting)", "🔎 Review"],
                            }
                        ]
                    },
                }
            ),
            "review-1",
        )

    def test_missing_payload_is_none(self):
        self.assertIsNone(omnifocus.created_task_id({"ok": True}))
        self.assertIsNone(omnifocus.created_task_id(None))


class AddTaskTests(unittest.IsolatedAsyncioTestCase):
    async def test_passes_project_id(self):
        with patch.object(
            omnifocus, "_call", new=AsyncMock(return_value={"ok": True})
        ) as call:
            await omnifocus.add_task(
                "Review: Add Nubiani",
                note="obsidian://adv-uri?vault=x",
                tags=["🔎 Review"],
                project_id="p1",
                project_name="Places",
            )
        call.assert_awaited_once_with(
            "add_task",
            {
                "name": "Review: Add Nubiani",
                "note": "obsidian://adv-uri?vault=x",
                "tags": ["🔎 Review"],
                "project_id": "p1",
            },
        )

    async def test_falls_back_to_project_name(self):
        with patch.object(
            omnifocus, "_call", new=AsyncMock(return_value={"ok": True})
        ) as call:
            await omnifocus.add_task("Review: Inbox thing", project_name="Places")
        call.assert_awaited_once_with(
            "add_task",
            {"name": "Review: Inbox thing", "project_name": "Places"},
        )
