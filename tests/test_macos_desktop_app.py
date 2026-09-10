from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

import macos_desktop_app as desktop


class MacOSDesktopAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frontend = desktop.MacOSDesktopApp()

    def test_ui_contains_text_scans_accessibility_attributes(self) -> None:
        root = object()
        child = object()
        needle = "The message you submitted was too long"
        values = {
            (root, "AXChildren"): [child],
            (child, "AXChildren"): [],
            (child, "AXValue"): needle,
        }

        with mock.patch.object(
            desktop.probe,
            "ax_attr",
            side_effect=lambda element, attr: values.get((element, attr)),
        ):
            self.assertTrue(self.frontend.ui_contains_text(root, needle))
            self.assertFalse(self.frontend.ui_contains_text(root, "definitely absent"))

    def test_too_long_detector_ignores_equal_conversation_proxy(self) -> None:
        class AxProxy:
            def __init__(self, identity: str) -> None:
                self.identity = identity

            def __eq__(self, other) -> bool:
                return isinstance(other, AxProxy) and self.identity == other.identity

            def __hash__(self) -> int:
                return hash(self.identity)

        needle = "The message you submitted was too long"
        root = AxProxy("root")
        conversation_from_selector = AxProxy("conversation")
        conversation_from_tree = AxProxy("conversation")
        transcript_text = AxProxy("transcript")
        banner = AxProxy("banner")
        values = {
            (root, "AXChildren"): [conversation_from_tree],
            (conversation_from_tree, "AXChildren"): [transcript_text],
            (transcript_text, "AXChildren"): [],
            (transcript_text, "AXValue"): needle,
        }

        self.assertIsNot(conversation_from_selector, conversation_from_tree)
        self.assertEqual(conversation_from_selector, conversation_from_tree)

        with (
            mock.patch.object(self.frontend, "conversation_group", return_value=conversation_from_selector),
            mock.patch.object(
                desktop.probe,
                "ax_attr",
                side_effect=lambda element, attr: values.get((element, attr)),
            ),
        ):
            self.assertFalse(self.frontend.ui_contains_text_outside_conversation(root, needle))

        values[(root, "AXChildren")] = [conversation_from_tree, banner]
        values[(banner, "AXChildren")] = []
        values[(banner, "AXValue")] = needle
        with (
            mock.patch.object(self.frontend, "conversation_group", return_value=conversation_from_selector),
            mock.patch.object(
                desktop.probe,
                "ax_attr",
                side_effect=lambda element, attr: values.get((element, attr)),
            ),
        ):
            self.assertTrue(self.frontend.ui_contains_text_outside_conversation(root, needle))

    def test_manual_accessibility_dump_records_reason_and_tree(self) -> None:
        output = mock.mock_open()
        with (
            mock.patch.object(self.frontend, "root", return_value=("app", "root")),
            mock.patch.object(
                desktop.probe,
                "dump_tree",
                return_value=["APP: Role='AXApplication'", "AX nodes dumped: 1"],
            ),
            mock.patch.object(desktop.time, "strftime", return_value="20260908-202500"),
            mock.patch.object(desktop.time, "time_ns", return_value=123456789),
            mock.patch("builtins.open", output),
        ):
            path = self.frontend.save_debug_dump()

        self.assertEqual(path, "poor-girls-codex-ax-dump-20260908-202500-123456789.txt")
        contents = output().write.call_args.args[0]
        self.assertIn("reason: manual Ctrl-X dump", contents)
        self.assertIn("=== Full accessibility tree ===", contents)
        self.assertIn("AXApplication", contents)

    def test_pid_targeted_return_retries_with_exponential_backoff(self) -> None:
        composer = object()
        app = mock.Mock()
        app.processIdentifier.return_value = 123
        values = iter(["payload", "payload", ""])
        monotonic_values = iter([0.0, 2.1, 3.0, 5.1])

        with (
            mock.patch.object(desktop, "RETURN_RETRY_INITIAL_SECONDS", 0.25),
            mock.patch.object(desktop, "RETURN_RETRY_MAX_SECONDS", 8.0),
            mock.patch.object(self.frontend, "root", return_value=(app, "root")),
            mock.patch.object(self.frontend, "find_elements", return_value=[composer]),
            mock.patch.object(desktop.probe, "ax_attr", side_effect=lambda element, attr: next(values)),
            mock.patch.object(desktop.AS, "AXUIElementSetAttributeValue", return_value=0),
            mock.patch.object(desktop.Quartz, "CGEventCreateKeyboardEvent", return_value=object()),
            mock.patch.object(desktop.Quartz, "CGEventPostToPid") as post,
            mock.patch.object(desktop.time, "monotonic", side_effect=lambda: next(monotonic_values)),
            mock.patch.object(desktop.time, "sleep") as sleep,
            redirect_stdout(StringIO()) as stdout,
        ):
            self.frontend._submit_composer_to_pid(app, composer, "payload")

        self.assertEqual(post.call_count, 4)
        sleeps = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(0.25, sleeps)
        self.assertIn(0.5, sleeps)
        output = stdout.getvalue()
        self.assertIn("PID-targeted Return", output)
        self.assertIn("retrying in 0.25s", output)
        self.assertIn("retrying in 0.5s", output)


if __name__ == "__main__":
    unittest.main()
