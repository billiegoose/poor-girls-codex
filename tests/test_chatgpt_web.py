from __future__ import annotations

import unittest
from unittest import mock

import chatgpt_web as web


class FakeLocator:
    def __init__(self, texts=None, *, message_id=None):
        self.texts = list(texts or [])
        self.message_id = message_id

    @property
    def last(self):
        return self

    def count(self):
        return len(self.texts)

    def nth(self, index):
        return FakeLocator([self.texts[index]], message_id=self.message_id)

    def inner_text(self):
        return self.texts[0]

    def get_attribute(self, name):
        if name == 'data-message-id':
            return self.message_id
        return None

    def locator(self, selector):
        if selector == 'pre code':
            return FakeLocator(self.texts, message_id=self.message_id)
        return FakeLocator([])


class FakePage:
    def __init__(self, url, *, title='', code=None, message_id='message-1'):
        self.url = url
        self._title = title
        self.code = code or []
        self.message_id = message_id

    def title(self):
        return self._title

    def locator(self, selector):
        if selector == web.ASSISTANT_SELECTOR:
            return FakeLocator(self.code, message_id=self.message_id)
        return FakeLocator([])


class ChatGPTWebTests(unittest.TestCase):
    def test_conversation_url_filter_is_exact(self) -> None:
        good = 'https' + '://chatgpt.com/c/abc'
        self.assertEqual(web.ChatGPTWeb.conversation_id_for_url(good), 'abc')
        self.assertIsNone(web.ChatGPTWeb.conversation_id_for_url('https' + '://chatgpt.com/'))
        self.assertIsNone(web.ChatGPTWeb.conversation_id_for_url('https' + '://chatgpt.com.evil.example/c/abc'))
        self.assertIsNone(web.ChatGPTWeb.conversation_id_for_url('https' + '://example.com/c/abc'))

    def test_pages_only_returns_conversation_tabs(self) -> None:
        interface = web.ChatGPTWeb()
        context = mock.Mock()
        context.pages = [
            FakePage('https' + '://chatgpt.com/c/a'),
            FakePage('https' + '://chatgpt.com/'),
            FakePage('https' + '://example.com/'),
        ]
        browser = mock.Mock()
        browser.contexts = [context]
        interface._browser = browser
        self.assertEqual([page.url for page in interface.pages()], ['https' + '://chatgpt.com/c/a'])

    def test_create_conversation_uses_plain_chatgpt_root_url(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/child'
        page.locator.return_value.last.wait_for.return_value = None
        page.wait_for_url.return_value = None
        context = mock.Mock()
        context.new_page.return_value = page
        origin_page = mock.Mock()
        origin_page.context = context
        origin = web.WebSession('parent', 'Parent', origin_page)
        interface.submit = mock.Mock()
        interface.describe_page = mock.Mock(return_value=('child', 'Child'))

        child = interface.create_conversation(
            origin=origin,
            label='subagent:worker',
            prompt='Do work',
        )

        page.goto.assert_called_once_with('https' + '://chatgpt.com/', wait_until='domcontentloaded')
        self.assertEqual(child.conversation_id, 'child')

    def test_submit_uses_dom_click_without_playwright_actionability(self) -> None:
        interface = web.ChatGPTWeb()
        button = mock.Mock()
        button.is_enabled.return_value = True
        locator = mock.Mock()
        locator.count.return_value = 1
        locator.last = button
        page = mock.Mock()
        page.locator.return_value = locator
        session = web.WebSession('a', 'Background', page)
        interface.set_composer_text = mock.Mock()

        interface.submit(session, 'result')

        interface.set_composer_text.assert_called_once_with(session, 'result')
        button.evaluate.assert_called_once_with('element => element.click()')
        button.click.assert_not_called()

    def test_fingerprint_includes_assistant_message_identity(self) -> None:
        interface = web.ChatGPTWeb()
        source = '{"id":"same","tool":"status"}'
        session = web.WebSession('a', 'A', FakePage('https' + '://chatgpt.com/c/a', code=[source], message_id='m1'))
        first = interface.valid_request(session, lambda request: None)
        session.page.message_id = 'm2'
        second = interface.valid_request(session, lambda request: None)
        self.assertIsNotNone(first)
        self.assertIsNotNone(second)
        self.assertNotEqual(first[2], second[2])

    def test_refresh_gives_each_conversation_independent_state(self) -> None:
        interface = mock.Mock()
        pages = [
            FakePage('https' + '://chatgpt.com/c/a', title='Cats'),
            FakePage('https' + '://chatgpt.com/c/b', title='Dogs'),
        ]
        interface.pages.return_value = pages
        interface.describe_page.side_effect = [('a', 'Cats'), ('b', 'Dogs')]
        interface.valid_request.return_value = None
        interface.latest_too_long_error_id.return_value = None
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.refresh_sessions()
        self.assertEqual(set(watcher.sessions), {'a', 'b'})
        self.assertIsNot(watcher.sessions['a'].seen_fingerprints, watcher.sessions['b'].seen_fingerprints)
        self.assertIsNot(watcher.sessions['a'].pending_results, watcher.sessions['b'].pending_results)

    def test_new_session_primes_and_binds_visible_root_without_executing(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        interface.valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'visible-toolcall',
        )
        execute = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        session = watcher.sessions['a']
        self.assertEqual(session.routing_session_id, 'root')
        self.assertIs(watcher.sessions_by_routing_id['root'], session)
        self.assertEqual(session.seen_fingerprints, {'visible-toolcall'})
        execute.assert_not_called()

    def test_refresh_rekeys_same_page_when_temporary_conversation_id_changes(self) -> None:
        interface = mock.Mock()
        original_page = mock.Mock()
        rediscovered_page = mock.Mock()
        original_page.__eq__ = mock.Mock(return_value=True)
        rediscovered_page.__eq__ = mock.Mock(return_value=True)
        interface.pages.return_value = [rediscovered_page]
        interface.describe_page.return_value = ('real-child', 'Child')
        interface.valid_request.return_value = ('{}', {'tool': 'status'}, 'visible-toolcall')
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        child = web.WebSession(
            'WEB:temporary',
            'subagent:worker',
            original_page,
            routing_session_id='root::worker',
        )
        child.pending_results.append('pending')
        watcher.sessions['WEB:temporary'] = child
        watcher.sessions_by_routing_id['root::worker'] = child

        watcher.refresh_sessions()

        self.assertNotIn('WEB:temporary', watcher.sessions)
        self.assertIs(watcher.sessions['real-child'], child)
        self.assertEqual(child.conversation_id, 'real-child')
        self.assertEqual(child.routing_session_id, 'root::worker')
        self.assertEqual(child.pending_results, ['pending'])
        self.assertEqual(child.seen_fingerprints, set())
        self.assertIs(watcher.sessions_by_routing_id['root::worker'], child)
        interface.valid_request.assert_not_called()

    def test_root_session_has_no_progress_prefix(self) -> None:
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )
        self.assertIsNone(watcher.progress_prefix('root'))

    def test_subagent_progress_prefix_uses_leaf_name(self) -> None:
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )
        with mock.patch.object(web.sys.stdout, 'isatty', return_value=False):
            self.assertEqual(watcher.progress_prefix('root::parent::closure-parity'), '[closure-parity]')

    def test_subagent_uses_derived_session_and_composed_prompt(self) -> None:
        interface = mock.Mock()
        child = web.WebSession(
            'child-conversation',
            'subagent:worker2',
            FakePage('https' + '://chatgpt.com/c/child-conversation'),
        )
        interface.create_conversation.return_value = child
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            bootstrap_prompt_for_session=lambda session: f'BOOTSTRAP {session}',
        )
        parent = web.WebSession(
            'parent-conversation',
            'Parent',
            mock.Mock(),
            routing_session_id='root',
        )
        watcher.sessions[parent.conversation_id] = parent
        watcher.sessions_by_routing_id['root'] = parent

        result = watcher.execute_orchestration_tool(
            parent,
            'root',
            {'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Investigate this'},
            0,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['session'], 'root::worker2')
        self.assertIs(watcher.sessions_by_routing_id['root::worker2'], child)
        interface.create_conversation.assert_called_once_with(
            origin=parent,
            label='subagent:worker2',
            prompt='Investigate this\n\n---\n\nBOOTSTRAP root::worker2',
        )

    def test_duplicate_subagent_session_is_rejected(self) -> None:
        interface = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            bootstrap_prompt_for_session=lambda session: f'BOOTSTRAP {session}',
        )
        existing = web.WebSession('child', 'Child', mock.Mock(), routing_session_id='root::worker2')
        watcher.sessions_by_routing_id['root::worker2'] = existing
        result = watcher.execute_orchestration_tool(
            web.WebSession('parent', 'Parent', mock.Mock(), routing_session_id='root'),
            'root',
            {'id': 's', 'tool': 'subagent', 'name': 'worker2', 'prompt': 'Again'},
            0,
        )
        self.assertFalse(result['ok'])
        self.assertIn('already exists', result['error'])
        interface.create_conversation.assert_not_called()

    def test_handoff_queues_result_on_immediate_parent(self) -> None:
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        root = web.WebSession('root-c', 'Root', mock.Mock(), routing_session_id='root')
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root::a')
        child = web.WebSession('child-c', 'Child', mock.Mock(), routing_session_id='root::a::b')
        watcher.sessions_by_routing_id = {
            'root': root,
            'root::a': parent,
            'root::a::b': child,
        }

        result = watcher.execute_orchestration_tool(
            child,
            'root::a::b',
            {'id': 'h', 'tool': 'handoff', 'result': 'nested result'},
            0,
        )

        self.assertTrue(result['ok'])
        self.assertEqual(result['parent_session'], 'root::a')
        self.assertEqual(root.pending_results, [])
        self.assertEqual(len(parent.pending_results), 1)
        self.assertIn('nested result', parent.pending_results[0])

    def test_root_handoff_is_rejected(self) -> None:
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        result = watcher.execute_orchestration_tool(
            web.WebSession('root-c', 'Root', mock.Mock(), routing_session_id='root'),
            'root',
            {'id': 'h', 'tool': 'handoff', 'result': 'no parent'},
            0,
        )
        self.assertFalse(result['ok'])
        self.assertIn('no parent', result['error'])

    def test_new_session_primes_existing_request_without_executing_it(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.valid_request.return_value = ('{}', {'tool': 'status'}, 'already-visible')
        execute = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.refresh_sessions()
        self.assertEqual(watcher.sessions['a'].seen_fingerprints, {'already-visible'})
        execute.assert_not_called()

    def test_settling_is_nonblocking_and_requires_same_fingerprint(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.side_effect = [
            ('{}', {'tool': 'status'}, 'first'),
            ('{}', {'tool': 'status'}, 'second'),
            ('{}', {'tool': 'status'}, 'second'),
        ]
        execute = mock.Mock(return_value={'ok': True})
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.35,
        )
        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertFalse(watcher.scan_session(session, 1.0))
        self.assertTrue(watcher.scan_session(session, 1.35))
        execute.assert_called_once_with({'tool': 'status'}, announce=True)
        self.assertEqual(session.pending_results, ['RESULT'])
        self.assertEqual(session.seen_fingerprints, {'second'})

    def test_step_routes_results_to_originating_sessions(self) -> None:
        interface = mock.Mock()
        a = web.WebSession('a', 'Cats', mock.Mock(), settling_fingerprint='fa', settle_deadline=0.0)
        b = web.WebSession('b', 'Dogs', mock.Mock(), settling_fingerprint='fb', settle_deadline=0.0)
        requests = {
            'a': ('{}', {'id': 'a', 'tool': 'status'}, 'fa'),
            'b': ('{}', {'id': 'b', 'tool': 'tree'}, 'fb'),
        }
        interface.pages.return_value = [a.page, b.page]
        interface.describe_page.side_effect = [('a', 'Cats'), ('b', 'Dogs')]
        interface.valid_request.side_effect = lambda session, validate: requests[session.conversation_id]
        execute = mock.Mock(side_effect=lambda request, announce=False: {'id': request['id'], 'ok': True})
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'RESULT-' + result['id'],
            too_long_fallback=lambda request, result: 'FALLBACK-' + result['id'],
            settle_seconds=0.0,
            clock=lambda: 1.0,
        )
        watcher.sessions = {'a': a, 'b': b}
        watcher.step()
        self.assertEqual(interface.submit.call_args_list, [
            mock.call(a, 'RESULT-a\n'),
            mock.call(b, 'RESULT-b\n'),
        ])
        self.assertEqual(a.delivered, 1)
        self.assertEqual(b.delivered, 1)

    def test_inspect_web_sessions_is_read_only_and_detaches(self) -> None:
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface = mock.Mock()
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        with mock.patch.object(web, 'ChatGPTWeb', return_value=interface):
            rows = web.inspect_web_sessions(cdp_url='http' + '://127.0.0.1:9222')
        self.assertEqual(rows, [('a', 'Cats', 'https' + '://chatgpt.com/c/a')])
        interface.connect.assert_called_once_with()
        interface.close.assert_called_once_with()
        interface.submit.assert_not_called()

    def test_delivery_failure_is_isolated_and_retried_without_reexecution(self) -> None:
        interface = mock.Mock()
        a = web.WebSession('a', 'Cats', mock.Mock(), pending_results=['RESULT'])
        interface.submit.side_effect = [RuntimeError('busy'), None]
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        self.assertFalse(watcher.deliver_session(a, 0.0))
        self.assertEqual(a.pending_results, ['RESULT'])
        self.assertFalse(watcher.deliver_session(a, 0.1))
        self.assertTrue(watcher.deliver_session(a, 0.25))
        self.assertEqual(a.pending_results, [])
        self.assertEqual(a.delivered, 1)

    def test_too_long_delivery_sends_compact_fallback_without_reexecution(self) -> None:
        interface = mock.Mock()
        interface.submit.side_effect = [web.MessageTooLongError('too long'), None]
        execute = mock.Mock(return_value={'id': 'x', 'tool': 'status', 'ok': True})
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'FULL',
            too_long_fallback=lambda request, result: 'COMPACT',
            settle_seconds=0.0,
        )
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        interface.valid_request.return_value = (
            '{}',
            {'id': 'x', 'tool': 'status'},
            'fp',
        )

        self.assertTrue(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.deliver_session(session, 0.0))
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, announce=True)
        self.assertEqual(
            interface.submit.call_args_list,
            [mock.call(session, 'FULL\n'), mock.call(session, 'COMPACT\n')],
        )
        self.assertEqual(session.pending_results, [])
        self.assertEqual(session.pending_fallbacks, [])
        self.assertEqual(session.delivered, 1)

    def test_post_submit_too_long_error_queues_compact_retry_without_reexecution(self) -> None:
        interface = mock.Mock()
        interface.latest_too_long_error_id.return_value = 'error-1'
        execute = mock.Mock(return_value={'id': 'x', 'tool': 'status', 'ok': True})
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'FULL',
            too_long_fallback=lambda request, result: 'COMPACT',
            settle_seconds=0.0,
        )
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        interface.valid_request.return_value = ('{}', {'id': 'x', 'tool': 'status'}, 'fp')

        self.assertTrue(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.deliver_session(session, 0.0))
        self.assertEqual(session.last_submitted_fallbacks, ['COMPACT'])
        self.assertTrue(watcher.recover_rejected_submission(session))
        self.assertEqual(session.pending_results, ['COMPACT'])
        self.assertEqual(session.pending_fallbacks, ['COMPACT'])
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, announce=True)

        # The same rendered error must not enqueue the compact result repeatedly.
        session.pending_results.clear()
        session.pending_fallbacks.clear()
        session.last_submitted_fallbacks = ['COMPACT']
        self.assertFalse(watcher.recover_rejected_submission(session))
        self.assertEqual(session.pending_results, [])
        self.assertEqual(session.pending_fallbacks, [])

    def test_existing_too_long_error_is_baselined_when_tab_attaches(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.valid_request.return_value = None
        interface.latest_too_long_error_id.return_value = 'existing-error'
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        watcher.refresh_sessions()
        session = watcher.sessions['a']
        session.last_submitted_fallbacks = ['COMPACT']
        self.assertEqual(session.last_too_long_error_id, 'existing-error')
        self.assertFalse(watcher.recover_rejected_submission(session))
        self.assertEqual(session.pending_results, [])


if __name__ == '__main__':
    unittest.main()
