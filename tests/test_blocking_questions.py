from __future__ import annotations

from tests.support.tess_core_case import *

from tess_core.agent._question_gate import (
    inject_open_question_reminders,
    read_open_child_questions,
    resolve_open_questions_with_backstop,
)
from tess_core.tools.messaging import _send_message_tool
from tess_core.tasks.communication import create_task_question, read_task_questions


def _context(session, workspace):
    return {
        "session": session,
        "config": {"sessionPersistence": False},
        "cwd": str(workspace),
        "daemon": None,
        "abortSignal": None,
    }


class BlockingQuestionTests(TessCoreTestCase):
    def _family(self, workspace):
        parent = core.create_empty_session(
            cwd=str(workspace), provider="openai", model="gpt-5.4",
            systemPrompt="parent", permissionMode="default", sessionId="bq-parent",
        )
        parent["metadata"]["taskIds"] = ["bq-task"]
        child = core.create_empty_session(
            cwd=str(workspace), provider="openai", model="gpt-5.4",
            systemPrompt="child", permissionMode="default", sessionId="bq-child",
        )
        child["metadata"]["subagentTaskId"] = "bq-task"
        run(core.write_task_meta("bq-task", {
            "type": "subagent", "taskId": "bq-task", "id": "bq-task",
            "sessionId": child["id"], "session_id": child["id"],
            "parent_session_id": parent["id"], "agent_id": "bq-agent",
            "status": "running", "cwd": str(workspace),
            "startedAt": "2026-01-01T00:00:00Z", "updatedAt": "2026-01-01T00:00:00Z",
        }))
        run(core.save_child_agent({
            "agentId": "bq-agent", "sessionId": child["id"],
            "parentSessionId": parent["id"], "name": "Worker",
            "provider": "openai", "model": "gpt-5.4", "mode": "default",
            "runInBackground": True, "status": "running", "taskId": "bq-task",
            "startedAt": "2026-01-01T00:00:00Z",
        }))
        return parent, child

    def test_child_question_answered_by_parent_reply(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._family(workspace)
            tool = _send_message_tool()
            os.environ["TESS_CORE_COMMS_REPLY_POLL_MS"] = "50"
            try:
                async def exercise():
                    async def parent_answers():
                        for _ in range(100):
                            await asyncio.sleep(0.05)
                            open_questions = await read_open_child_questions(parent)
                            if open_questions:
                                break
                        self.assertEqual((await core.read_task_meta("bq-task"))["status"], "awaiting_input")
                        question_id = open_questions[0]["questionId"]
                        return await tool.handler(
                            {"to": "bq-agent", "reply_to": question_id, "message": "yes, ship it"},
                            _context(parent, workspace),
                        )

                    answer_task = asyncio.get_running_loop().create_task(parent_answers())
                    child_result = await tool.handler(
                        {"to": "parent", "message": "May I ship this?", "expects_reply": True, "timeout_ms": 8000},
                        _context(child, workspace),
                    )
                    await answer_task
                    return child_result

                result = run(exercise())
                payload = json.loads(result["content"])
                self.assertEqual(payload["status"], "reply_received")
                self.assertIn("yes, ship it", payload["reply"])
                questions = run(read_task_questions("bq-task"))
                self.assertEqual(questions[0]["status"], "answered")
                self.assertEqual((run(core.read_task_meta("bq-task")))["status"], "running")
                self.assertEqual(run(read_open_child_questions(parent)), [])
            finally:
                os.environ.pop("TESS_CORE_COMMS_REPLY_POLL_MS", None)
        finally:
            self.restore_home(root, previous)

    def test_child_timeout_resolves_question_visibly(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, child = self._family(workspace)
            tool = _send_message_tool()
            os.environ["TESS_CORE_COMMS_REPLY_POLL_MS"] = "50"
            try:
                result = run(tool.handler(
                    {"to": "parent", "message": "Anyone there?", "expects_reply": True, "timeout_ms": 300},
                    _context(child, workspace),
                ))
            finally:
                os.environ.pop("TESS_CORE_COMMS_REPLY_POLL_MS", None)
            payload = json.loads(result["content"])
            self.assertEqual(payload["status"], "no_reply")
            questions = run(read_task_questions("bq-task"))
            self.assertEqual(questions[0]["status"], "skipped")
            self.assertEqual(questions[0]["answer"], "unanswered:child_timeout")
            self.assertEqual((run(core.read_task_meta("bq-task")))["status"], "running")
        finally:
            self.restore_home(root, previous)

    def test_runtime_backstop_answers_every_open_question_exactly_once(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, _child = self._family(workspace)
            run(create_task_question("bq-task", {"questionId": "bq-q1", "childSessionId": "bq-child", "question": "first?"}))
            run(create_task_question("bq-task", {"questionId": "bq-q2", "childSessionId": "bq-child", "question": "second?"}))
            resolved = run(resolve_open_questions_with_backstop(parent))
            self.assertEqual({q["questionId"] for q in resolved}, {"bq-q1", "bq-q2"})
            questions = {q["questionId"]: q for q in run(read_task_questions("bq-task"))}
            self.assertEqual(questions["bq-q1"]["status"], "answered")
            self.assertEqual(questions["bq-q1"]["answer"], "unanswered:parent_turn_exhausted")
            answers = [m for m in run(core.readTaskMailboxMessages("bq-task")) if m.get("kind") == "answer"]
            self.assertEqual(len(answers), 2)
            self.assertEqual(run(resolve_open_questions_with_backstop(parent)), [])
            self.assertEqual(len([m for m in run(core.readTaskMailboxMessages("bq-task")) if m.get("kind") == "answer"]), 2)
        finally:
            self.restore_home(root, previous)

    def test_standing_reminders_inject_only_on_news(self):
        root, _home, workspace, previous = self.temp_context()
        try:
            parent, _child = self._family(workspace)
            reminded: set[str] = set()
            run(create_task_question("bq-task", {"questionId": "bq-r1", "childSessionId": "bq-child", "question": "first?"}))
            open_questions = run(inject_open_question_reminders(parent, reminded))
            self.assertEqual(len(open_questions), 1)
            self.assertEqual(len(parent["messages"]), 1)
            body = parent["messages"][0]["content"]
            text = body if isinstance(body, str) else body[0]["text"]
            self.assertIn("bq-r1", text)
            self.assertIn("bq-agent", text)

            run(inject_open_question_reminders(parent, reminded))
            self.assertEqual(len(parent["messages"]), 1)  # no news, no spam

            run(create_task_question("bq-task", {"questionId": "bq-r2", "childSessionId": "bq-child", "question": "second?"}))
            run(inject_open_question_reminders(parent, reminded))
            self.assertEqual(len(parent["messages"]), 2)
            body = parent["messages"][1]["content"]
            text = body if isinstance(body, str) else body[0]["text"]
            self.assertIn("bq-r1", text)
            self.assertIn("bq-r2", text)
        finally:
            self.restore_home(root, previous)


if __name__ == "__main__":
    unittest.main()
