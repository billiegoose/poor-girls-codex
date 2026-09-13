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
        if not self.texts:
            return self
        return self.nth(len(self.texts) - 1)

    def count(self):
        return len(self.texts)

    def nth(self, index):
        message_id = self.message_id
        if isinstance(message_id, list):
            message_id = message_id[index]
        return FakeLocator([self.texts[index]], message_id=message_id)

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
        page.locator.assert_any_call(web.ASSISTANT_SELECTOR)
        page.locator.return_value.last.wait_for.assert_any_call(state='attached', timeout=30_000)
        self.assertEqual(child.conversation_id, 'child')

    def test_archive_conversation_marks_chat_archived(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.evaluate.return_value = {'ok': True, 'status': 200, 'text': ''}
        session = web.WebSession('conversation-123', 'Child', page)

        interface.archive_conversation(session)

        script, conversation_id = page.evaluate.call_args.args
        self.assertEqual(conversation_id, 'conversation-123')
        self.assertIn('is_archived: true', script)
        self.assertIn("method: 'PATCH'", script)

    def test_archive_conversation_raises_on_failed_request(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.evaluate.return_value = {'ok': False, 'status': 500, 'text': 'archive failed'}
        session = web.WebSession('conversation-123', 'Child', page)

        with self.assertRaisesRegex(RuntimeError, 'failed to archive.*500.*archive failed'):
            interface.archive_conversation(session)

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

    def test_submit_focuses_background_tab_only_when_send_button_is_missing(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        enabled_button = mock.Mock()
        enabled_button.is_enabled.return_value = True
        present = mock.Mock()
        present.count.return_value = 1
        present.last = enabled_button
        page = mock.Mock()
        page.locator.side_effect = [missing, present]
        page.get_by_role.return_value = missing
        session = web.WebSession('a', 'Background', page)
        interface.set_composer_text = mock.Mock()

        interface.submit(session, 'result')

        page.bring_to_front.assert_called_once_with()
        enabled_button.evaluate.assert_called_once_with('element => element.click()')

    def test_submit_does_not_focus_when_send_button_is_already_enabled(self) -> None:
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

        page.bring_to_front.assert_not_called()
        button.evaluate.assert_called_once_with('element => element.click()')

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

    def test_recent_valid_request_looks_past_newer_non_tool_assistant_message(self) -> None:
        interface = web.ChatGPTWeb()
        session = web.WebSession(
            'a',
            'A',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=[
                    '{"session":"root","id":"x","tool":"status"}',
                    'finished normally without a tool call',
                ],
                message_id=['m1', 'm2'],
            ),
        )

        result = interface.recent_valid_request(
            session,
            lambda request: None if request.get('session') == 'root' else (_ for _ in ()).throw(ValueError()),
        )

        self.assertIsNotNone(result)
        _, request, fingerprint = result
        self.assertEqual(request['session'], 'root')
        self.assertEqual(request['tool'], 'status')
        self.assertTrue(fingerprint)

    def test_recent_valid_request_is_bounded_to_last_three_assistant_messages(self) -> None:
        interface = web.ChatGPTWeb()
        session = web.WebSession(
            'a',
            'A',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=[
                    '{"session":"root","id":"old","tool":"status"}',
                    'newer prose one',
                    'newer prose two',
                    'newer prose three',
                ],
                message_id=['m0', 'm1', 'm2', 'm3'],
            ),
        )

        self.assertIsNone(interface.recent_valid_request(session, lambda request: None))

    def test_refresh_gives_each_conversation_independent_state(self) -> None:
        interface = mock.Mock()
        pages = [
            FakePage('https' + '://chatgpt.com/c/a', title='Cats'),
            FakePage('https' + '://chatgpt.com/c/b', title='Dogs'),
        ]
        interface.pages.return_value = pages
        interface.describe_page.side_effect = [('a', 'Cats'), ('b', 'Dogs')]
        interface.recent_valid_request.return_value = None
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
        self.assertIsNot(watcher.sessions['a'].pending_responses, watcher.sessions['b'].pending_responses)

    def test_new_session_primes_and_binds_visible_root_without_executing(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        interface.recent_valid_request.return_value = (
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

    def test_ensure_root_session_reuses_chat_with_recent_root_toolcall(self) -> None:
        interface = mock.Mock()
        existing = web.WebSession('a', 'Existing root', mock.Mock())
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'recent-root-toolcall',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )
        watcher.sessions['a'] = existing

        root = watcher.ensure_root_session('BOOTSTRAP')

        self.assertIs(root, existing)
        self.assertEqual(existing.routing_session_id, 'root')
        self.assertIs(watcher.sessions_by_routing_id['root'], existing)
        self.assertEqual(existing.seen_fingerprints, {'recent-root-toolcall'})
        interface.recent_valid_request.assert_called_once_with(existing, watcher.validate_request)
        interface.create_root_conversation.assert_not_called()

    def test_ensure_root_session_ignores_recent_descendant_toolcall(self) -> None:
        interface = mock.Mock()
        existing = web.WebSession('a', 'Subagent', mock.Mock())
        created = web.WebSession('new-root', 'Created root', mock.Mock())
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root::worker', 'tool': 'status'},
            'recent-child-toolcall',
        )
        interface.create_root_conversation.return_value = created
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )
        watcher.sessions['a'] = existing

        root = watcher.ensure_root_session('BOOTSTRAP')

        self.assertIs(root, created)
        interface.create_root_conversation.assert_called_once_with('BOOTSTRAP')
        self.assertIs(watcher.sessions['new-root'], created)
        self.assertEqual(created.routing_session_id, 'root')
        self.assertIs(watcher.sessions_by_routing_id['root'], created)

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
        child.pending_responses.append(web.PendingResponse('pending', 'pending'))
        watcher.sessions['WEB:temporary'] = child
        watcher.sessions_by_routing_id['root::worker'] = child

        watcher.refresh_sessions()

        self.assertNotIn('WEB:temporary', watcher.sessions)
        self.assertIs(watcher.sessions['real-child'], child)
        self.assertEqual(child.conversation_id, 'real-child')
        self.assertEqual(child.routing_session_id, 'root::worker')
        self.assertEqual([response.result for response in child.pending_responses], ['pending'])
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
        self.assertEqual(root.pending_responses, [])
        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('nested result', parent.pending_responses[0].result)

    def test_handoff_result_delivers_from_parent_without_parallel_state(self) -> None:
        interface = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root::a')
        child = web.WebSession('child-c', 'Child', mock.Mock(), routing_session_id='root::a::b')
        watcher.sessions_by_routing_id = {
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
        self.assertTrue(watcher.deliver_session(parent, 0.0))
        interface.submit.assert_called_once()
        submitted_session, submitted_text = interface.submit.call_args.args
        self.assertIs(submitted_session, parent)
        self.assertIn('nested result', submitted_text)
        self.assertEqual(parent.pending_responses, [])
        self.assertEqual(parent.delivered, 1)

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

    def test_failed_handoff_stays_open_and_queues_tool_result(self) -> None:
        interface = mock.Mock()
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        request = {
            'session': 'root::worker',
            'id': 'h',
            'tool': 'handoff',
            'result': 'cannot deliver',
        }
        interface.valid_request.return_value = ('{}', request, 'fp')

        def execute(request, **kwargs):
            return kwargs['tool_executor'](request, 0)

        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root::worker': child}

        self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertFalse(child.retire_requested)
        self.assertIs(watcher.sessions['child-c'], child)
        self.assertIs(watcher.sessions_by_routing_id['root::worker'], child)
        interface.archive_conversation.assert_not_called()
        child_page.close.assert_not_called()
        self.assertEqual([response.result for response in child.pending_responses], ['RESULT'])

    def test_successful_handoff_archives_closes_and_unbinds_subagent(self) -> None:
        interface = mock.Mock()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        request = {
            'session': 'root::worker',
            'id': 'h',
            'tool': 'handoff',
            'result': 'finished work',
        }
        interface.valid_request.return_value = ('{}', request, 'fp')

        def execute(request, **kwargs):
            return kwargs['tool_executor'](request, 0)

        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('finished work', parent.pending_responses[0].result)
        interface.archive_conversation.assert_called_once_with(child)
        child_page.close.assert_called_once_with()
        self.assertNotIn('child-c', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)
        self.assertIsNone(child.routing_session_id)
        self.assertEqual(child.pending_responses, [])
        interface.submit.assert_not_called()

    def test_handoff_retirement_retries_archive_without_reexecuting_handoff(self) -> None:
        interface = mock.Mock()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        request = {
            'session': 'root::worker',
            'id': 'h',
            'tool': 'handoff',
            'result': 'finished once',
        }
        interface.pages.return_value = [child_page]
        interface.describe_page.return_value = ('child-c', 'Child')
        interface.valid_request.return_value = ('{}', request, 'fp')
        interface.archive_conversation.side_effect = [RuntimeError('archive busy'), None]
        executions = 0

        def execute(request, **kwargs):
            nonlocal executions
            executions += 1
            return kwargs['tool_executor'](request, 0)

        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
            clock=lambda: 0.0,
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        watcher.step()
        self.assertTrue(child.retire_requested)
        self.assertEqual(executions, 1)
        self.assertEqual(len(parent.pending_responses), 1)
        child_page.close.assert_not_called()

        watcher.step()
        self.assertEqual(executions, 1)
        self.assertEqual(len(parent.pending_responses), 1)
        self.assertEqual(interface.archive_conversation.call_count, 2)
        child_page.close.assert_called_once_with()
        self.assertNotIn('child-c', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

    def test_calls_after_successful_handoff_are_skipped(self) -> None:
        interface = mock.Mock()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        request = {
            'session': 'root::worker',
            'calls': [
                {'id': 'h', 'tool': 'handoff', 'result': 'done now'},
                {'id': 'x', 'tool': 'status'},
            ],
        }
        interface.valid_request.return_value = ('{}', request, 'fp')
        captured_results = []

        def execute(request, **kwargs):
            for index, call in enumerate(request['calls']):
                captured_results.append(kwargs['tool_executor'](call, index))
            return captured_results

        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        with mock.patch('toolcall_lib.execute') as local_execute:
            self.assertTrue(watcher.scan_session(child, 0.0))

        local_execute.assert_not_called()
        self.assertTrue(captured_results[0]['ok'])
        self.assertTrue(captured_results[1]['skipped'])
        self.assertIn('already handed off', captured_results[1]['error'])
        interface.archive_conversation.assert_called_once_with(child)
        child_page.close.assert_called_once_with()

    def test_mixed_batch_detects_handoff_result_at_matching_index(self) -> None:
        interface = mock.Mock()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            settling_fingerprint='fp',
            settle_deadline=0.0,
        )
        request = {
            'session': 'root::worker',
            'calls': [
                {'id': 'x', 'tool': 'status'},
                {'id': 'h', 'tool': 'handoff', 'result': 'mixed result'},
            ],
        }
        interface.valid_request.return_value = ('{}', request, 'fp')

        def execute(request, **kwargs):
            return [
                kwargs['tool_executor'](call, index)
                for index, call in enumerate(request['calls'])
            ]

        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        with mock.patch('toolcall_lib.execute', return_value={'id': 'x', 'tool': 'status', 'ok': True}):
            self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('mixed result', parent.pending_responses[0].result)
        interface.archive_conversation.assert_called_once_with(child)
        child_page.close.assert_called_once_with()
        self.assertEqual(child.pending_responses, [])

    def test_new_session_primes_existing_request_without_executing_it(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.recent_valid_request.return_value = ('{}', {'tool': 'status'}, 'already-visible')
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
        self.assertEqual([response.result for response in session.pending_responses], ['RESULT'])
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

    def test_run_web_watcher_passes_bootstrap_prompt_to_watcher_run(self) -> None:
        interface = mock.Mock()
        watcher = mock.Mock()
        with (
            mock.patch.object(web, 'ChatGPTWeb', return_value=interface),
            mock.patch.object(web, 'WebSessionWatcher', return_value=watcher) as watcher_type,
        ):
            web.run_web_watcher(
                cdp_url='http' + '://127.0.0.1:9222',
                session_id='root',
                bootstrap_prompt='BOOTSTRAP',
                validate_request=lambda request: None,
                execute_request=mock.Mock(),
                fenced_result=str,
                too_long_fallback=lambda request, result: 'FALLBACK',
            )

        interface.connect.assert_called_once_with()
        interface.close.assert_called_once_with()
        watcher_type.assert_called_once()
        watcher.run.assert_called_once_with(bootstrap_prompt='BOOTSTRAP')

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
        progress = mock.Mock(spec=web.ResponseProgress)
        a = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK', progress=progress)],
        )
        interface.submit.side_effect = [RuntimeError('busy'), None]
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        self.assertFalse(watcher.deliver_session(a, 0.0))
        self.assertEqual([response.result for response in a.pending_responses], ['RESULT'])
        progress.set_status.assert_called_with(
            'ChatGPT send button is not available [retry in 0.25s]'
        )
        self.assertFalse(watcher.deliver_session(a, 0.1))
        self.assertTrue(watcher.deliver_session(a, 0.25))
        self.assertEqual(a.pending_responses, [])
        self.assertEqual(a.delivered, 1)
        progress.set_status.assert_called_with('[sent]')

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
        progress = session.pending_responses[0].progress
        self.assertEqual(progress.status, '[pending]')
        self.assertTrue(watcher.deliver_session(session, 0.0))
        self.assertEqual(progress.status, '[sent summary]')
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, announce=True)
        self.assertEqual(
            interface.submit.call_args_list,
            [mock.call(session, 'FULL\n'), mock.call(session, 'COMPACT\n')],
        )
        self.assertEqual(session.pending_responses, [])
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
        progress = session.pending_responses[0].progress
        self.assertTrue(watcher.deliver_session(session, 0.0))
        self.assertEqual(progress.status, '[sent]')
        self.assertEqual(
            [response.fallback for response in session.last_submitted_responses],
            ['COMPACT'],
        )
        self.assertTrue(watcher.recover_rejected_submission(session))
        self.assertEqual(progress.status, 'Message too large [retry with summary]')
        self.assertEqual([response.render() for response in session.pending_responses], ['COMPACT'])
        self.assertTrue(session.pending_responses[0].compact)
        execute.assert_called_once_with({'id': 'x', 'tool': 'status'}, announce=True)

        # The same rendered error must not enqueue the compact result repeatedly.
        session.pending_responses.clear()
        session.last_submitted_responses = [web.PendingResponse('FULL', 'COMPACT')]
        self.assertFalse(watcher.recover_rejected_submission(session))
        self.assertEqual(session.pending_responses, [])

    def test_existing_too_long_error_is_baselined_when_tab_attaches(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.recent_valid_request.return_value = None
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
        session.last_submitted_responses = [web.PendingResponse('FULL', 'COMPACT')]
        self.assertEqual(session.last_too_long_error_id, 'existing-error')
        self.assertFalse(watcher.recover_rejected_submission(session))
        self.assertEqual(session.pending_responses, [])


if __name__ == '__main__':
    unittest.main()
