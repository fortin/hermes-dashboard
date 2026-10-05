import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.models.schemas import AgentReply, TaskItem
from app.services import omnifocus, omnifocus_agent
from app.services.omnifocus_agent import AgentTags
TZ = ZoneInfo("UTC")
TAGS = AgentTags()
NOW = datetime(2026, 9, 18, 16, 0, tzinfo=TZ)


def _task(**overrides) -> TaskItem:
    data = dict(
        id="t1",
        name="add to places - neighborhood cafe",
        note="https://example.com",
        tags=["🤖 Gladys"],
        planned=None,
    )
    data.update(overrides)
    return TaskItem(**data)


class PlannedEligibilityTests(unittest.TestCase):
    def test_missing_planned_is_eligible(self):
        self.assertTrue(omnifocus_agent.planned_is_due(None, NOW))

    def test_past_planned_is_eligible(self):
        self.assertTrue(
            omnifocus_agent.planned_is_due("2026-09-18T15:59:00+00:00", NOW)
        )

    def test_planned_exactly_now_is_eligible(self):
        self.assertTrue(
            omnifocus_agent.planned_is_due("2026-09-18T16:00:00+00:00", NOW)
        )

    def test_future_planned_is_not_eligible(self):
        self.assertFalse(
            omnifocus_agent.planned_is_due("2026-09-18T16:01:00+00:00", NOW)
        )

    def test_date_only_today_is_eligible(self):
        self.assertTrue(
            omnifocus_agent.planned_is_due("2026-09-18T00:00:00+00:00", NOW)
        )

    def test_date_only_tomorrow_is_not_eligible(self):
        self.assertFalse(
            omnifocus_agent.planned_is_due("2026-09-19T00:00:00+00:00", NOW)
        )


class QueueSelectionTests(unittest.TestCase):
    def test_skips_state_tags_and_future_planned(self):
        tasks = [
            _task(id="running", tags=["gladys-running"]),
            _task(id="blocked", tags=["gladys-blocked"]),
            _task(id="failed", tags=["gladys-failed"]),
            _task(id="done", tags=["gladys-done"]),
            _task(id="later", tags=["🤖 Gladys"], planned="2026-09-18T18:00:00+00:00"),
            _task(id="ready", tags=["🤖 Gladys", "errand"]),
            _task(id="also", tags=["🤖 Gladys"]),
        ]
        picked = omnifocus_agent.pick_eligible(tasks, TAGS, NOW)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, "ready")

    def test_requeues_blocked_when_assignment_tag_is_back(self):
        tasks = [
            _task(id="blocked", tags=["🤖 Gladys", "gladys-blocked"]),
        ]
        picked = omnifocus_agent.pick_eligible(tasks, TAGS, NOW)
        self.assertIsNotNone(picked)
        self.assertEqual(picked.id, "blocked")

    def test_empty_when_nothing_ready(self):
        tasks = [_task(id="later", planned="2026-09-19T09:00:00+00:00")]
        self.assertIsNone(omnifocus_agent.pick_eligible(tasks, TAGS, NOW))


class TagAndNoteTests(unittest.TestCase):
    def test_classify_state(self):
        self.assertEqual(
            omnifocus_agent.classify_state(["errand", "gladys-running"], TAGS),
            "running",
        )
        self.assertEqual(omnifocus_agent.classify_state(["🤖 Gladys"], TAGS), "assign")
        self.assertIsNone(omnifocus_agent.classify_state(["errand"], TAGS))

    def test_next_tags_keeps_unrelated(self):
        self.assertEqual(
            omnifocus_agent.next_tags(
                ["🤖 Gladys", "errand", "gladys-failed"],
                TAGS,
                "running",
            ),
            ["errand", "gladys-running"],
        )

    def test_instruction_strips_previous_log(self):
        note = (
            "https://nubiani.example\n"
            f"{omnifocus_agent.LOG_DELIMITER}\n"
            "old receipt\n"
        )
        original, previous = omnifocus_agent.split_note(note)
        self.assertEqual(original, "https://nubiani.example")
        self.assertIn("old receipt", previous)

    def test_parse_status(self):
        self.assertEqual(
            omnifocus_agent.parse_status("Added it.\nSTATUS: blocked\nNeed hours"),
            "blocked",
        )
        self.assertEqual(omnifocus_agent.parse_status("All done."), "done")
        self.assertEqual(omnifocus_agent.parse_status(""), "failed")

    def test_prompt_tells_gladys_to_schedule_with_cronjob(self):
        text = omnifocus_agent.build_prompt("weekly CRM check", "", "")
        self.assertIn("cronjob", text)
        self.assertIn("deliver='telegram'", text)

    def test_receipt_keeps_original_above_the_line(self):
        text = omnifocus_agent.format_receipt(
            "Added Harbor Cafe to Places.",
            claimed_at="2026-09-18T16:02+00:00",
        )
        self.assertIn(omnifocus_agent.LOG_DELIMITER, text)
        self.assertIn("Added Harbor Cafe to Places.", text)
        self.assertIn("2026-09-18T16:02+00:00", text)


