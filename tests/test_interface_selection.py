from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import poor_girls_codex as pgc
import toolcall_lib


class InterfaceSelectionTests(unittest.TestCase):
    def test_web_default_cdp_url_is_plain_url(self) -> None:
        import chatgpt_web

        self.assertEqual(chatgpt_web.DEFAULT_CDP_URL, 'http' + '://127.0.0.1:9222')

    def test_session_id_is_persistent_and_locally_git_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            subprocess.run(['git', 'init', '-q'], cwd=cwd, check=True)
            first = pgc.load_or_create_session_id(cwd)
            second = pgc.load_or_create_session_id(cwd)
            self.assertEqual(first, second)
            self.assertTrue(first)
            with open(os.path.join(cwd, '.pgc', 'session'), encoding='utf-8') as session_file:
                self.assertEqual(session_file.read().strip(), first)
            for path in ('.pgc/session', '.pgc/ntfy', '.pgc/conversations.json'):
                ignored = subprocess.run(
                    ['git', 'check-ignore', '-q', path],
                    cwd=cwd,
                    check=False,
                )
                self.assertEqual(ignored.returncode, 0)

    def test_conversation_history_round_trips_and_updates_last_seen(self) -> None:
        with tempfile.TemporaryDirectory() as cwd:
            pgc.remember_conversation('a', 'Cats', cwd=cwd, now=1.0)
            pgc.remember_conversation('b', 'Dogs', cwd=cwd, now=2.0)
            pgc.remember_conversation('a', 'Cats renamed', cwd=cwd, now=3.0)
            history = pgc.load_conversation_history(cwd)
            self.assertEqual(
                history,
                [
                    {'conversation_id': 'a', 'title': 'Cats renamed', 'last_seen': 3.0},
                    {'conversation_id': 'b', 'title': 'Dogs', 'last_seen': 2.0},
                ],
            )
            with open(os.path.join(cwd, '.pgc', 'conversations.json'), encoding='utf-8') as history_file:
                self.assertIn('Cats renamed', history_file.read())

    def test_terminal_session_title_uses_cwd_basename_and_browser_title(self) -> None:
        stream = mock.Mock()
        stream.isatty.return_value = True

        pgc.set_terminal_session_title(
            'Root Browser Title',
            cwd='/tmp/projects/asgard',
            stream=stream,
        )

        stream.write.assert_called_once_with('\033]0;asgard · Root Browser Title\007')
        stream.flush.assert_called_once_with()

    def test_terminal_session_title_is_not_written_when_stdout_is_not_a_tty(self) -> None:
        stream = mock.Mock()
        stream.isatty.return_value = False

        pgc.set_terminal_session_title('Root Browser Title', stream=stream)

        stream.write.assert_not_called()
        stream.flush.assert_not_called()

    def test_web_session_attachment_sets_title_for_root_only(self) -> None:
        root = mock.Mock(
            conversation_id='root-chat',
            label='Cached Root Title',
            routing_session_id='root',
        )
        root.page.title.return_value = 'Live Root Browser Title'
        child = mock.Mock(
            conversation_id='child-chat',
            label='Child',
            routing_session_id='root::worker',
        )

        with (
            mock.patch.object(pgc, 'remember_conversation') as remember,
            mock.patch.object(pgc, 'set_terminal_session_title') as set_title,
        ):
            pgc.note_web_session_attached(root, 'root')
            pgc.note_web_session_attached(child, 'root')

        self.assertEqual(
            remember.call_args_list,
            [
                mock.call('root-chat', 'Cached Root Title', cwd='.'),
                mock.call('child-chat', 'Child', cwd='.'),
            ],
        )
        set_title.assert_called_once_with('Live Root Browser Title', cwd='.')

    def test_choose_saved_conversation_defaults_to_new_and_accepts_index(self) -> None:
        history = [
            {'conversation_id': 'a', 'title': 'Cats', 'last_seen': 2.0},
            {'conversation_id': 'b', 'title': 'Dogs', 'last_seen': 1.0},
        ]
        with mock.patch('builtins.input', return_value=''):
            self.assertIsNone(pgc.choose_saved_conversation(history))
        with mock.patch('builtins.input', return_value='2'):
            self.assertEqual(pgc.choose_saved_conversation(history), 'b')

    def test_choose_saved_conversation_reprompts_after_invalid_choice(self) -> None:
        history = [
            {'conversation_id': 'a', 'title': 'Cats', 'last_seen': 1.0},
        ]
        with mock.patch('builtins.input', side_effect=['bogus', '9', '1']):
            with mock.patch('builtins.print') as printed:
                self.assertEqual(pgc.choose_saved_conversation(history), 'a')
        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn('Please enter a number', output)
        self.assertIn('Please choose one of the listed conversations', output)

    def test_web_startup_ui_prints_ntfy_topic_aesthetically(self) -> None:
        clipboard_write = mock.Mock()
        with mock.patch('builtins.print') as printed:
            pgc.show_startup_ui(
                'BOOTSTRAP',
                clipboard_write=clipboard_write,
                web_managed=True,
                ntfy_topic='poor_girls_codex_billiegoose',
            )
        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn('notifications', output)
        self.assertIn('ntfy', output)
        self.assertIn('poor_girls_codex_billiegoose', output)
        clipboard_write.assert_called_once_with('BOOTSTRAP')

    def test_web_startup_ui_prints_ntfy_off_when_disabled(self) -> None:
        with mock.patch('builtins.print') as printed:
            pgc.show_startup_ui(
                'BOOTSTRAP',
                clipboard_write=mock.Mock(),
                web_managed=True,
                ntfy_topic='',
            )
        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn('notifications', output)
        self.assertIn('ntfy', output)
        self.assertIn('off', output)

    def test_ntfy_topic_falls_back_to_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(home, '.pgc'))
            with open(os.path.join(home, '.pgc', 'ntfy'), 'w', encoding='utf-8') as topic_file:
                topic_file.write('global-topic\n')
            self.assertEqual(pgc.load_ntfy_topic(cwd, home), 'global-topic')

    def test_local_ntfy_topic_overrides_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(cwd, '.pgc'))
            os.makedirs(os.path.join(home, '.pgc'))
            with open(os.path.join(home, '.pgc', 'ntfy'), 'w', encoding='utf-8') as topic_file:
                topic_file.write('global-topic\n')
            with open(os.path.join(cwd, '.pgc', 'ntfy'), 'w', encoding='utf-8') as topic_file:
                topic_file.write('local-topic\n')
            self.assertEqual(pgc.load_ntfy_topic(cwd, home), 'local-topic')

    def test_empty_local_ntfy_topic_disables_home_config(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
            os.makedirs(os.path.join(cwd, '.pgc'))
            os.makedirs(os.path.join(home, '.pgc'))
            with open(os.path.join(home, '.pgc', 'ntfy'), 'w', encoding='utf-8') as topic_file:
                topic_file.write('global-topic\n')
            with open(os.path.join(cwd, '.pgc', 'ntfy'), 'w', encoding='utf-8') as topic_file:
                topic_file.write('\n')
            self.assertEqual(pgc.load_ntfy_topic(cwd, home), '')

    def test_missing_ntfy_topic_disables_notifications(self) -> None:
        with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as home:
            self.assertEqual(pgc.load_ntfy_topic(cwd, home), '')

    def test_terminal_response_summary_uses_first_sentence_of_final_paragraph(self) -> None:
        text = (
            'Implemented the requested change.\n\n'
            'Tests are all passing. The branch is clean and ready.'
        )
        self.assertEqual(pgc.terminal_response_summary(text), 'Tests are all passing.')

    def test_terminal_response_summary_truncates_sentence(self) -> None:
        text = 'Earlier paragraph.\n\n' + ('x' * 200) + '.'
        summary = pgc.terminal_response_summary(text, max_chars=40)
        self.assertEqual(len(summary), 40)
        self.assertTrue(summary.endswith('…'))

    def test_publish_ntfy_completion_posts_short_message_to_topic(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        with mock.patch.object(pgc.urllib.request, 'urlopen', return_value=response) as urlopen:
            pgc.publish_ntfy_completion(
                topic='poor girls/codex',
                title='Task complete (poor-girls-codex)',
                message='Finished.',
                server='https://notify.example/base/',
            )

        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, 'https://notify.example/base/poor%20girls%2Fcodex')
        self.assertEqual(request.data, b'Finished.')
        self.assertEqual(request.method, 'POST')
        self.assertEqual(request.headers['Title'], 'Task complete (poor-girls-codex)')
        urlopen.assert_called_once_with(request, timeout=pgc.NTFY_TIMEOUT_SECONDS)
        response.read.assert_called_once_with()

    def test_publish_ntfy_completion_empty_topic_is_disabled(self) -> None:
        with mock.patch.object(pgc.urllib.request, 'urlopen') as urlopen:
            pgc.publish_ntfy_completion(
                topic='  ',
                title='Task complete (repo)',
                message='Finished.',
            )
        urlopen.assert_not_called()

    def test_web_session_validation_accepts_current_session_and_descendants(self) -> None:
        pgc.validate_web_session_request({'session': 'abc', 'id': 'x', 'tool': 'status'}, 'abc')
        pgc.validate_web_session_request({'session': 'abc::child', 'id': 'x', 'tool': 'status'}, 'abc')
        pgc.validate_web_session_request({'session': 'abc::child::grandchild', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'session': 'other', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'session': 'abcd', 'id': 'x', 'tool': 'status'}, 'abc')
        with self.assertRaisesRegex(ValueError, 'different PGC session'):
            pgc.validate_web_session_request({'id': 'x', 'tool': 'status'}, 'abc')

    def test_subagent_web_bootstrap_describes_role_and_session_tree_routing(self) -> None:
        prompt = pgc.subagent_web_bootstrap_prompt('abc123')
        self.assertIn('You are a subagent.', prompt)
        self.assertIn('"session": "abc123"', prompt)
        self.assertIn('abc123::', prompt)
        self.assertIn('subagent {name,prompt}', prompt)
        self.assertIn('finish normally with a concise prose response', prompt)
        self.assertIn('forward that terminal response to your parent automatically', prompt)
        self.assertNotIn('handoff {result}', prompt)
        self.assertIn('Requests outside that session tree are inert', prompt)
        self.assertNotIn('subagent {name,prompt}', pgc.BOOTSTRAP_PROMPT)

    def test_root_web_bootstrap_includes_working_directory_and_omits_handoff(self) -> None:
        prompt = pgc.root_web_bootstrap_prompt('abc123', '/tmp/projects/asgard')
        self.assertTrue(
            prompt.startswith(
                'You are working on the `asgard` codebase.\n'
                'Working directory: asgard\n\n'
            )
        )
        self.assertIn('You are the root agent', prompt)
        self.assertIn('Finish normally in this conversation', prompt)
        self.assertIn('subagent {name,prompt}', prompt)
        self.assertNotIn('handoff', prompt.lower())
        self.assertIn('\"session\": \"abc123\"', prompt)
        self.assertNotIn('Working directory:', pgc.subagent_web_bootstrap_prompt('abc123'))

    def test_web_bootstrap_requires_explicit_known_role(self) -> None:
        with self.assertRaises(TypeError):
            pgc.web_bootstrap_prompt('abc123')
        with self.assertRaisesRegex(ValueError, 'unsupported web bootstrap role'):
            pgc.web_bootstrap_prompt('abc123', role='manager')

    def test_root_web_bootstrap_handles_filesystem_root(self) -> None:
        prompt = pgc.root_web_bootstrap_prompt('abc123', '/')
        self.assertTrue(
            prompt.startswith(
                'You are working on the `/` codebase.\n'
                'Working directory: /\n\n'
            )
        )

    def test_bootstrap_prompt_discourages_redundant_cwd_overrides(self) -> None:
        self.assertIn(
            'Local tools already execute from the current working directory.',
            pgc.BOOTSTRAP_PROMPT,
        )
        self.assertIn(
            'Omit `cwd` unless you intentionally need to operate in a different directory.',
            pgc.BOOTSTRAP_PROMPT,
        )
        self.assertIn('cwd? (only to change directories)', pgc.BOOTSTRAP_PROMPT)

    def test_bootstrap_prompt_uses_timeout_seconds(self) -> None:
        self.assertIn('timeout_seconds?', pgc.BOOTSTRAP_PROMPT)
        self.assertNotIn('timeout?', pgc.BOOTSTRAP_PROMPT)

    def test_timeout_seconds_takes_precedence_over_legacy_timeout(self) -> None:
        self.assertEqual(
            toolcall_lib.timeout_seconds({'timeout_seconds': 7, 'timeout': 99}),
            7,
        )

    def test_legacy_timeout_remains_supported(self) -> None:
        self.assertEqual(toolcall_lib.timeout_seconds({'timeout': 11}), 11)
        self.assertEqual(toolcall_lib.timeout_seconds({}), toolcall_lib.DEFAULT_TIMEOUT)

    def test_patch_tool_is_not_supported(self) -> None:
        self.assertNotIn('patch', pgc.LOCAL_TOOLS)
        with self.assertRaisesRegex(ValueError, 'unsupported tool'):
            pgc.validate_request(
                {'tool': 'patch', 'patch': 'not used'},
                supported_tools=pgc.SUPPORTED_TOOLS,
            )

    def test_hyphenated_subagent_name_is_valid(self) -> None:
        pgc.validate_request(
            {'tool': 'subagent', 'name': 'closure-parity', 'prompt': 'Review closures'},
            supported_tools=pgc.SUPPORTED_TOOLS,
        )

    def test_subagent_and_handoff_validation(self) -> None:
        pgc.validate_web_session_request(
            {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Do work'},
            'abc',
        )
        pgc.validate_web_session_request(
            {'session': 'abc', 'id': 'h', 'tool': 'handoff', 'result': 'Done'},
            'abc',
        )
        with self.assertRaisesRegex(ValueError, 'unsupported tool'):
            pgc.validate_request({'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Do work'})
        with self.assertRaisesRegex(ValueError, 'contain only alphanumerics'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker two!', 'prompt': 'Do work'},
                'abc',
            )
        with self.assertRaisesRegex(ValueError, 'non-empty'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': ''},
                'abc',
            )
        with self.assertRaisesRegex(ValueError, 'non-empty'):
            pgc.validate_web_session_request(
                {'session': 'abc', 'id': 'h', 'tool': 'handoff', 'result': ''},
                'abc',
            )

    def test_main_export_conversation_is_read_only_cli_path(self) -> None:
        import chatgpt_web

        with (
            mock.patch.object(
                sys,
                'argv',
                [
                    'poor_girls_codex.py',
                    'export-conversation',
                    'https://chatgpt.com/c/abc',
                    '--output',
                    '/tmp/export.jsonl',
                ],
            ),
            mock.patch.object(
                chatgpt_web,
                'export_conversation',
                return_value={
                    'conversation_id': 'abc',
                    'output_path': '/tmp/export.jsonl',
                    'format': 'jsonl',
                    'message_count': 3,
                },
            ) as export,
            mock.patch.object(chatgpt_web, 'run_web_watcher') as run_watcher,
            mock.patch('builtins.print') as printed,
        ):
            pgc.main()

        export.assert_called_once_with(
            'https://chatgpt.com/c/abc',
            '/tmp/export.jsonl',
            cdp_url=chatgpt_web.DEFAULT_CDP_URL,
            format='jsonl',
        )
        run_watcher.assert_not_called()
        self.assertIn('"message_count": 3', printed.call_args.args[0])

    def test_main_uses_open_root_without_showing_saved_conversation_picker(self) -> None:
        import chatgpt_web

        with (
            mock.patch.object(sys, 'argv', ['poor_girls_codex.py']),
            mock.patch.object(pgc, 'load_or_create_session_id', return_value='root'),
            mock.patch.object(pgc, 'load_ntfy_topic', return_value=''),
            mock.patch.object(pgc, 'load_conversation_history') as load_history,
            mock.patch.object(pgc, 'choose_saved_conversation') as choose_saved,
            mock.patch.object(pgc, 'show_startup_ui'),
            mock.patch.object(chatgpt_web, 'focus_open_root_conversation', return_value='open-chat') as focus_open,
            mock.patch.object(chatgpt_web, 'run_web_watcher') as run_watcher,
        ):
            pgc.main()

        focus_open.assert_called_once()
        load_history.assert_not_called()
        choose_saved.assert_not_called()
        self.assertEqual(run_watcher.call_args.kwargs['root_conversation_id'], 'open-chat')

    def test_main_shows_saved_conversation_picker_when_no_root_tab_is_open(self) -> None:
        import chatgpt_web

        selected = {'conversation_id': 'saved-chat', 'title': 'Saved chat', 'last_seen': 1.0}
        with (
            mock.patch.object(sys, 'argv', ['poor_girls_codex.py']),
            mock.patch.object(pgc, 'load_or_create_session_id', return_value='root'),
            mock.patch.object(pgc, 'load_ntfy_topic', return_value=''),
            mock.patch.object(pgc, 'load_conversation_history', return_value=[selected]) as load_history,
            mock.patch.object(pgc, 'choose_saved_conversation', return_value='saved-chat') as choose_saved,
            mock.patch.object(pgc, 'show_startup_ui'),
            mock.patch.object(pgc, 'remember_conversation') as remember,
            mock.patch.object(chatgpt_web, 'focus_open_root_conversation', return_value=None),
            mock.patch.object(chatgpt_web, 'run_web_watcher') as run_watcher,
        ):
            pgc.main()

            load_history.assert_called_once_with()
            choose_saved.assert_called_once_with([selected])
            kwargs = run_watcher.call_args.kwargs
            self.assertEqual(kwargs['root_conversation_id'], 'saved-chat')
            callback = kwargs['on_session_attached']
            session = mock.Mock(conversation_id='saved-chat', label='Saved chat')
            callback(session)
            remember.assert_called_once_with('saved-chat', 'Saved chat', cwd='.')

    def test_main_terminal_browser_selects_experimental_backend(self) -> None:
        import terminal_browser

        with (
            mock.patch.object(
                sys,
                'argv',
                ['poor_girls_codex.py', '--interface', 'terminal-browser'],
            ),
            mock.patch.object(pgc, 'load_or_create_session_id', return_value='root'),
            mock.patch.object(pgc, 'load_ntfy_topic', return_value=''),
            mock.patch.object(pgc, 'load_conversation_history', return_value=[]),
            mock.patch.object(pgc, 'choose_saved_conversation', return_value=None),
            mock.patch.object(pgc, 'show_startup_ui') as startup,
            mock.patch.object(
                terminal_browser,
                'focus_open_root_conversation',
                return_value='terminal-chat',
            ) as focus_open,
            mock.patch.object(terminal_browser, 'run_terminal_browser_watcher') as run_watcher,
        ):
            pgc.main()

        focus_open.assert_called_once()
        self.assertEqual(startup.call_args.kwargs['web_backend_label'], 'terminal-browser instance')
        self.assertEqual(run_watcher.call_args.kwargs['root_conversation_id'], 'terminal-chat')
        self.assertEqual(run_watcher.call_args.kwargs['session_id'], 'root')

    def test_terminal_browser_non_watch_mode_is_rejected(self) -> None:
        with mock.patch.object(
            sys,
            'argv',
            ['poor_girls_codex.py', 'copy', '--interface', 'terminal-browser'],
        ):
            with self.assertRaisesRegex(SystemExit, 'terminal-browser currently supports watch mode only'):
                pgc.main()

    def test_web_execution_strips_session_from_single_call(self) -> None:
        with mock.patch.object(pgc.toolcall_lib, 'execute', return_value={'ok': True}) as execute:
            result = pgc.execute_web_session_request(
                {'session': 'abc', 'id': 'x', 'tool': 'status'},
                'abc',
            )
        self.assertEqual(result, {'ok': True})
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, 0)

    def test_web_execution_strips_session_from_batch_wrapper(self) -> None:
        request = {
            'session': 'abc',
            'calls': [
                {'id': 'x', 'tool': 'status'},
                {'id': 'y', 'tool': 'tree'},
            ],
        }
        with mock.patch.object(
            pgc.toolcall_lib,
            'execute',
            side_effect=[{'ok': True}, {'ok': True}],
        ) as execute:
            pgc.execute_web_session_request(request, 'abc')
        self.assertEqual(
            execute.call_args_list,
            [
                mock.call({'id': 'x', 'tool': 'status'}, 0),
                mock.call({'id': 'y', 'tool': 'tree'}, 1),
            ],
        )


if __name__ == '__main__':
    unittest.main()
