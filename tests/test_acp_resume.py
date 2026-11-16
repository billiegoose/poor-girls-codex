from __future__ import annotations

import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import acp_server as acp
from acp_session_store import AcpSessionStore
from chatgpt_web import ConversationProtocolState, WebSession


class AcpResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cwd = str(Path(self.temp.name).resolve())
        self.store = AcpSessionStore(Path(self.cwd) / 'store')
        self.server = self.make_server()

    def make_server(self) -> acp.AcpServer:
        server = acp.AcpServer(writer=io.StringIO(), session_store=self.store)
        server.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': 2}})
        return server

    @staticmethod
    def request(server: acp.AcpServer, method: str, params: dict, request_id: int = 2) -> dict:
        server.handle({'jsonrpc': '2.0', 'id': request_id, 'method': method, 'params': params})
        return json.loads(server.writer.getvalue().splitlines()[-1])

    def new_session(self) -> acp.AcpSession:
        response = self.request(self.server, 'session/new', {'cwd': self.cwd, 'mcpServers': []})
        return self.server.sessions[response['result']['sessionId']]

    def resume(self, server: acp.AcpServer, session_id: str, *, cwd: str | None = None) -> dict:
        return self.request(server, 'session/resume', {
            'sessionId': session_id, 'cwd': cwd or self.cwd, 'mcpServers': [],
        })

    def test_persistent_id_and_settings_survive_separate_servers(self) -> None:
        original = self.new_session()
        self.request(self.server, 'session/set_config_option', {
            'sessionId': original.session_id, 'configId': 'thinking_effort', 'value': 'medium',
        })
        record = self.store.load(original.session_id)
        self.assertEqual(record['routingId'], original.routing_id)
        self.assertEqual(record['thinkingEffort'], 'medium')
        new_server = self.make_server()
        result = self.resume(new_server, original.session_id)
        self.assertEqual(result['result']['configOptions'][0]['currentValue'], 'medium')
        resumed = new_server.sessions[original.session_id]
        self.assertEqual((resumed.session_id, resumed.routing_id, resumed.cwd),
                         (original.session_id, original.routing_id, original.cwd))
        self.assertIsNone(new_server.worker)
        self.assertIsNone(resumed.web)
        self.assertIsNone(resumed.turn)
        self.assertEqual(self.resume(new_server, original.session_id)['result'], result['result'])

    def test_reconnects_same_browser_conversation_and_submits_followup(self) -> None:
        original = self.new_session()
        original.turn = acp.Turn(12, 'First request')
        created = WebSession('original-chat-id', 'Original', mock.Mock())
        browser = mock.Mock()
        browser.create_root_conversation.return_value = created
        with mock.patch.object(acp, 'WebSessionWatcher') as watcher_factory:
            original.start(browser)
            watcher_factory.return_value.bind_routing_session.assert_called_once_with(
                created, original.routing_id,
            )
        self.assertEqual(self.store.load(original.session_id)['conversationId'], 'original-chat-id')
        next_server = self.make_server()
        self.assertIn('result', self.resume(next_server, original.session_id))
        recovered = next_server.sessions[original.session_id]
        recovered.turn = acp.Turn(13, 'Continue')
        reopened = WebSession('original-chat-id', 'Original', mock.Mock())
        browser2 = mock.Mock()
        browser2.open_conversation.return_value = reopened
        browser2.conversation_protocol_state.return_value = ConversationProtocolState(
            routing_session_id=original.routing_id,
            recent_request=('message-id', {'tool': 'status'}, 'prior-fingerprint'),
            request_pending=False, terminal_response=None,
        )
        browser2.is_generating.return_value = False
        browser2.latest_user_message.return_value = ('previous-user', 'Previous')
        with mock.patch.object(acp, 'WebSessionWatcher') as watcher_factory:
            recovered.start(browser2)
            watcher = watcher_factory.return_value
            watcher.bind_routing_session.assert_called_once_with(reopened, original.routing_id)
            self.assertEqual(watcher.note_forward_progress.call_count, 2)
        browser2.open_conversation.assert_called_once_with('original-chat-id')
        browser2.create_root_conversation.assert_not_called()
        browser2.submit.assert_called_once_with(reopened, 'Continue')
        self.assertEqual(recovered.turn.previous_user_id, 'previous-user')
        self.assertEqual(reopened.seen_fingerprints, {'prior-fingerprint'})

    def test_reconnect_failure_does_not_start_new_conversation_or_run_prompt(self) -> None:
        original = self.new_session()
        original.conversation_id = 'missing-chat-id'
        self.server.persist_session(original)
        fresh = self.make_server()
        self.resume(fresh, original.session_id)
        recovered = fresh.sessions[original.session_id]
        recovered.turn = acp.Turn(99, 'No silent reset')
        browser = mock.Mock()
        browser.open_conversation.side_effect = RuntimeError('ChatGPT conversation unavailable')
        with mock.patch.object(acp, 'WebSessionWatcher'):
            with self.assertRaisesRegex(RuntimeError, 'conversation unavailable'):
                recovered.start(browser)
        browser.create_root_conversation.assert_not_called()
        browser.submit.assert_not_called()
        self.assertIsNone(recovered.web)

    def test_rejects_unknown_or_foreign_workspace_without_allocation(self) -> None:
        original = self.new_session()
        fresh = self.make_server()
        for session_id in ('pgc-' + '0' * 24, '../../escape', '', 123):
            with self.subTest(session_id=session_id):
                answer = self.resume(fresh, session_id)
                self.assertEqual(answer['error']['code'], -32602)
        other = Path(self.cwd) / 'other-workspace'
        other.mkdir()
        response = self.resume(fresh, original.session_id, cwd=str(other))
        self.assertEqual(response['error']['code'], -32602)
        self.assertNotIn(original.session_id, fresh.sessions)
        self.assertIsNone(fresh.worker)

    def test_actual_stdio_process_restart_restores_session_and_settings(self) -> None:
        script = Path(acp.__file__).with_name('poor_girls_codex.py')
        env = {**os.environ, 'PGC_ACP_SESSION_DIR': str(self.store.directory)}

        def run_process(*requests: dict) -> list[dict]:
            process = subprocess.run(
                [sys.executable, str(script), 'acp'],
                input='\n'.join(json.dumps(request) for request in requests) + '\n',
                text=True, capture_output=True, timeout=15, env=env, check=True,
            )
            return [json.loads(line) for line in process.stdout.splitlines()]

        init = {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize',
                'params': {'protocolVersion': 2}}
        created = run_process(init, {'jsonrpc': '2.0', 'id': 2,
                                     'method': 'session/new',
                                     'params': {'cwd': self.cwd, 'mcpServers': []}})
        session_id = created[1]['result']['sessionId']
        self.assertEqual(created[0]['result']['agentCapabilities']['sessionCapabilities']['resume'], {})
        resume = {'jsonrpc': '2.0', 'id': 3, 'method': 'session/resume',
                  'params': {'sessionId': session_id, 'cwd': self.cwd, 'mcpServers': []}}
        restored = run_process(init, resume, {
            'jsonrpc': '2.0', 'id': 4, 'method': 'session/set_config_option',
            'params': {'sessionId': session_id, 'configId': 'thinking_effort', 'value': 'medium'},
        })
        self.assertIn('result', restored[1])
        self.assertEqual(restored[2]['result']['configOptions'][0]['currentValue'], 'medium')
        again = run_process(init, resume)
        self.assertEqual(again[1]['result']['configOptions'][0]['currentValue'], 'medium')

    def test_rejects_reopening_conversation_with_foreign_routing_id(self) -> None:
        original = self.new_session()
        original.conversation_id = 'old-chat-id'
        self.server.persist_session(original)
        fresh = self.make_server()
        self.resume(fresh, original.session_id)
        recovered = fresh.sessions[original.session_id]
        recovered.turn = acp.Turn(41, 'Next prompt')
        browser = mock.Mock()
        reopened = WebSession('old-chat-id', 'Old', mock.Mock())
        browser.open_conversation.return_value = reopened
        browser.conversation_protocol_state.return_value = ConversationProtocolState(
            routing_session_id='acp-' + 'f' * 24,
            recent_request=None, request_pending=False, terminal_response=None,
        )
        with mock.patch.object(acp, 'WebSessionWatcher'):
            with self.assertRaisesRegex(RuntimeError, 'different PGC routing session'):
                recovered.start(browser)
        reopened.page.close.assert_called_once_with()
        browser.submit.assert_not_called()
        self.assertIsNone(recovered.web)

    def test_undelivered_old_tool_call_is_not_marked_complete(self) -> None:
        original = self.new_session()
        original.conversation_id = 'pending-chat-id'
        self.server.persist_session(original)
        fresh = self.make_server()
        self.resume(fresh, original.session_id)
        recovered = fresh.sessions[original.session_id]
        recovered.turn = acp.Turn(42, 'Continue')
        browser = mock.Mock()
        reopened = WebSession('pending-chat-id', 'Pending', mock.Mock())
        browser.open_conversation.return_value = reopened
        browser.conversation_protocol_state.return_value = ConversationProtocolState(
            routing_session_id=original.routing_id,
            recent_request=('assistant-id', {'tool': 'status'}, 'pending-fingerprint'),
            request_pending=True, terminal_response=None,
        )
        browser.is_generating.return_value = False
        browser.latest_user_message.return_value = ('previous-user', 'Previous')
        with mock.patch.object(acp, 'WebSessionWatcher'):
            with self.assertRaisesRegex(RuntimeError, 'unresolved tool request'):
                recovered.start(browser)
        self.assertNotIn('pending-fingerprint', reopened.seen_fingerprints)
        reopened.page.close.assert_called_once_with()
        browser.submit.assert_not_called()

    def test_corrupt_persistent_record_is_not_resumed(self) -> None:
        original = self.new_session()
        self.store.path_for(original.session_id).write_text('{not-json', encoding='utf-8')
        fresh = self.make_server()
        response = self.resume(fresh, original.session_id)
        self.assertEqual(response['error']['code'], -32602)
        self.assertNotIn(original.session_id, fresh.sessions)

    def test_records_have_user_only_permissions(self) -> None:
        original = self.new_session()
        directory_mode = stat.S_IMODE(os.stat(self.store.directory).st_mode)
        file_mode = stat.S_IMODE(os.stat(self.store.path_for(original.session_id)).st_mode)
        self.assertEqual(directory_mode & 0o077, 0)
        self.assertEqual(file_mode & 0o077, 0)


if __name__ == '__main__':
    unittest.main()