class ReviewFilingTests(unittest.TestCase):
    def test_review_action_name(self):
        self.assertEqual(
            omnifocus_agent.review_action_name("Book table at Kai"),
            "Review: Book table at Kai",
        )

    def test_receipt_path_and_uri(self):
        filename = omnifocus_agent.receipt_filename("Book table at Kai", NOW)
        self.assertEqual(filename, "2026-09-18-1600 Book table at Kai.md")
        path = omnifocus_agent.receipt_path(
            "📁 500 📒 Notes/Gladys",
            filename,
        )
        self.assertIn("Gladys/", path)
        uri = omnifocus_agent.advanced_obsidian_uri(
            "My Vault", uid="9105001a-dba2-440c-a64f-c0c55ebedafa"
        )
        self.assertEqual(
            uri,
            "obsidian://adv-uri?vault=My%20Vault&uid=9105001a-dba2-440c-a64f-c0c55ebedafa",
        )
        legacy = omnifocus_agent.advanced_obsidian_uri("My Vault", path)
        self.assertTrue(legacy.startswith("obsidian://adv-uri?"))
        self.assertIn("filepath=", legacy)
        self.assertNotIn(" ", legacy)


class RecordSuccessTests(unittest.IsolatedAsyncioTestCase):
    async def test_writes_obsidian_note_and_review_action(self):
        settings = SimpleNamespace(
            obsidian_vault_name="My Vault",
            obsidian_agent_receipt_folder="📁 Reviews/Gladys",
            omnifocus_review_tag="🔎 Review",
        )
        task = _task(name="Book table at Riverside Bistro")
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.obsidian,
                "write_vault_note",
                new=AsyncMock(),
            ) as write,
            patch.object(
                omnifocus_agent.omnifocus,
                "add_task",
                new=AsyncMock(),
            ) as add,
        ):
            await omnifocus_agent.record_success(
                task,
                "Booked for Friday.\nSTATUS: done",
                claimed_at="2026-09-18T16:00+00:00",
                clock=NOW,
                original="https://kai.example",
            )
        path = write.await_args.args[0]
        body = write.await_args.args[1]
        self.assertTrue(path.endswith("2026-09-18-1600 Book table at Riverside Bistro.md"))
        self.assertIn("📁 Reviews/Gladys/", path)
        self.assertTrue(body.startswith("---\nuid: "))
        self.assertIn("Booked for Friday.", body)
        self.assertIn("https://kai.example", body)
        add.assert_awaited_once()
        args, kwargs = add.await_args
        self.assertEqual(args[0], "Review: Book table at Riverside Bistro")
        note = str(kwargs.get("note"))
        self.assertTrue(note.startswith("obsidian://adv-uri?"))
        self.assertIn("uid=", note)
        self.assertNotIn("filepath=", note)
        self.assertEqual(kwargs.get("tags"), ["🔎 Review"])
        self.assertEqual(kwargs.get("defer_date"), "2026-09-18T16:00:00+00:00")
        self.assertIsNone(kwargs.get("project_id"))
        self.assertIsNone(kwargs.get("project_name"))

    async def test_review_action_drops_inherited_waiting_tag(self):
        settings = SimpleNamespace(
            obsidian_vault_name="My Vault",
            obsidian_agent_receipt_folder="📁 Reviews/Gladys",
            omnifocus_review_tag="🔎 Review",
        )
        created = {
            "ok": True,
            "data": {
                "items": [
                    {
                        "id": "review-1",
                        "name": "Review: Book table at Riverside Bistro",
                        "tags": ["(Waiting)", "🔎 Review"],
                    }
                ]
            },
        }
        task = _task(name="Book table at Riverside Bistro", project_id="p-places")
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.obsidian,
                "write_vault_note",
                new=AsyncMock(),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "add_task",
                new=AsyncMock(return_value=created),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_tags",
                new=AsyncMock(),
            ) as set_tags,
        ):
            await omnifocus_agent.record_success(
                task,
                "Booked for Friday.\nSTATUS: done",
                claimed_at="2026-09-18T16:00+00:00",
                clock=NOW,
            )
        set_tags.assert_awaited_once_with("review-1", ["🔎 Review"])

    async def test_review_action_skips_tag_replace_without_id(self):
        settings = SimpleNamespace(
            obsidian_vault_name="My Vault",
            obsidian_agent_receipt_folder="📁 Reviews/Gladys",
            omnifocus_review_tag="🔎 Review",
        )
        task = _task(name="Book table")
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.obsidian,
                "write_vault_note",
                new=AsyncMock(),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "add_task",
                new=AsyncMock(return_value={"ok": True}),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_tags",
                new=AsyncMock(),
            ) as set_tags,
        ):
            await omnifocus_agent.record_success(
                task,
                "Booked.\nSTATUS: done",
                claimed_at="2026-09-18T16:00+00:00",
                clock=NOW,
            )
        set_tags.assert_not_awaited()

    async def test_review_action_stays_in_original_project(self):
        settings = SimpleNamespace(
            obsidian_vault_name="My Vault",
            obsidian_agent_receipt_folder="📁 Reviews/Gladys",
            omnifocus_review_tag="🔎 Review",
        )
        task = _task(
            name="Add Harbor Cafe to Places",
            project="Places",
            project_id="p-places",
        )
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.obsidian,
                "write_vault_note",
                new=AsyncMock(),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "add_task",
                new=AsyncMock(),
            ) as add,
        ):
            await omnifocus_agent.record_success(
                task,
                "Added.\nSTATUS: done",
                claimed_at="2026-09-18T16:00+00:00",
                clock=NOW,
            )
        kwargs = add.await_args.kwargs
        self.assertEqual(kwargs.get("project_id"), "p-places")
        self.assertIsNone(kwargs.get("project_name"))

    async def test_review_action_uses_project_name_without_id(self):
        settings = SimpleNamespace(
            obsidian_vault_name="My Vault",
            obsidian_agent_receipt_folder="📁 Reviews/Gladys",
            omnifocus_review_tag="🔎 Review",
        )
        task = _task(name="Add Harbor Cafe to Places", project="Places")
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.obsidian,
                "write_vault_note",
                new=AsyncMock(),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "add_task",
                new=AsyncMock(),
            ) as add,
        ):
            await omnifocus_agent.record_success(
                task,
                "Added.\nSTATUS: done",
                claimed_at="2026-09-18T16:00+00:00",
                clock=NOW,
            )
        kwargs = add.await_args.kwargs
        self.assertIsNone(kwargs.get("project_id"))
        self.assertEqual(kwargs.get("project_name"), "Places")


