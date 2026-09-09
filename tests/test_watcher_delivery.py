from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest import mock

import poor_girls_codex as pgc


class WatcherDeliveryTests(unittest.TestCase):
    def test_send_button_retries_with_exponential_backoff(self) -> None:
        send_button = object()
        composer = object()
        send_scans = iter([[], [], [send_button]])

        def fake_find_elements(root, *, role=None, description=None):
            if role == "AXButton":
                return next(send_scans)
            if role == "AXTextArea":
                return [composer]
            self.fail(f"unexpected selector: role={role!r} description={description!r}")

        with (
            mock.patch.object(pgc, "SEND_RETRY_INITIAL_SECONDS", 0.25),
            mock.patch.object(pgc, "SEND_RETRY_MAX_SECONDS", 8.0),
            mock.patch.object(pgc, "set_composer_text") as set_text,
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(pgc, "find_elements", side_effect=fake_find_elements),
            mock.patch.object(pgc.probe, "ax_attr", return_value=True),
            mock.patch.object(pgc, "submit_composer_to_pid") as submit,
            mock.patch.object(pgc.time, "sleep") as sleep,
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.paste_result_into_composer("app", "root", "payload", send=True)

        self.assertEqual(set_text.call_count, 3)
        submit.assert_called_once_with("app", composer, "payload")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.1, 0.25, 0.1, 0.5, 0.1],
        )
        output = stdout.getvalue()
        self.assertIn("watcher warning", output)
        self.assertIn("retrying in 0.25s", output)
        self.assertIn("retrying in 0.5s", output)

    def test_send_wait_is_interrupted_by_new_tool_call(self) -> None:
        candidate_a = ('{"id":"a","tool":"read"}', {"id": "a", "tool": "read"}, "fingerprint-a")
        candidate_b = ('{"id":"b","tool":"read"}', {"id": "b", "tool": "read"}, "fingerprint-b")

        with (
            mock.patch.object(pgc, "latest_valid_request", side_effect=[candidate_a, candidate_b]),
            mock.patch.object(pgc, "set_composer_text") as set_text,
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "refreshed-root")),
            mock.patch.object(pgc, "submit_composer_to_pid") as submit,
            mock.patch.object(pgc.time, "sleep"),
        ):
            outcome = pgc.paste_result_into_composer(
                "app",
                "initial-root",
                "payload",
                send=True,
                known_fingerprints={"fingerprint-a"},
            )

        self.assertIs(outcome, pgc.DeliveryOutcome.INTERRUPTED)
        self.assertEqual(
            set_text.call_args_list,
            [mock.call("initial-root", "payload"), mock.call("refreshed-root", "")],
        )
        submit.assert_not_called()

    def test_pid_targeted_return_retries_with_exponential_backoff(self) -> None:
        composer = object()
        app = mock.Mock()
        app.processIdentifier.return_value = 123
        # Attempt 1 pre-check, attempt 2 pre-check, then the previous Return is
        # observed to have finally cleared the composer before attempt 3.
        values = iter(["payload", "payload", ""])
        monotonic_values = iter([0.0, 2.1, 3.0, 5.1])

        with (
            mock.patch.object(pgc, "RETURN_RETRY_INITIAL_SECONDS", 0.25),
            mock.patch.object(pgc, "RETURN_RETRY_MAX_SECONDS", 8.0),
            mock.patch.object(pgc, "chatgpt_root", return_value=(app, "root")),
            mock.patch.object(pgc, "find_elements", return_value=[composer]),
            mock.patch.object(pgc.probe, "ax_attr", side_effect=lambda element, attr: next(values)),
            mock.patch.object(pgc.AS, "AXUIElementSetAttributeValue", return_value=0),
            mock.patch.object(pgc.Quartz, "CGEventCreateKeyboardEvent", return_value=object()),
            mock.patch.object(pgc.Quartz, "CGEventPostToPid") as post,
            mock.patch.object(pgc.time, "monotonic", side_effect=lambda: next(monotonic_values)),
            mock.patch.object(pgc.time, "sleep") as sleep,
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.submit_composer_to_pid(app, composer, "payload")

        # Two Return keypresses, each consisting of key-down + key-up.
        self.assertEqual(post.call_count, 4)
        sleeps = [call.args[0] for call in sleep.call_args_list]
        self.assertIn(0.25, sleeps)
        self.assertIn(0.5, sleeps)
        output = stdout.getvalue()
        self.assertIn("PID-targeted Return", output)
        self.assertIn("retrying in 0.25s", output)
        self.assertIn("retrying in 0.5s", output)

    def test_send_button_backoff_caps_and_keeps_retrying(self) -> None:
        send_button = object()
        composer = object()
        send_scans = iter([[], [], [], [], [send_button]])

        def fake_find_elements(root, *, role=None, description=None):
            if role == "AXButton":
                return next(send_scans)
            if role == "AXTextArea":
                return [composer]
            self.fail(f"unexpected selector: role={role!r} description={description!r}")

        with (
            mock.patch.object(pgc, "SEND_RETRY_INITIAL_SECONDS", 1.0),
            mock.patch.object(pgc, "SEND_RETRY_MAX_SECONDS", 2.0),
            mock.patch.object(pgc, "set_composer_text"),
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(pgc, "find_elements", side_effect=fake_find_elements),
            mock.patch.object(pgc.probe, "ax_attr", return_value=True),
            mock.patch.object(pgc, "submit_composer_to_pid") as submit,
            mock.patch.object(pgc.time, "sleep") as sleep,
            redirect_stdout(StringIO()),
        ):
            pgc.paste_result_into_composer("app", "root", "payload", send=True)

        submit.assert_called_once_with("app", composer, "payload")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [0.1, 1.0, 0.1, 2.0, 0.1, 2.0, 0.1, 2.0, 0.1],
        )

    def test_too_long_fallback_is_small_and_summarizes_calls(self) -> None:
        calls = [
            {"id": f"call-{index}", "tool": "read"}
            for index in range(15)
        ]
        results = [
            {"id": f"call-{index}", "tool": "read", "ok": index != 3}
            for index in range(15)
        ]

        message = pgc.too_long_fallback(calls, results)

        self.assertIn("full tool results were too large", message)
        self.assertIn("The tools already ran", message)
        self.assertIn("call-0: read (ok)", message)
        self.assertIn("call-3: read (error)", message)
        self.assertIn("and 3 more tool calls", message)
        self.assertIn("Retry with smaller chunks", message)
        self.assertNotIn("call-14: read", message)
        self.assertLess(len(message), 2000)

    def test_ui_contains_text_scans_accessibility_attributes(self) -> None:
        root = object()
        child = object()
        values = {
            (root, "AXChildren"): [child],
            (child, "AXChildren"): [],
            (child, "AXValue"): pgc.MESSAGE_TOO_LONG_TEXT,
        }

        with mock.patch.object(
            pgc.probe,
            "ax_attr",
            side_effect=lambda element, attr: values.get((element, attr)),
        ):
            self.assertTrue(pgc.ui_contains_text(root, pgc.MESSAGE_TOO_LONG_TEXT))
            self.assertFalse(pgc.ui_contains_text(root, "definitely absent"))

    def test_too_long_detector_ignores_verbatim_text_inside_conversation(self) -> None:
        class AxProxy:
            def __init__(self, identity: str) -> None:
                self.identity = identity

            def __eq__(self, other) -> bool:
                return isinstance(other, AxProxy) and self.identity == other.identity

            def __hash__(self) -> int:
                return hash(self.identity)

        root = AxProxy("root")
        conversation_from_selector = AxProxy("conversation")
        conversation_from_tree = AxProxy("conversation")
        transcript_text = AxProxy("transcript")
        banner = AxProxy("banner")
        values = {
            (root, "AXChildren"): [conversation_from_tree],
            (conversation_from_tree, "AXChildren"): [transcript_text],
            (transcript_text, "AXChildren"): [],
            (transcript_text, "AXValue"): pgc.MESSAGE_TOO_LONG_TEXT,
        }

        self.assertIsNot(conversation_from_selector, conversation_from_tree)
        self.assertEqual(conversation_from_selector, conversation_from_tree)

        with (
            mock.patch.object(pgc, "conversation_group", return_value=conversation_from_selector),
            mock.patch.object(
                pgc.probe,
                "ax_attr",
                side_effect=lambda element, attr: values.get((element, attr)),
            ),
        ):
            self.assertFalse(
                pgc.ui_contains_text_outside_conversation(root, pgc.MESSAGE_TOO_LONG_TEXT)
            )

        values[(root, "AXChildren")] = [conversation_from_tree, banner]
        values[(banner, "AXChildren")] = []
        values[(banner, "AXValue")] = pgc.MESSAGE_TOO_LONG_TEXT
        with (
            mock.patch.object(pgc, "conversation_group", return_value=conversation_from_selector),
            mock.patch.object(
                pgc.probe,
                "ax_attr",
                side_effect=lambda element, attr: values.get((element, attr)),
            ),
        ):
            self.assertTrue(
                pgc.ui_contains_text_outside_conversation(root, pgc.MESSAGE_TOO_LONG_TEXT)
            )

    def test_too_long_error_sends_compact_summary_without_reexecuting_tools(self) -> None:
        request = {"id": "large-read", "tool": "read", "path": "huge.txt"}
        candidate = ('{"id":"large-read","tool":"read","path":"huge.txt"}', request, "fingerprint")
        latest_calls = 0
        executions = 0
        deliveries: list[str] = []
        too_long_scans = iter([False, False, False, True, True])

        def fake_latest(root):
            nonlocal latest_calls
            latest_calls += 1
            if latest_calls == 1:
                return None
            if latest_calls <= 5:
                return candidate
            raise KeyboardInterrupt

        def fake_execute(request, *, announce=False):
            nonlocal executions
            executions += 1
            return {
                "id": "large-read",
                "tool": "read",
                "ok": True,
                "content": "x" * 100_000,
            }

        def fake_paste(app, root, text, *, send, **kwargs):
            deliveries.append(text)

        with (
            mock.patch.object(pgc, "clipboard_write"),
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(pgc, "dismiss_work_prompt", return_value=False),
            mock.patch.object(pgc, "latest_valid_request", side_effect=fake_latest),
            mock.patch.object(pgc, "ui_contains_text_outside_conversation", side_effect=lambda root, needle: next(too_long_scans)),
            mock.patch.object(pgc, "execute_request", side_effect=fake_execute),
            mock.patch.object(pgc, "paste_result_into_composer", side_effect=fake_paste),
            mock.patch.object(pgc, "SETTLE_SECONDS", 0.0),
            mock.patch.object(pgc, "POLL_SECONDS", 0.0),
            mock.patch.object(pgc.time, "sleep"),
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.watch_loop()

        self.assertEqual(executions, 1)
        self.assertEqual(len(deliveries), 2)
        self.assertIn('"content": "' + "x" * 100, deliveries[0])
        self.assertIn("full tool results were too large", deliveries[1])
        self.assertIn("The tools already ran; do not repeat them", deliveries[1])
        self.assertLess(len(deliveries[1]), 2000)
        output = stdout.getvalue()
        self.assertIn("queued a compact retry while retaining completed results", output)
        self.assertGreaterEqual(output.count("sent results"), 2)

    def test_manual_accessibility_dump_records_reason_and_tree(self) -> None:
        output = mock.mock_open()
        with (
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(
                pgc.probe,
                "dump_tree",
                return_value=["APP: Role='AXApplication'", "AX nodes dumped: 1"],
            ),
            mock.patch.object(pgc.time, "strftime", return_value="20260908-202500"),
            mock.patch.object(pgc.time, "time_ns", return_value=123456789),
            mock.patch("builtins.open", output),
        ):
            path = pgc.save_accessibility_dump()

        self.assertEqual(path, "poor-girls-codex-ax-dump-20260908-202500-123456789.txt")
        contents = output().write.call_args.args[0]
        self.assertIn("reason: manual Ctrl-X dump", contents)
        self.assertIn("=== Full accessibility tree ===", contents)
        self.assertIn("AXApplication", contents)

    def test_ctrl_x_dumps_while_watcher_waits(self) -> None:
        hotkeys = mock.MagicMock()
        hotkeys.__enter__.return_value = hotkeys
        hotkeys.read.return_value = "\x18"

        with (
            mock.patch.object(pgc, "clipboard_write"),
            mock.patch.object(
                pgc,
                "chatgpt_root",
                side_effect=[("app", "root"), KeyboardInterrupt()],
            ),
            mock.patch.object(pgc, "latest_valid_request", return_value=None),
            mock.patch.object(pgc, "ui_contains_text", return_value=False),
            mock.patch.object(pgc, "TerminalHotkeys", return_value=hotkeys),
            mock.patch.object(pgc, "save_accessibility_dump", return_value="dump.txt") as dump,
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.watch_loop()

        dump.assert_called_once_with()
        hotkeys.__exit__.assert_called_once()
        self.assertIn("Press Ctrl-X", stdout.getvalue())
        self.assertIn("accessibility dump: dump.txt", stdout.getvalue())

    def test_terminal_hotkeys_reads_ctrl_x_and_restores_terminal(self) -> None:
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        stdin.fileno.return_value = 7
        saved = ["terminal-state"]

        with (
            mock.patch.object(pgc.sys, "stdin", stdin),
            mock.patch.object(pgc.termios, "tcgetattr", return_value=saved),
            mock.patch.object(pgc.tty, "setcbreak") as setcbreak,
            mock.patch.object(pgc.select, "select", return_value=([7], [], [])),
            mock.patch.object(pgc.os, "read", return_value=b"\x18"),
            mock.patch.object(pgc.termios, "tcsetattr") as restore,
        ):
            with pgc.TerminalHotkeys() as hotkeys:
                self.assertEqual(hotkeys.read(), "\x18")

        setcbreak.assert_called_once_with(7)
        restore.assert_called_once_with(7, pgc.termios.TCSADRAIN, saved)

    def test_new_tool_call_interrupts_delivery_and_combines_completed_results(self) -> None:
        request_a = {"id": "call-a", "tool": "read", "path": "a.txt"}
        request_b = {"id": "call-b", "tool": "read", "path": "b.txt"}
        candidate_a = ('{"id":"call-a","tool":"read","path":"a.txt"}', request_a, "fingerprint-a")
        candidate_b = ('{"id":"call-b","tool":"read","path":"b.txt"}', request_b, "fingerprint-b")
        candidates = iter([
            None,          # startup scan
            candidate_a,   # discover A
            candidate_a,   # settle A
            candidate_a,   # A remains latest while first delivery begins
            candidate_b,   # after interrupted delivery, discover B
            candidate_b,   # settle B
            candidate_b,   # B remains latest while combined delivery begins
        ])
        executions: list[str] = []
        deliveries: list[str] = []
        delivery_outcomes = iter([
            pgc.DeliveryOutcome.INTERRUPTED,
            pgc.DeliveryOutcome.SENT,
        ])

        def fake_latest(root):
            try:
                return next(candidates)
            except StopIteration:
                raise KeyboardInterrupt

        def fake_execute(request, *, announce=False):
            executions.append(request["id"])
            return {"id": request["id"], "tool": request["tool"], "ok": True}

        def fake_paste(app, root, text, *, send, **kwargs):
            deliveries.append(text)
            return next(delivery_outcomes)

        with (
            mock.patch.object(pgc, "clipboard_write"),
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(pgc, "dismiss_work_prompt", return_value=False),
            mock.patch.object(pgc, "latest_valid_request", side_effect=fake_latest),
            mock.patch.object(pgc, "ui_contains_text", return_value=False),
            mock.patch.object(pgc, "execute_request", side_effect=fake_execute),
            mock.patch.object(pgc, "paste_result_into_composer", side_effect=fake_paste),
            mock.patch.object(pgc, "SETTLE_SECONDS", 0.0),
            mock.patch.object(pgc, "POLL_SECONDS", 0.0),
            mock.patch.object(pgc.time, "sleep"),
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.watch_loop()

        self.assertEqual(executions, ["call-a", "call-b"])
        self.assertEqual(len(deliveries), 2)
        self.assertIn('"id": "call-a"', deliveries[0])
        self.assertNotIn('"id": "call-b"', deliveries[0])
        self.assertEqual(deliveries[1].count('"id": "call-a"'), 1)
        self.assertEqual(deliveries[1].count('"id": "call-b"'), 1)
        self.assertIn("delivery interrupted by new tool calls", stdout.getvalue())

    def test_delivery_error_does_not_reexecute_same_toolcall(self) -> None:
        request = {"id": "side-effect", "tool": "run", "script": "echo hi"}
        candidate = ('{"id":"side-effect","tool":"run","script":"echo hi"}', request, "fingerprint")
        latest_calls = 0
        executions = 0
        deliveries = 0

        def fake_latest(root):
            nonlocal latest_calls
            latest_calls += 1
            if latest_calls == 1:
                # Initial startup scan: no request is considered pre-existing.
                return None
            if latest_calls <= 4:
                return candidate
            raise KeyboardInterrupt

        def fake_execute(request, *, announce=False):
            nonlocal executions
            executions += 1
            return {"id": "side-effect", "tool": "run", "ok": True}

        def fake_paste(app, root, text, *, send, **kwargs):
            nonlocal deliveries
            deliveries += 1
            raise RuntimeError("synthetic delivery failure")

        with (
            mock.patch.object(pgc, "clipboard_write"),
            mock.patch.object(pgc, "chatgpt_root", return_value=("app", "root")),
            mock.patch.object(pgc, "dismiss_work_prompt", return_value=False),
            mock.patch.object(pgc, "latest_valid_request", side_effect=fake_latest),
            mock.patch.object(pgc, "execute_request", side_effect=fake_execute),
            mock.patch.object(pgc, "paste_result_into_composer", side_effect=fake_paste),
            mock.patch.object(pgc, "SETTLE_SECONDS", 0.0),
            mock.patch.object(pgc, "POLL_SECONDS", 0.0),
            mock.patch.object(pgc.time, "sleep"),
            redirect_stdout(StringIO()) as stdout,
        ):
            pgc.watch_loop()

        self.assertEqual(executions, 1)
        self.assertEqual(deliveries, 1)
        self.assertIn("watcher error: synthetic delivery failure", stdout.getvalue())


if __name__ == "__main__":
    unittest.main()
