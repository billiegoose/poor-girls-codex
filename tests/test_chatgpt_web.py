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
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
        )
        watcher.refresh_sessions()
        self.assertEqual(set(watcher.sessions), {'a', 'b'})
        self.assertIsNot(watcher.sessions['a'].seen_fingerprints, watcher.sessions['b'].seen_fingerprints)
        self.assertIsNot(watcher.sessions['a'].pending_results, watcher.sessions['b'].pending_results)

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
        )
        self.assertFalse(watcher.deliver_session(a, 0.0))
        self.assertEqual(a.pending_results, ['RESULT'])
        self.assertFalse(watcher.deliver_session(a, 0.1))
        self.assertTrue(watcher.deliver_session(a, 0.25))
        self.assertEqual(a.pending_results, [])
        self.assertEqual(a.delivered, 1)


if __name__ == '__main__':
    unittest.main()
