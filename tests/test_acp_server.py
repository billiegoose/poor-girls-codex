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
        session.check_completion(interface)
        self.assertEqual(self.messages()[-2]['params']['update']['content']['text'], 'Done')
        self.assertEqual(self.messages()[-1]['result']['stopReason'], 'end_turn')

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


if __name__ == '__main__':
    unittest.main()