class OutcomeMessageTests(unittest.TestCase):
    def test_done_includes_task_and_receipt(self):
        title, body = omnifocus_agent.outcome_message(
            "add to places - neighborhood cafe",
            "done",
            "Added Harbor Cafe to Places.\nSTATUS: done",
        )
        self.assertEqual(title, "Gladys · done")
        self.assertIn("add to places - neighborhood cafe", body)
        self.assertIn("Added Harbor Cafe to Places.", body)
        self.assertNotIn("STATUS:", body)

    def test_blocked_keeps_the_reason(self):
        title, body = omnifocus_agent.outcome_message(
            "Book table",
            "blocked",
            "Need the vault path.\nSTATUS: blocked",
        )
        self.assertEqual(title, "Gladys · blocked")
        self.assertIn("Need the vault path.", body)

    def test_failed_title(self):
        title, body = omnifocus_agent.outcome_message(
            "File expenses",
            "failed",
            "Cannot reach Hermes",
        )
        self.assertEqual(title, "Gladys · failed")
        self.assertIn("File expenses", body)
        self.assertIn("Cannot reach Hermes", body)


class NotifyOutcomeTests(unittest.IsolatedAsyncioTestCase):
    async def test_sends_pushover(self):
        with patch(
            "app.services.pushover.send_message",
            new=AsyncMock(return_value=True),
        ) as send:
            await omnifocus_agent.notify_outcome(
                "Book table",
                "done",
                "Booked Friday.\nSTATUS: done",
            )
        send.assert_awaited_once()
        title, message = send.await_args.args[:2]
        self.assertEqual(title, "Gladys · done")
        self.assertIn("Book table", message)
        self.assertIn("Booked Friday.", message)
        self.assertNotIn("STATUS:", message)

    async def test_skips_non_terminal_status(self):
        with patch(
            "app.services.pushover.send_message",
            new=AsyncMock(return_value=True),
        ) as send:
            await omnifocus_agent.notify_outcome("x", "running", "working")
        send.assert_not_awaited()


class TickTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings = SimpleNamespace(
            omnifocus_agent_enabled=True,
            omnifocus_agent_tag="🤖 Gladys",
            omnifocus_agent_running_tag="gladys-running",
            omnifocus_agent_done_tag="gladys-done",
            omnifocus_agent_blocked_tag="gladys-blocked",
            omnifocus_agent_failed_tag="gladys-failed",
        )
        self._notify = patch.object(
            omnifocus_agent, "notify_outcome", new=AsyncMock()
        )
        self.notify = self._notify.start()

    def tearDown(self):
        self._notify.stop()

    async def test_claims_executes_and_completes_one_task(self):
        ready = _task()
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=self.settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[ready, _task(id="t2")]),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_tags",
                new=AsyncMock(),
            ) as set_tags,
            patch.object(
                omnifocus_agent.omnifocus,
                "append_task_note",
                new=AsyncMock(),
            ) as append,
            patch.object(
                omnifocus_agent.omnifocus,
                "complete_task",
                new=AsyncMock(),
            ) as complete,
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_flagged",
                new=AsyncMock(),
            ) as flag,
            patch.object(
                omnifocus_agent.hermes,
                "execute_delegated_task",
                new=AsyncMock(
                    return_value=AgentReply(
                        reply="Added Harbor Cafe.\nSTATUS: done",
                        model="hermes-agent",
                    )
                ),
            ) as execute,
            patch.object(
                omnifocus_agent,
                "record_success",
                new=AsyncMock(),
            ) as filed,
        ):
            result = await omnifocus_agent.tick(now=NOW)

        self.assertEqual(result["id"], "t1")
        self.assertEqual(result["status"], "done")
        execute.assert_awaited_once()
        prompt = execute.await_args.args[0]
        self.assertIn("add to places - neighborhood cafe", prompt)
        self.assertIn("https://example.com", prompt)
        set_tags.assert_any_await("t1", ["gladys-running"])
        set_tags.assert_any_await("t1", ["gladys-done"])
        append.assert_awaited_once()
        complete.assert_awaited_once_with("t1")
        flag.assert_not_awaited()
        filed.assert_awaited_once()
        self.assertEqual(filed.await_args.kwargs["original"], "https://example.com")
        self.notify.assert_awaited_once()
        self.assertEqual(
            self.notify.await_args.args[:2],
            ("add to places - neighborhood cafe", "done"),
        )

    async def test_still_completes_when_success_filing_fails(self):
        ready = _task()
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=self.settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[ready]),
            ),
            patch.object(omnifocus_agent.omnifocus, "set_task_tags", new=AsyncMock()),
            patch.object(omnifocus_agent.omnifocus, "append_task_note", new=AsyncMock()),
            patch.object(
                omnifocus_agent.omnifocus,
                "complete_task",
                new=AsyncMock(),
            ) as complete,
            patch.object(
                omnifocus_agent.hermes,
                "execute_delegated_task",
                new=AsyncMock(
                    return_value=AgentReply(reply="Done.\nSTATUS: done", model="hermes-agent")
                ),
            ),
            patch.object(
                omnifocus_agent,
                "record_success",
                new=AsyncMock(side_effect=RuntimeError("vault down")),
            ),
        ):
            result = await omnifocus_agent.tick(now=NOW)
        self.assertEqual(result["status"], "done")
        complete.assert_awaited_once_with("t1")
        self.notify.assert_awaited_once()

    async def test_blocked_flags_and_does_not_complete(self):
        ready = _task(tags=["🤖 Gladys", "errand"])
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=self.settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[ready]),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_tags",
                new=AsyncMock(),
            ) as set_tags,
            patch.object(
                omnifocus_agent.omnifocus,
                "append_task_note",
                new=AsyncMock(),
            ),
            patch.object(
                omnifocus_agent.omnifocus,
                "complete_task",
                new=AsyncMock(),
            ) as complete,
            patch.object(
                omnifocus_agent.omnifocus,
                "set_task_flagged",
                new=AsyncMock(),
            ) as flag,
            patch.object(
                omnifocus_agent.hermes,
                "execute_delegated_task",
                new=AsyncMock(
                    return_value=AgentReply(
                        reply="Need the vault path.\nSTATUS: blocked",
                        model="hermes-agent",
                    )
                ),
            ),
            patch.object(
                omnifocus_agent,
                "record_success",
                new=AsyncMock(),
            ) as filed,
        ):
            result = await omnifocus_agent.tick(now=NOW)

        self.assertEqual(result["status"], "blocked")
        set_tags.assert_any_await("t1", ["errand", "gladys-blocked"])
        flag.assert_awaited_once_with("t1", True)
        complete.assert_not_awaited()
        filed.assert_not_awaited()
        self.notify.assert_awaited_once()
        self.assertEqual(self.notify.await_args.args[1], "blocked")

    async def test_skips_when_nothing_eligible(self):
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=self.settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[]),
            ),
            patch.object(
                omnifocus_agent.hermes,
                "execute_delegated_task",
                new=AsyncMock(),
            ) as execute,
        ):
            result = await omnifocus_agent.tick(now=NOW)
        self.assertIsNone(result)
        execute.assert_not_awaited()
        self.notify.assert_not_awaited()

    async def test_failed_hermes_notifies(self):
        ready = _task()
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=self.settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[ready]),
            ),
            patch.object(omnifocus_agent.omnifocus, "set_task_tags", new=AsyncMock()),
            patch.object(omnifocus_agent.omnifocus, "append_task_note", new=AsyncMock()),
            patch.object(omnifocus_agent.omnifocus, "complete_task", new=AsyncMock()),
            patch.object(omnifocus_agent.omnifocus, "set_task_flagged", new=AsyncMock()),
            patch.object(
                omnifocus_agent.hermes,
                "execute_delegated_task",
                new=AsyncMock(
                    side_effect=omnifocus_agent.HermesUnavailable("Cannot reach Hermes")
                ),
            ),
        ):
            result = await omnifocus_agent.tick(now=NOW)
        self.assertEqual(result["status"], "failed")
        self.notify.assert_awaited_once()
        self.assertEqual(self.notify.await_args.args[1], "failed")
        self.assertIn("Cannot reach Hermes", self.notify.await_args.args[2])

    async def test_peek_queue_names_the_next_task(self):
        ready = _task()
        later = _task(id="later", planned="2026-09-18T18:00:00+00:00")
        settings = SimpleNamespace(
            omnifocus_agent_enabled=True,
            omnifocus_agent_tag="🤖 Gladys",
            omnifocus_agent_running_tag="gladys-running",
            omnifocus_agent_done_tag="gladys-done",
            omnifocus_agent_blocked_tag="gladys-blocked",
            omnifocus_agent_failed_tag="gladys-failed",
            omnifocus_agent_poll_seconds=900,
        )
        with (
            patch.object(omnifocus_agent, "get_settings", return_value=settings),
            patch.object(
                omnifocus_agent.omnifocus,
                "get_tagged_tasks",
                new=AsyncMock(return_value=[later, ready]),
            ),
        ):
            snapshot = await omnifocus_agent.peek_queue(now=NOW)
        self.assertEqual(snapshot["tag"], "🤖 Gladys")
        self.assertEqual(snapshot["next"]["id"], "t1")
        self.assertEqual([row["id"] for row in snapshot["eligible"]], ["t1"])
        self.assertIsNone(snapshot["error"])


class NormalizeTagTests(unittest.TestCase):
    def test_detailed_tag_objects(self):
        task = omnifocus._normalize_task(
            {
                "id": "x",
                "name": "A",
                "tags": [
                    {"id": "1", "name": "🤖 Gladys", "path": "Assign to / Gladys"}
                ],
                "note": "hello",
            }
        )
        self.assertEqual(task.tags, ["🤖 Gladys"])
        self.assertEqual(task.note, "hello")
