from __future__ import annotations

import io
import re
import unittest
from unittest import mock

import poor_girls_codex as pgc


ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")


def visible_text(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class ToolCallProgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = [
            {"id": "inspect-live-chatgpt-buttons-python", "tool": "run"},
            {"id": "inspect-streaming-tests-for-completion-mocks", "tool": "read"},
        ]

    def test_initial_render_lists_all_calls_without_status(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls)
            progress.render()

        self.assertEqual(
            stdout.getvalue(),
            "  > run      inspect-live-chatgpt-buttons-python\n"
            "  > read     inspect-streaming-tests-for-completion-mocks\n",
        )

    def test_tty_updates_redraw_full_call_list_with_statuses(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls)
            progress.render()
            progress.set_status(0, "running")
            progress.set_status(0, "done")
            progress.set_status(1, "running")
            progress.set_status(1, "done")

        output = stdout.getvalue()
        visible = visible_text(output)
        self.assertIn("inspect-live-chatgpt-buttons-python [running]", visible)
        self.assertIn("inspect-live-chatgpt-buttons-python [done]", visible)
        self.assertIn("inspect-streaming-tests-for-completion-mocks [running]", visible)
        self.assertTrue(visible.endswith("inspect-streaming-tests-for-completion-mocks [done]\n"))
        self.assertEqual(output.count("\x1b[2A"), 4)

    def test_execute_request_marks_each_call_running_then_done(self) -> None:
        stdout = FakeTTY()
        with (
            mock.patch.object(pgc.sys, "stdout", stdout),
            mock.patch.object(
                pgc.toolcall_lib,
                "execute",
                side_effect=[
                    {"id": self.calls[0]["id"], "tool": "run", "ok": True},
                    {"id": self.calls[1]["id"], "tool": "read", "ok": True},
                ],
            ) as execute,
        ):
            result = pgc.execute_request({"calls": self.calls}, announce=True)

        self.assertEqual(execute.call_count, 2)
        self.assertEqual(len(result), 2)
        visible = visible_text(stdout.getvalue())
        self.assertIn("inspect-live-chatgpt-buttons-python [running]", visible)
        self.assertIn("inspect-live-chatgpt-buttons-python [done]", visible)
        self.assertIn("inspect-streaming-tests-for-completion-mocks [running]", visible)
        self.assertIn("inspect-streaming-tests-for-completion-mocks [done]", visible)


if __name__ == "__main__":
    unittest.main()
