from __future__ import annotations

import io
import json
import unittest
from unittest import mock

import acp_server as acp
from acp_markdown import html_to_markdown
from acp_tool_display import display_name, display_title, display_input, display_output
from chatgpt_web import AssistantSnapshot, ChatGPTWeb, WebSession


class MarkdownTests(unittest.TestCase):
    def test_rich_prose_headings_links_and_code(self):
        source = (
            '<h2>Result</h2><p><strong>Passed</strong> with '
            '<a href="https://example.com/docs">docs</a> and <code>make test</code>.</p>'
            '<pre><code class="language-python">print("hi")\n</code></pre>'
        )
        self.assertEqual(html_to_markdown(source), (
            '## Result\n\n**Passed** with [docs](https://example.com/docs) '
            'and `make test`.\n\n```python\nprint("hi")\n```'
        ))

    def test_lists_tables_quotes_and_ui_exclusion(self):
        source = (
            '<ul><li>First</li><li>Second<ul><li>Nested</li></ul></li></ul>'
            '<blockquote><p>Important</p></blockquote>'
            '<table><thead><tr><th>A</th><th>B</th></tr></thead>'
            '<tbody><tr><td>1</td><td>2</td></tr></tbody></table>'
            '<button>Copy</button><script>malicious()</script>'
        )
        result = html_to_markdown(source)
        self.assertIn('- First\n- Second\n  - Nested', result)
        self.assertIn('> Important', result)
        self.assertIn('| A | B |\n| --- | --- |\n| 1 | 2 |', result)
        self.assertNotIn('Copy', result)
        self.assertNotIn('malicious', result)

    def test_code_fence_grows_for_embedded_backticks(self):
        self.assertEqual(html_to_markdown('<pre><code>```\nhello\n</code></pre>'),
                         '````\n```\nhello\n````')

    def test_extract_markdown_only_from_matching_assistant_node(self):
        interface = ChatGPTWeb()
        body = mock.Mock()
        body.inner_html.return_value = '<h3>Ready</h3><p>A <em>formatted</em> reply.</p>'
        blocks = mock.Mock()
        blocks.count.return_value = 1
        blocks.nth.return_value = body
        message = mock.Mock()
        message.get_attribute.side_effect = lambda key: 'answer-7' if key == 'data-message-id' else None
        message.locator.return_value = blocks
        messages = mock.Mock()
        messages.count.return_value = 1
        messages.nth.return_value = message
        session = WebSession('conversation', 'Test', mock.Mock())
        with mock.patch.object(interface, 'message_locator', return_value=messages):
            self.assertEqual(interface.markdown_from_assistant_snapshot(
                session, AssistantSnapshot('answer-7', (), 'Ready A formatted reply.')),
                '### Ready\n\nA *formatted* reply.')
            self.assertIsNone(interface.markdown_from_assistant_snapshot(
                session, AssistantSnapshot('stale-answer', (), 'Old reply')))


class ToolDisplayTests(unittest.TestCase):
    def test_command_names_and_titles(self):
        cases = [
            ({'tool': 'run', 'command': ['git', 'status', '--short']}, 'git', 'git status --short'),
            ({'tool': 'run', 'command': 'python -m unittest'}, 'python', 'python -m unittest'),
            ({'tool': 'run', 'script': 'echo hello\nexit 0'}, 'bash', 'bash: echo hello'),
        ]
        for call, name, title in cases:
            with self.subTest(call=call):
                self.assertEqual(display_name(call), name)
                self.assertEqual(display_title(call, 0), title)
        self.assertEqual(display_title({'tool': 'read', 'path': 'src/foo.py'}, 0), 'read: src/foo.py')

    def test_tool_output_exposes_real_stdout_stderr_and_exit(self):
        output = display_output('run', {
            'ok': True, 'exit_code': 7, 'stdout': 'a line\n',
            'stderr': 'oops\n', 'stderr_truncated': True,
        })
        for text in ('Exit code: 7', 'stdout:\na line', 'stderr:\noops', 'Harness output truncated'):
            self.assertIn(text, output)
        self.assertIn('No stdout or stderr', display_output('run', {
            'ok': True, 'exit_code': 0, 'stdout': '', 'stderr': '',
        }))
        self.assertIn('Error: failed', display_output('run', {'ok': False, 'error': 'failed'}))
        self.assertEqual(display_output('status', {'ok': True, 'status': '## feature/acp\n'}), '## feature/acp\n')
        self.assertNotIn('SECRET', str(display_input({
            'tool': 'run', 'script': 'echo yes', 'env': {'TOKEN': 'SECRET'},
        })))


