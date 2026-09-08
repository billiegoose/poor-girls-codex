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

        def fake_paste(app, root, text, *, send):
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
