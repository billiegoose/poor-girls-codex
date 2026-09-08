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

    def test_tool_level_stop_on_error_skips_only_later_calls(self) -> None:
        calls = [
            {"id": "first", "tool": "read"},
            {"id": "second", "tool": "run", "stop_on_error": True},
            {"id": "third", "tool": "read"},
        ]
        with mock.patch.object(
            pgc.toolcall_lib,
            "execute",
            side_effect=[
                {"id": "first", "tool": "read", "ok": True},
                {"id": "second", "tool": "run", "ok": False, "error": "boom"},
            ],
        ) as execute:
            results = pgc.execute_request({"calls": calls})

        self.assertEqual(execute.call_count, 2)
        self.assertEqual([result["id"] for result in results], ["first", "second", "third"])
        self.assertTrue(results[0]["ok"])
        self.assertFalse(results[1]["ok"])
        self.assertTrue(results[2]["skipped"])
        self.assertIn("stop_on_error", results[2]["error"])
        self.assertNotIn("stop_on_error", execute.call_args_list[1].args[0])

    def test_failed_call_without_stop_on_error_does_not_stop_batch(self) -> None:
        calls = [
            {"id": "first", "tool": "run"},
            {"id": "second", "tool": "read"},
        ]
        with mock.patch.object(
            pgc.toolcall_lib,
            "execute",
            side_effect=[
                {"id": "first", "tool": "run", "ok": False, "error": "boom"},
                {"id": "second", "tool": "read", "ok": True},
            ],
        ) as execute:
            results = pgc.execute_request({"calls": calls})

        self.assertEqual(execute.call_count, 2)
        self.assertFalse(results[0]["ok"])
        self.assertTrue(results[1]["ok"])

    def test_top_level_stop_on_error_warns_and_preserves_legacy_behavior(self) -> None:
        calls = [
            {"id": "first", "tool": "run"},
            {"id": "second", "tool": "read"},
        ]
        stdout = io.StringIO()
        with (
            mock.patch.object(
                pgc.toolcall_lib,
                "execute",
                return_value={"id": "first", "tool": "run", "ok": False, "error": "boom"},
            ) as execute,
            mock.patch.object(pgc.sys, "stdout", stdout),
        ):
            results = pgc.execute_request({"calls": calls, "stop_on_error": True})

        self.assertEqual(execute.call_count, 1)
        self.assertTrue(results[1]["skipped"])
        self.assertIn("deprecation warning", stdout.getvalue())
        self.assertIn("individual call", stdout.getvalue())

    def test_stop_on_error_must_be_boolean_at_both_levels(self) -> None:
        with self.assertRaisesRegex(ValueError, "top-level stop_on_error must be a boolean"):
            pgc.validate_request({"calls": [{"id": "a", "tool": "read"}], "stop_on_error": "yes"})
        with self.assertRaisesRegex(ValueError, "call 0 stop_on_error must be a boolean"):
            pgc.validate_request({"calls": [{"id": "a", "tool": "read", "stop_on_error": "yes"}]})

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
