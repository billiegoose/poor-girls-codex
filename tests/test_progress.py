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

    def test_progress_prefix_is_rendered_before_tool_marker(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls[:1], prefix="[closure-parity]")
            progress.render()

        self.assertEqual(
            stdout.getvalue(),
            "[closure-parity] > run      inspect-live-chatgpt-buttons-python\n",
        )

    def test_tty_progress_rewrites_each_running_row_in_place(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls)
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
        self.assertEqual(output.count("\r\x1b[2K"), 2)
        self.assertNotIn("\x1b[2A", output)

    def test_prefixed_tty_progress_rewrites_same_row(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls[:1], prefix="[closure-parity]")
            progress.set_status(0, "running")
            progress.set_status(0, "done")

        output = stdout.getvalue()
        visible = visible_text(output)
        self.assertIn("[closure-parity] > run", visible)
        self.assertIn("[running]", visible)
        self.assertTrue(visible.endswith("[done]\n"))
        self.assertEqual(output.count("\r\x1b[2K"), 1)

    def test_non_tty_progress_remains_append_only(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ToolCallProgress(self.calls[:1])
            progress.set_status(0, "running")
            progress.set_status(0, "done")

        output = stdout.getvalue()
        self.assertIn("[running]\n", output)
        self.assertTrue(output.endswith("[done]\n"))
        self.assertNotIn("\r\x1b[2K", output)

    def test_response_status_colors_sent_green_on_tty(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ResponseProgress()
            progress.set_status("[sent]")
            progress.commit()

        self.assertIn("\x1b[1;32m[sent]\x1b[0m", stdout.getvalue())

    def test_response_progress_rewrites_live_tty_row(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ResponseProgress()
            progress.set_status("[pending]")
            progress.set_status("ChatGPT send button is not available [retry in 0.25s]")
            progress.set_status("[sent]")
            progress.commit()

        output = stdout.getvalue()
        visible = visible_text(output)
        self.assertIn("response   [pending]", visible)
        self.assertIn(
            "response   ChatGPT send button is not available [retry in 0.25s]",
            visible,
        )
        self.assertIn("response   [sent]", visible)
        self.assertTrue(output.endswith("\n"))
        self.assertEqual(output.count("\r\x1b[2K"), 2)

    def test_response_progress_non_tty_reports_each_state(self) -> None:
        stdout = io.StringIO()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            progress = pgc.ResponseProgress(prefix="[closure-parity]")
            progress.set_status("[pending]")
            progress.set_status("Message too large [retry with summary]")
            progress.set_status("[sent summary]")

        self.assertEqual(
            stdout.getvalue(),
            "[closure-parity] response   [pending]\n"
            "[closure-parity] response   Message too large [retry with summary]\n"
            "[closure-parity] response   [sent summary]\n",
        )

    def test_response_progress_commits_previous_live_owner(self) -> None:
        stdout = FakeTTY()
        with mock.patch.object(pgc.sys, "stdout", stdout):
            first = pgc.ResponseProgress(prefix="[first]")
            second = pgc.ResponseProgress(prefix="[second]")
            first.set_status("[pending]")
            second.set_status("[pending]")
            second.set_status("[sent]")
            second.commit()

        output = stdout.getvalue()
        visible = visible_text(output)
        self.assertIn("[first] response   [pending]\n[second] response   [pending]", visible)
        self.assertTrue(visible.endswith("[second] response   [sent]\n"))
        self.assertEqual(output.count("\r\x1b[2K"), 1)

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
