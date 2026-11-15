from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import acp_server as acp
from chatgpt_web import AssistantSnapshot, WebSession, WebSessionWatcher


class AcpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.output = io.StringIO()
        self.server = acp.AcpServer(writer=self.output)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def messages(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def initialize(self):
        self.server.handle({
            'jsonrpc': '2.0', 'id': 0, 'method': 'initialize',
            'params': {'protocolVersion': 2, 'capabilities': {}, 'info': {
                'name': 't3-code', 'version': '1.0.0',
            }},
        })

    def new_session(self):
        self.initialize()
        self.server.handle({
            'jsonrpc': '2.0', 'id': 1, 'method': 'session/new',
            'params': {'cwd': self.directory.name, 'mcpServers': []},
        })
        return self.messages()[-1]['result']['sessionId']

    def test_negotiates_v1_from_v2_and_allocates_without_browser(self):
        session_id = self.new_session()
        messages = self.messages()
        self.assertEqual(messages[0]['result']['protocolVersion'], 1)
        self.assertEqual(messages[0]['result']['agentCapabilities'], {'sessionCapabilities': {'close': {}}})
        self.assertEqual(messages[1]['result']['sessionId'], session_id)
        self.assertEqual(self.server.sessions[session_id].cwd, str(Path(self.directory.name).resolve()))
        self.assertIsNone(self.server.worker)

    def test_advertises_thinking_effort_as_t3_reasoning_control(self):
        self.new_session()
        self.assertEqual(self.messages()[-1]['result']['configOptions'], [{
            'id': 'thinking_effort', 'name': 'Thinking Effort',
            'category': 'thought_level', 'type': 'select',
            'description': 'ChatGPT thinking effort. Changes apply to the next browser submission.',
            'currentValue': 'high',
            'options': [
                {'value': 'instant', 'name': 'Instant'},
                {'value': 'medium', 'name': 'Medium'},
                {'value': 'high', 'name': 'High'},
            ],
        }])

    def test_config_changes_are_session_local_and_work_during_a_turn(self):
        session_id = self.new_session()
        other_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(10, 'Working')
        for effort in ('instant', 'high', 'medium'):
            with self.subTest(effort=effort):
                self.server.handle({
                    'jsonrpc': '2.0', 'id': 2, 'method': 'session/set_config_option',
                    'params': {'sessionId': session_id, 'configId': 'thinking_effort', 'value': effort},
                })
                self.assertEqual(self.messages()[-1]['result']['configOptions'], session.config_options())
                self.assertEqual(session.thinking_effort, effort)
                self.assertEqual(self.server.sessions[other_id].thinking_effort, 'high')
        self.assertIsNone(self.server.worker)
        self.assertTrue(self.server.jobs.empty())

    def test_invalid_config_does_not_change_effort(self):
        session_id = self.new_session()
        for overrides in (
            {'sessionId': 'unknown'}, {'configId': 'unknown'},
            {'value': 'low'}, {'value': True}, {'value': []}, {'value': None},
        ):
            with self.subTest(overrides=overrides):
                self.server.handle({
                    'jsonrpc': '2.0', 'id': 2, 'method': 'session/set_config_option',
                    'params': {'sessionId': session_id, 'configId': 'thinking_effort',
                               'value': 'high', **overrides},
                })
                self.assertEqual(self.messages()[-1]['error']['code'], -32602)
                self.assertEqual(self.server.sessions[session_id].thinking_effort, 'high')

    def test_closing_session_rejects_effort_changes(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.closing = True
        self.server.handle({
            'jsonrpc': '2.0', 'id': 2, 'method': 'session/set_config_option',
            'params': {'sessionId': session_id, 'configId': 'thinking_effort', 'value': 'high'},
        })
        self.assertEqual(self.messages()[-1]['error']['code'], -32000)
        self.assertEqual(session.thinking_effort, 'high')

    def test_sync_effort_updates_owned_tabs_and_followup_submission(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        root, child = self.fake_web('root'), self.fake_web('child')
        session.web = root
        session.watcher = mock.Mock()
        session.watcher.sessions = {'root': root, 'child': child}
        session.thinking_effort = 'instant'
        session.turn = acp.Turn(10, 'Follow up')
        interface = mock.Mock()
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = None
        session.start(interface)
        self.assertEqual(root.thinking_effort, 'instant')
        self.assertEqual(child.thinking_effort, 'instant')
        interface.submit.assert_called_once_with(root, 'Follow up')

    def test_strip_pull_request_linking_preserves_unrelated_text(self):
        prompt = (
            'Please change the literal `<pull_request_linking>` label.\n'
            '<runtime_info>Keep this metadata.</runtime_info>\n'
            '<pull_request_linking>\n'
            'T3-only instruction text\n'
            '</pull_request_linking>\n'
            'Continue with the actual task.\n'
            '\\<pull_request_linking>\r\n'
            'More T3-only instructions\r\n'
            '\\</pull_request_linking>\r\n'
            'End.\n'
        )
        self.assertEqual(acp.strip_pull_request_linking(prompt), (
            'Please change the literal `<pull_request_linking>` label.\n'
            '<runtime_info>Keep this metadata.</runtime_info>\n'
            'Continue with the actual task.\n'
            'End.\n'
        ))
        unclosed = 'Before\n<pull_request_linking>\nNever closed\nAfter'
        self.assertEqual(acp.strip_pull_request_linking(unclosed), unclosed)
        inline = 'An inline <pull_request_linking> tag is not a block.'
        self.assertEqual(acp.strip_pull_request_linking(inline), inline)
        nested_unclosed = (
            'A\n<pull_request_linking>\nUnclosed\n'
            '<pull_request_linking>\nComplete\n</pull_request_linking>\nB\n'
        )
        self.assertEqual(acp.strip_pull_request_linking(nested_unclosed), (
            'A\n<pull_request_linking>\nUnclosed\nB\n'
        ))

    def test_strips_t3_instructions_on_first_and_followup_submissions(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        prompt = 'Implement feature.\n<pull_request_linking>\nT3 wiring\n</pull_request_linking>\nDone.'
        filtered = 'Implement feature.\nDone.'
        session.turn = acp.Turn(9, prompt)
        interface = mock.Mock()
        interface.create_root_conversation.return_value = WebSession('root', 'Root', mock.Mock())
        with mock.patch.object(acp, 'WebSessionWatcher'):
            session.start(interface)
        self.assertEqual(
            interface.create_root_conversation.call_args.args[0], session.bootstrap(filtered),
        )
        self.assertEqual(session.turn.prompt, prompt)
        session.turn = acp.Turn(10, prompt)
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = None
        session.start(interface)
        interface.submit.assert_called_once_with(session.web, filtered)
        self.assertEqual(session.turn.prompt, prompt)

    def test_initialize_required_and_invalid_cwd_rejected(self):
        self.server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'session/new', 'params': {}})
        self.assertEqual(self.messages()[-1]['error']['code'], -32600)
        self.initialize()
        self.server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'session/new',
                            'params': {'cwd': 'relative/path', 'mcpServers': []}})
        self.assertEqual(self.messages()[-1]['error']['code'], -32602)

    def test_prompt_is_long_running_and_final_text_precedes_response(self):
        session_id = self.new_session()
        with mock.patch.object(self.server, 'ensure_worker'):
            self.server.handle({
                'jsonrpc': '2.0', 'id': 2, 'method': 'session/prompt',
                'params': {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': 'Hello'}]},
            })
        session = self.server.sessions[session_id]
        self.assertEqual(session.turn.prompt, 'Hello')
        self.assertEqual(self.server.jobs.get_nowait(), ('prompt', session_id))
        self.assertEqual(len(self.messages()), 2)
        session.complete('Hello from ChatGPT')
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'Hello from ChatGPT')
        self.assertEqual(self.messages()[-1], {
            'jsonrpc': '2.0', 'id': 2, 'result': {'stopReason': 'end_turn'},
        })
        self.assertIsNone(session.turn)

    def test_rejects_unsupported_prompt_content_without_starting_turn(self):
        session_id = self.new_session()
        self.server.handle({
            'jsonrpc': '2.0', 'id': 2, 'method': 'session/prompt',
            'params': {'sessionId': session_id, 'prompt': [{'type': 'image', 'data': 'abc'}]},
        })
        self.assertEqual(self.messages()[-1]['error']['code'], -32602)
        self.assertIsNone(self.server.sessions[session_id].turn)
        self.assertIsNone(self.server.worker)

    def test_first_turn_startup_observes_cancel_close_and_shutdown(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(2, 'Start')
        interface = mock.Mock()
        interface.create_root_conversation.return_value = WebSession('root-chat', 'Root', mock.Mock())
        with mock.patch.object(acp, 'WebSessionWatcher'):
            session.start(interface)
        self.assertEqual(interface.create_root_conversation.call_args.kwargs['thinking_effort'], 'high')
        callback = interface.create_root_conversation.call_args.kwargs['should_abort']
        self.assertFalse(callback())
        session.turn.cancelled = True
        self.assertTrue(callback())
        session.turn.cancelled = False
        session.closing = True
        self.assertTrue(callback())
        session.closing = False
        self.server.stopping.set()
        self.assertTrue(callback())

    def test_cancellation_reports_cancelled_and_does_not_report_late_completion(self):
        session_id = self.new_session()
        with mock.patch.object(self.server, 'ensure_worker'):
            self.server.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'session/prompt',
                                'params': {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': 'Wait'}]}})
        self.server.handle({'jsonrpc': '2.0', 'method': 'session/cancel',
                            'params': {'sessionId': session_id}})
        session = self.server.sessions[session_id]
        session.cancel(None)
        session.complete('Late response')
        self.assertEqual(self.messages()[-1]['result'], {'stopReason': 'cancelled'})
        self.assertFalse(any(m.get('method') == 'session/update' for m in self.messages()))

    def test_tool_activity_emits_matching_call_and_completion(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        request = {'session': session.routing_id, 'id': 'inspect', 'tool': 'status'}
        with mock.patch.object(acp.pgc, 'execute_web_session_request', return_value={
            'id': 'inspect', 'tool': 'status', 'ok': True,
        }) as executor:
            result = session.tool_request(request, announce=True)
        self.assertTrue(result['ok'])
        executor.assert_called_once()
        events = [m['params']['update'] for m in self.messages() if m.get('method') == 'session/update']
        self.assertEqual([e['sessionUpdate'] for e in events], ['tool_call', 'tool_call_update'])
        self.assertEqual(events[0]['toolCallId'], events[1]['toolCallId'])
        self.assertEqual(events[1]['status'], 'completed')

    def test_browser_completion_detects_followup_without_tool_call(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(9, 'Next question', previous_user_id='old-user')
        session.web = WebSession('chat-1', 'Project', mock.Mock())
        interface = mock.Mock()
        interface.latest_user_message.return_value = ('new-user', 'Next question')
        interface.is_generating.return_value = False
        interface.latest_assistant_snapshot.return_value = AssistantSnapshot('answer-1', (), text='Done')
        interface.valid_request_from_snapshot.return_value = None
        interface.terminal_response_from_snapshot.return_value = ('Done', 'fingerprint')
        with mock.patch.object(acp.time, 'monotonic', side_effect=[100.0, 100.1, 100.4]):
            session.check_completion(interface)
            self.assertIsNotNone(session.turn)
            session.check_completion(interface)
            self.assertIsNotNone(session.turn)
            session.check_completion(interface)
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'Done')
        self.assertEqual(self.messages()[-1]['result']['stopReason'], 'end_turn')

    def test_acp_completion_tracks_confirmed_tool_result_user_turn(self):
        from chatgpt_web import TerminalExpectation

        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(9, 'Question', previous_user_id='older', user_id='prompt-user')
        session.web = WebSession('chat-1', 'Project', mock.Mock())
        session.web.terminal_expectation = TerminalExpectation('```\nRESULT\n```', 'prompt-user')
        interface = mock.Mock()
        interface.latest_user_message.return_value = ('result-user', 'RESULT\n…\nShow more')
        interface.latest_user_matches_submission.return_value = True
        interface.is_generating.side_effect = [True, False, False]
        interface.latest_assistant_snapshot.return_value = AssistantSnapshot('answer-1', (), 'Done')
        interface.valid_request_from_snapshot.return_value = None
        interface.terminal_response_from_snapshot.return_value = ('Done', 'terminal-fingerprint')

        session.check_completion(interface)
        self.assertEqual(session.turn.user_id, 'result-user')
        self.assertIsNone(session.turn.completion_candidate)
        with mock.patch.object(acp.time, 'monotonic', side_effect=[10.0, 10.4]):
            session.check_completion(interface)
            self.assertIsNotNone(session.turn)
            session.check_completion(interface)
        self.assertIsNone(session.turn)
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'Done')
        self.assertEqual(self.messages()[-1]['result'], {'stopReason': 'end_turn'})
        interface.terminal_response_from_snapshot.assert_called_with(
            session.web, interface.latest_assistant_snapshot.return_value, 'result-user',
        )

    def test_acp_does_not_trust_unmatched_new_user_turn(self):
        from chatgpt_web import TerminalExpectation

        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(9, 'Question', previous_user_id='older', user_id='prompt-user')
        session.web = WebSession('chat-1', 'Project', mock.Mock())
        session.web.terminal_expectation = TerminalExpectation('```\nRESULT\n```', 'prompt-user')
        interface = mock.Mock()
        interface.latest_user_message.return_value = ('foreign-user', 'Unrelated human message')
        interface.latest_user_matches_submission.return_value = False
        interface.is_generating.return_value = False
        interface.latest_assistant_snapshot.return_value = AssistantSnapshot('answer-1', (), 'Not our response')
        interface.valid_request_from_snapshot.return_value = None
        interface.terminal_response_from_snapshot.return_value = None
        session.check_completion(interface)
        self.assertEqual(session.turn.user_id, 'prompt-user')
        self.assertIsNone(session.turn.completion_candidate)
        self.assertFalse(any(message.get('result', {}).get('stopReason') == 'end_turn'
                             for message in self.messages()))

    def test_complete_never_acknowledges_empty_response(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(9, 'Question')
        for text in ('', '  \n  ', '\t'):
            session.complete(text)
        self.assertEqual(len(self.messages()), 2)
        self.assertIsNotNone(session.turn)
        session.complete('Actual answer')
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'Actual answer')
        self.assertEqual(self.messages()[-1]['result'], {'stopReason': 'end_turn'})

    def test_followup_waits_for_nonempty_stable_response(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        session.turn = acp.Turn(9, 'Question', previous_user_id='old-user')
        session.web = WebSession('chat-1', 'Project', mock.Mock())
        interface = mock.Mock()
        interface.latest_user_message.return_value = ('new-user', 'Question')
        interface.is_generating.return_value = False
        interface.valid_request_from_snapshot.return_value = None
        interface.latest_assistant_snapshot.side_effect = [
            AssistantSnapshot('answer-1', (), text=''),
            AssistantSnapshot('answer-1', (), text='A partial'),
            AssistantSnapshot('answer-1', (), text='A complete answer'),
            AssistantSnapshot('answer-1', (), text='A complete answer'),
            AssistantSnapshot('answer-1', (), text='A complete answer'),
        ]
        interface.terminal_response_from_snapshot.side_effect = [
            None, ('A partial', 'partial-fp'), ('A complete answer', 'complete-fp'),
            ('A complete answer', 'complete-fp'), ('A complete answer', 'complete-fp'),
        ]
        with mock.patch.object(acp.time, 'monotonic', side_effect=[1.0, 1.1, 1.2, 1.5]):
            for _ in range(4):
                session.check_completion(interface)
                self.assertIsNotNone(session.turn)
            session.check_completion(interface)
        self.assertIsNone(session.turn)
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'A complete answer')
        self.assertEqual(self.messages()[-1]['result'], {'stopReason': 'end_turn'})

    def test_workspace_is_restored_even_on_error(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        original = os.getcwd()
        def fail():
            self.assertEqual(os.getcwd(), session.cwd)
            raise ValueError('expected')
        with self.assertRaisesRegex(ValueError, 'expected'):
            self.server.in_workspace(session, fail)
        self.assertEqual(os.getcwd(), original)

    def test_managed_watcher_ignores_other_browser_tabs(self):
        interface = mock.Mock()
        own = mock.Mock(url='https://chatgpt.com/c/ours')
        other = mock.Mock(url='https://chatgpt.com/c/theirs')
        interface.pages.return_value = [own, other]
        watcher = WebSessionWatcher(interface, validate_request=mock.Mock(),
                                    execute_request=mock.Mock(), fenced_result=mock.Mock(),
                                    too_long_fallback=mock.Mock(), managed_only=True)
        watcher.sessions['ours'] = WebSession('ours', 'Our chat', own)
        watcher.refresh_sessions()
        self.assertEqual(list(watcher.sessions), ['ours'])
        self.assertNotIn('theirs', watcher.sessions)
        interface.instrument_conversations_responses.assert_called_once_with(own)

    @staticmethod
    def fake_web(conversation_id):
        page = mock.Mock()
        page.url = f'https://chatgpt.com/c/{conversation_id}'
        page.is_closed.return_value = False
        return WebSession(conversation_id, conversation_id, page)

    def test_close_before_first_prompt_needs_no_browser_and_is_idempotent(self):
        session_id = self.new_session()
        for request_id in (2, 3):
            self.server.handle({'jsonrpc': '2.0', 'id': request_id, 'method': 'session/close',
                                'params': {'sessionId': session_id}})
            self.assertEqual(self.messages()[-1]['result'], {})
        self.assertNotIn(session_id, self.server.sessions)
        self.assertIsNone(self.server.worker)

    def test_cleanup_closes_only_owned_conversation_pages_and_subagents(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        root = self.fake_web('root-chat')
        child = self.fake_web('child-chat')
        repurposed = self.fake_web('old-chat')
        repurposed.page.url = 'https://example.com/a-user-page'
        unrelated = self.fake_web('unrelated-chat')
        cdp = mock.Mock()
        root.cdp_session = cdp
        session.web = root
        session.watcher = mock.Mock()
        session.watcher.sessions = {
            'root-chat': root, 'child-chat': child, 'old-chat': repurposed,
        }
        session.watcher.sessions_by_routing_id = {'root': root}
        session.close_owned_tabs()
        root.page.close.assert_called_once_with()
        cdp.detach.assert_called_once_with()
        child.page.close.assert_called_once_with()
        repurposed.page.close.assert_not_called()
        unrelated.page.close.assert_not_called()
        self.assertEqual(session.watcher, None)
        self.assertIsNone(session.web)

    def test_failed_tab_close_keeps_ownership_for_retry(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        root = self.fake_web('retry-chat')
        root.page.close.side_effect = RuntimeError('temporary CDP error')
        session.web = root
        with self.assertRaisesRegex(RuntimeError, 'temporary CDP error'):
            session.close_owned_tabs()
        self.assertIs(session.web, root)
        root.page.close.side_effect = None
        session.close_owned_tabs()
        self.assertEqual(root.page.close.call_count, 2)
        self.assertIsNone(session.web)

    def test_close_active_turn_cancels_then_closes_and_rejects_new_prompt(self):
        session_id = self.new_session()
        session = self.server.sessions[session_id]
        root = self.fake_web('active-chat')
        session.web = root
        session.turn = acp.Turn(30, 'Still working')
        with mock.patch.object(self.server, 'ensure_worker'):
            self.server.handle({'jsonrpc': '2.0', 'id': 31, 'method': 'session/close',
                                'params': {'sessionId': session_id}})
            self.server.handle({'jsonrpc': '2.0', 'id': 32, 'method': 'session/close',
                                'params': {'sessionId': session_id}})
        self.assertEqual(self.server.jobs.get_nowait(), ('close', session_id))
        self.assertTrue(session.closing)
        self.assertTrue(session.turn.cancelled)
        self.server.handle({'jsonrpc': '2.0', 'id': 33, 'method': 'session/prompt',
                            'params': {'sessionId': session_id, 'prompt': [{'type': 'text', 'text': 'No'}]}})
        self.assertEqual(self.messages()[-1]['error']['code'], -32000)
        self.server.finish_close(session, None)
        root.page.close.assert_called_once_with()
        self.assertNotIn(session_id, self.server.sessions)
        by_id = {m['id']: m for m in self.messages() if 'id' in m}
        self.assertEqual(by_id[30]['result'], {'stopReason': 'cancelled'})
        self.assertEqual(by_id[31]['result'], {})
        self.assertEqual(by_id[32]['result'], {})

    def test_real_entrypoint_emits_only_jsonrpc_on_stdout(self):
        script = Path(acp.__file__).with_name('poor_girls_codex.py')
        lines = [
            {'jsonrpc': '2.0', 'id': 10, 'method': 'initialize',
             'params': {'protocolVersion': 2, 'clientCapabilities': {}}},
            {'jsonrpc': '2.0', 'id': 11, 'method': 'session/new',
             'params': {'cwd': str(Path(self.directory.name).resolve()), 'mcpServers': []}},
        ]
        proc = subprocess.run([sys.executable, str(script), 'acp'],
                              input='\n'.join(map(json.dumps, lines)) + '\n',
                              text=True, capture_output=True, timeout=15, check=True)
        result = [json.loads(line) for line in proc.stdout.splitlines()]
        self.assertEqual(len(result), 2)
        self.assertEqual([m['id'] for m in result], [10, 11])
        self.assertEqual(result[0]['result']['protocolVersion'], 1)
        self.assertIn('sessionId', result[1]['result'])
        self.assertEqual(result[1]['result']['configOptions'][0]['category'], 'thought_level')


if __name__ == '__main__':
    unittest.main()
