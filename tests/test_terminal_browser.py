from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

import terminal_browser as tb
from chatgpt_web import WebSession


class TerminalBrowserTests(unittest.TestCase):
    def make_page(
        self,
        interface: tb.TerminalBrowserChatGPT,
        *,
        url: str = "https" + "://chatgpt.com/c/abc",
        target_id: str | None = "target-1",
    ) -> tb.TerminalBrowserPage:
        return tb.TerminalBrowserPage(
            interface=interface,
            browser_key="browser-1",
            tab_id=7,
            target_id=target_id,
            url=url,
            cached_title="Cats",
        )

    def test_all_pages_parses_terminal_browser_ls_json(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        payload = {
            "self": {"tab": "1", "pane": "2"},
            "browsers": [
                {
                    "key": "browser-1",
                    "tabs": [
                        {
                            "id": 7,
                            "url": "https" + "://chatgpt.com/c/abc",
                            "title": "Cats",
                            "targetId": "target-1",
                            "active": True,
                        },
                        {
                            "id": 8,
                            "url": "https" + "://example.com/",
                            "title": "Example",
                            "targetId": "target-2",
                        },
                    ],
                }
            ],
        }
        completed = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--json"],
            0,
            stdout=json.dumps(payload),
            stderr="",
        )
        with mock.patch.object(interface, "_run", return_value=completed) as run:
            pages = interface.all_pages()
            chatgpt_pages = interface.pages()

        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list, [mock.call(["ls", "--json"]), mock.call(["ls", "--json"])])
        self.assertEqual(len(pages), 2)
        self.assertEqual(pages[0].browser_key, "browser-1")
        self.assertEqual(pages[0].tab_id, 7)
        self.assertEqual(pages[0].target_id, "target-1")
        self.assertEqual(pages[0].title(), "Cats")
        self.assertEqual(chatgpt_pages, pages[:1])

    def test_empty_scoped_ls_falls_back_to_unique_browser_outside_iterm(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        scoped = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--json"],
            0,
            stdout=json.dumps({"self": None, "browsers": []}),
            stderr="",
        )
        all_payload = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--all", "--json"],
            0,
            stdout=json.dumps(
                {
                    "self": None,
                    "browsers": [
                        {
                            "key": "browser-1",
                            "tabs": [
                                {
                                    "id": 7,
                                    "url": "https" + "://chatgpt.com/c/abc",
                                    "title": "Cats",
                                    "targetId": "target-1",
                                }
                            ],
                        }
                    ],
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(interface, "_run", side_effect=[scoped, all_payload]) as run,
            mock.patch.object(interface, "_iterm_session_uuid", return_value=None),
        ):
            pages = interface.pages()

        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0].browser_key, "browser-1")
        self.assertEqual(
            run.call_args_list,
            [mock.call(["ls", "--json"]), mock.call(["ls", "--all", "--json"])],
        )

    def test_iterm_discovery_keeps_only_browser_in_current_tab(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        scoped = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"self": None, "browsers": []}), stderr=""
        )
        all_payload = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "self": None,
                    "browsers": [
                        {
                            "key": "ours",
                            "tty": "/dev/ttys008",
                            "tabs": [{"id": 7, "url": "https" + "://chatgpt.com/c/abc", "targetId": "t1"}],
                        },
                        {
                            "key": "other-tab",
                            "tty": "/dev/ttys020",
                            "tabs": [{"id": 8, "url": "https" + "://chatgpt.com/c/def", "targetId": "t2"}],
                        },
                    ],
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(interface, "_run", side_effect=[scoped, all_payload]),
            mock.patch.object(interface, "_iterm_session_uuid", return_value="SESSION"),
            mock.patch.object(interface, "_iterm_tab_ttys", return_value=frozenset({"/dev/ttys001", "/dev/ttys008"})),
        ):
            pages = interface.pages()

        self.assertEqual([(page.browser_key, page.tab_id) for page in pages], [("ours", 7)])

    def test_iterm_discovery_does_not_adopt_browser_from_another_tab(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        scoped = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"self": None, "browsers": []}), stderr=""
        )
        all_payload = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps(
                {
                    "self": None,
                    "browsers": [{"key": "other-tab", "tty": "/dev/ttys020", "tabs": []}],
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(interface, "_run", side_effect=[scoped, all_payload]),
            mock.patch.object(interface, "_iterm_session_uuid", return_value="SESSION"),
            mock.patch.object(interface, "_iterm_tab_ttys", return_value=frozenset({"/dev/ttys001", "/dev/ttys008"})),
        ):
            self.assertEqual(interface.pages(), [])

    def test_empty_scoped_ls_with_no_global_browsers_has_no_pages(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        empty = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--json"],
            0,
            stdout=json.dumps({"self": None, "browsers": []}),
            stderr="",
        )
        with (
            mock.patch.object(interface, "_run", side_effect=[empty, empty]),
            mock.patch.object(interface, "_iterm_session_uuid", return_value=None),
        ):
            self.assertEqual(interface.pages(), [])

    def test_empty_scoped_ls_rejects_multiple_global_browsers_outside_iterm(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        scoped = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--json"],
            0,
            stdout=json.dumps({"self": None, "browsers": []}),
            stderr="",
        )
        all_payload = subprocess.CompletedProcess(
            ["terminal-browser", "ls", "--all", "--json"],
            0,
            stdout=json.dumps(
                {
                    "self": None,
                    "browsers": [
                        {"key": "browser-1", "tabs": []},
                        {"key": "browser-2", "tabs": []},
                    ],
                }
            ),
            stderr="",
        )
        with (
            mock.patch.object(interface, "_run", side_effect=[scoped, all_payload]),
            mock.patch.object(interface, "_iterm_session_uuid", return_value=None),
        ):
            with self.assertRaisesRegex(RuntimeError, "2 browsers are running"):
                interface.pages()

    def test_run_surfaces_terminal_browser_stderr(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        completed = subprocess.CompletedProcess(
            ["terminal-browser", "new-tab"],
            1,
            stdout="",
            stderr="Unsupported terminal: this terminal cannot display graphics\n",
        )
        with mock.patch.object(subprocess, "run", return_value=completed):
            with self.assertRaisesRegex(RuntimeError, "Unsupported terminal"):
                interface._run(["new-tab", "example.invalid"])

    def test_action_prefers_stable_target_id(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(interface, "_run", return_value=completed) as run:
            interface.action(page, "eval", "document.title", "--json")

        run.assert_called_once_with(
            [
                "action",
                "--target",
                "target-1",
                "--",
                "eval",
                "document.title",
                "--json",
            ],
            timeout=15.0,
        )

    def test_action_falls_back_to_browser_and_tab_before_target_exists(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface, target_id=None)
        completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
        with mock.patch.object(interface, "_run", return_value=completed) as run:
            interface.action(page, "snapshot")

        self.assertEqual(
            run.call_args.args[0],
            [
                "action",
                "--browser",
                "browser-1",
                "--tab",
                "7",
                "--",
                "snapshot",
            ],
        )

    def test_eval_unwraps_agent_browser_json_result(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        completed = subprocess.CompletedProcess(
            [],
            0,
            stdout=json.dumps({"success": True, "data": {"result": {"ok": 1}}}),
            stderr="",
        )
        with mock.patch.object(interface, "action", return_value=completed):
            self.assertEqual(interface.eval(page, "1 + 1"), {"ok": 1})

    def test_recent_valid_request_scans_back_from_terminal_prose(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        session = WebSession("abc", "Cats", page)
        rows = [
            {"role": "user", "id": "u1", "text": "do it"},
            {
                "role": "assistant",
                "id": "a1",
                "text": '```json\n{"session":"root","id":"x","tool":"status"}\n```',
            },
            {"role": "user", "id": "u2", "text": "result"},
            {"role": "assistant", "id": "a2", "text": "Done."},
        ]
        with mock.patch.object(interface, "transcript", return_value=rows):
            request = interface.recent_valid_request(
                session,
                lambda value: None if value.get("tool") == "status" else (_ for _ in ()).throw(ValueError()),
            )

        self.assertIsNotNone(request)
        assert request is not None
        self.assertEqual(request[1]["id"], "x")
        self.assertEqual(request[1]["session"], "root")

    def test_terminal_response_requires_expected_user_to_be_latest_user_turn(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        session = WebSession("abc", "Cats", page)
        rows = [
            {"role": "user", "id": "u1", "text": "tool result"},
            {"role": "assistant", "id": "a1", "text": "Finished."},
        ]
        with mock.patch.object(interface, "transcript", return_value=rows):
            terminal = interface.terminal_response_candidate(session, "u1")
        self.assertIsNotNone(terminal)
        assert terminal is not None
        self.assertEqual(terminal[0], "Finished.")

        newer_user = rows + [{"role": "user", "id": "u2", "text": "new request"}]
        with mock.patch.object(interface, "transcript", return_value=newer_user):
            self.assertIsNone(interface.terminal_response_candidate(session, "u1"))

    def test_submit_uses_semantic_fill_and_native_enter(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        session = WebSession("abc", "Cats", page)
        with (
            mock.patch.object(interface, "action") as action,
            mock.patch.object(
                interface,
                "latest_user_message",
                side_effect=[None, ("u1", "RESULT")],
            ),
            mock.patch("terminal_browser.time.sleep"),
        ):
            interface.submit(session, "RESULT")

        self.assertEqual(
            action.call_args_list,
            [
                mock.call(page, "find", "role", "textbox", "fill", "RESULT"),
                mock.call(page, "press", "Enter"),
            ],
        )

    def test_submit_retries_when_hydration_loses_first_message(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        session = WebSession("abc", "Cats", page)
        with (
            mock.patch.object(interface, "action") as action,
            mock.patch.object(
                interface,
                "latest_user_message",
                side_effect=[None, ("u1", "RESULT")],
            ),
            mock.patch.object(interface, "latest_too_long_error_id", return_value=None),
            mock.patch("terminal_browser.time.sleep"),
            mock.patch("terminal_browser.time.monotonic") as monotonic,
        ):
            # Attempt 1: deadline=2, first check=3 => verification timeout.
            # Attempt 2: deadline=6, first check=4 => observe the user turn.
            monotonic.side_effect = [0.0, 3.0, 4.0, 4.0]
            interface.submit(session, "RESULT")

        self.assertEqual(action.call_count, 4)
        self.assertEqual(
            action.call_args_list,
            [
                mock.call(page, "find", "role", "textbox", "fill", "RESULT"),
                mock.call(page, "press", "Enter"),
                mock.call(page, "find", "role", "textbox", "fill", "RESULT"),
                mock.call(page, "press", "Enter"),
            ],
        )

    def test_first_new_tab_in_iterm_launches_right_split(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        new = tb.TerminalBrowserPage(
            interface=interface,
            browser_key="browser-1",
            tab_id=1,
            target_id="target-1",
            url="https" + "://chatgpt.com/",
        )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "TERM_PROGRAM": "iTerm.app",
                    "ITERM_SESSION_ID": "w0t1p0:SESSION-UUID",
                },
                clear=False,
            ),
            mock.patch.object(interface, "all_pages", return_value=[]),
            mock.patch.object(interface, "_launch_iterm_browser") as launch,
            mock.patch.object(interface, "_run") as run,
            mock.patch.object(interface, "_wait_for_new_page", return_value=new) as wait,
        ):
            self.assertIs(interface._new_tab("https" + "://chatgpt.com/"), new)

        launch.assert_called_once_with("https" + "://chatgpt.com/")
        run.assert_not_called()
        wait.assert_called_once_with(set(), browser_key=None)

    def test_iterm_tab_ttys_uses_current_session_and_caches_result(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        completed = subprocess.CompletedProcess(
            ["osascript"], 0, stdout="/dev/ttys001\n/dev/ttys008\n", stderr=""
        )
        with (
            mock.patch.object(interface, "_iterm_session_uuid", return_value="SESSION-UUID"),
            mock.patch("terminal_browser.subprocess.run", return_value=completed) as run,
        ):
            first = interface._iterm_tab_ttys()
            second = interface._iterm_tab_ttys()

        self.assertEqual(first, frozenset({"/dev/ttys001", "/dev/ttys008"}))
        self.assertIs(first, second)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0][3:], ["--", "SESSION-UUID"])

    def test_iterm_launcher_targets_current_session_and_uses_vertical_split(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        interface._iterm_tab_ttys_cache = frozenset({"/dev/ttys001"})
        completed = subprocess.CompletedProcess(
            ["osascript"],
            0,
            stdout="NEW-SESSION\n",
            stderr="",
        )
        with (
            mock.patch.dict(
                "os.environ",
                {
                    "TERM_PROGRAM": "iTerm.app",
                    "ITERM_SESSION_ID": "w0t1p0:SESSION-UUID",
                },
                clear=False,
            ),
            mock.patch("terminal_browser.shutil.which", return_value="/usr/local/bin/terminal-browser"),
            mock.patch("terminal_browser.subprocess.run", return_value=completed) as run,
        ):
            interface._launch_iterm_browser("https" + "://chatgpt.com/")

        args = run.call_args.args[0]
        self.assertEqual(args[:2], ["osascript", "-e"])
        self.assertIn("split vertically with same profile", args[2])
        self.assertIn("delay 0.75", args[2])
        self.assertIn("write created text browserCommand", args[2])
        self.assertNotIn("command shellCommand", args[2])
        self.assertEqual(args[3], "--")
        self.assertEqual(args[4], "SESSION-UUID")
        self.assertEqual(
            args[5],
            "exec /usr/local/bin/terminal-browser open " + "https" + "://chatgpt.com/",
        )
        self.assertIsNone(interface._iterm_tab_ttys_cache)

    def test_new_tab_stays_in_origins_terminal_browser(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        old = self.make_page(interface)
        new = tb.TerminalBrowserPage(
            interface=interface,
            browser_key="browser-1",
            tab_id=8,
            target_id="target-2",
            url="https" + "://chatgpt.com/",
        )
        with (
            mock.patch.object(interface, "all_pages", return_value=[old]),
            mock.patch.object(interface, "_run") as run,
            mock.patch.object(interface, "_wait_for_new_page", return_value=new) as wait,
        ):
            self.assertIs(interface._new_tab("https" + "://chatgpt.com/", browser_key="browser-1"), new)

        run.assert_called_once_with(
            ["new-tab", "--browser", "browser-1", "https" + "://chatgpt.com/"],
            timeout=tb.TAB_START_TIMEOUT_SECONDS,
        )
        wait.assert_called_once_with({("browser-1", 7)}, browser_key="browser-1")

    def test_close_page_uses_terminal_browser_tab_lifecycle(self) -> None:
        interface = tb.TerminalBrowserChatGPT()
        page = self.make_page(interface)
        with mock.patch.object(interface, "_run") as run:
            page.close()
        run.assert_called_once_with(
            [
                "action",
                "--browser",
                "browser-1",
                "--tab",
                "7",
                "--",
                "tab",
                "close",
            ],
            timeout=10.0,
        )


if __name__ == "__main__":
    unittest.main()