class AcpPresentationTests(unittest.TestCase):
    def setUp(self):
        self.wire = io.StringIO()
        self.server = acp.AcpServer(writer=self.wire)
        self.session = acp.AcpSession(self.server, 'session-1', '.')
        self.server.sessions[self.session.session_id] = self.session

    def events(self):
        return [json.loads(line) for line in self.wire.getvalue().splitlines()]

    def test_run_emits_command_input_and_real_output(self):
        call = {'session': self.session.routing_id, 'tool': 'run',
                'id': 'test', 'script': 'printf hello'}
        result = {'tool': 'run', 'id': 'test', 'ok': True, 'exit_code': 0,
                  'stdout': 'hello', 'stderr': ''}
        with mock.patch.object(acp.pgc, 'execute_web_session_request', return_value=result):
            self.session.tool_request(call)
        start, finish = [event['params']['update'] for event in self.events()]
        self.assertEqual(start['title'], 'bash: printf hello')
        self.assertEqual(start['name'], 'bash')
        self.assertEqual(start['rawInput'], {'script': 'printf hello'})
        self.assertEqual(start['status'], 'in_progress')
        self.assertEqual(finish['status'], 'completed')
        self.assertIn('hello', finish['content'][0]['content']['text'])
        self.assertEqual(finish['rawOutput']['exit_code'], 0)
        self.assertEqual(start['toolCallId'], finish['toolCallId'])

    def test_batched_tools_have_distinct_output_and_skipped_call_is_failed(self):
        calls = [
            {'tool': 'status', 'id': 'first'},
            {'tool': 'run', 'id': 'second', 'command': ['printf', 'hello']},
            {'tool': 'read', 'id': 'not-run', 'path': 'skipped.txt'},
        ]
        request = {'session': self.session.routing_id, 'calls': calls}
        results = [
            {'tool': 'status', 'id': 'first', 'ok': True, 'status': '## feature/acp'},
            {'tool': 'run', 'id': 'second', 'ok': True, 'exit_code': 0,
             'stdout': 'hello', 'stderr': ''},
        ]
        with mock.patch.object(acp.pgc, 'execute_web_session_request', return_value=results):
            self.session.tool_request(request)
        events = [event['params']['update'] for event in self.events()]
        self.assertEqual([event['sessionUpdate'] for event in events],
                         ['tool_call'] * 3 + ['tool_call_update'] * 3)
        self.assertEqual([event['status'] for event in events[3:]],
                         ['completed', 'completed', 'failed'])
        self.assertEqual(len({event['toolCallId'] for event in events[:3]}), 3)
        self.assertIn('feature/acp', events[3]['content'][0]['content']['text'])
        self.assertIn('hello', events[4]['content'][0]['content']['text'])
        self.assertIn('Skipped or missing', events[5]['content'][0]['content']['text'])

    def test_nonzero_command_exit_is_failed_even_if_transport_ok(self):
        call = {'session': self.session.routing_id, 'tool': 'run',
                'id': 'failed', 'command': ['false']}
        result = {'tool': 'run', 'id': 'failed', 'ok': True, 'exit_code': 1,
                  'stdout': '', 'stderr': ''}
        with mock.patch.object(acp.pgc, 'execute_web_session_request', return_value=result):
            self.session.tool_request(call)
        self.assertEqual(self.events()[-1]['params']['update']['status'], 'failed')

    def test_watcher_callback_preserves_markdown(self):
        web = WebSession('root', 'Root', mock.Mock())
        self.session.web = web
        self.session.turn = acp.Turn(3, 'Task')
        interface = mock.Mock()
        interface.latest_assistant_snapshot.return_value = AssistantSnapshot('m1', (), 'Title\nAnswer')
        interface.markdown_from_assistant_snapshot.return_value = '## Title\n\n**Answer**'
        self.session.complete_from_watcher(interface, web, 'Title\nAnswer')
        self.assertEqual(self.events()[-2]['params']['update']['content']['text'], '## Title\n\n**Answer**')
        self.assertEqual(self.events()[-1]['result']['stopReason'], 'end_turn')
        self.assertIsNone(self.session.turn)

    def test_watcher_callback_does_not_use_markdown_from_stale_node(self):
        web = WebSession('root', 'Root', mock.Mock())
        self.session.web = web
        self.session.turn = acp.Turn(3, 'Task')
        interface = mock.Mock()
        interface.latest_assistant_snapshot.return_value = AssistantSnapshot('stale', (), 'Earlier reply')
        self.session.complete_from_watcher(interface, web, 'Correct reply')
        self.assertEqual(self.events()[-2]['params']['update']['content']['text'], 'Correct reply')
        interface.markdown_from_assistant_snapshot.assert_not_called()

    def test_watcher_callback_falls_back_on_extraction_error(self):
        web = WebSession('root', 'Root', mock.Mock())
        self.session.web = web
        self.session.turn = acp.Turn(3, 'Task')
        interface = mock.Mock()
        interface.latest_assistant_snapshot.side_effect = RuntimeError('DOM changed')
        with mock.patch('sys.stderr', io.StringIO()):
            self.session.complete_from_watcher(interface, web, 'Answer')
        self.assertEqual(self.events()[-2]['params']['update']['content']['text'], 'Answer')


if __name__ == '__main__':
    unittest.main()
