from __future__ import annotations

import unittest
from unittest import mock

import chatgpt_web as web


class FakeLocator:
    def __init__(self, texts=None, *, message_id=None, author_role=None):
        self.texts = list(texts or [])
        self.message_id = message_id
        self.author_role = author_role

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
        author_role = self.author_role
        if isinstance(author_role, list):
            author_role = author_role[index]
        return FakeLocator([self.texts[index]], message_id=message_id, author_role=author_role)

    def inner_text(self):
        return self.texts[0]

    def get_attribute(self, name):
        if name == 'data-message-id':
            return self.message_id
        if name == 'data-message-author-role':
            return self.author_role
        return None

    def locator(self, selector):
        if selector == 'pre code':
            return FakeLocator(self.texts, message_id=self.message_id)
        return FakeLocator([])


class FakePage:
    def __init__(
        self,
        url,
        *,
        title='',
        code=None,
        message_id='message-1',
        message_roles=None,
    ):
        self.url = url
        self._title = title
        self.code = code or []
        self.message_id = message_id
        self.message_roles = list(message_roles or [])

    def title(self):
        return self._title

    def locator(self, selector):
        if selector == web.ASSISTANT_SELECTOR:
            return FakeLocator(self.code, message_id=self.message_id)
        if selector == '[data-message-author-role]':
            return FakeLocator(
                [''] * len(self.message_roles),
                author_role=self.message_roles,
            )
        return FakeLocator([])


class ChatGPTWebTests(unittest.TestCase):
    def test_launch_chrome_for_cdp_uses_dedicated_profile_and_debugging_flags(self) -> None:
        with (
            mock.patch.object(web.Path, 'home', return_value=web.Path('/Users/tester')),
            mock.patch.object(web.subprocess, 'run') as run,
        ):
            web.ChatGPTWeb.launch_chrome_for_cdp()

        run.assert_called_once_with(
            [
                'open',
                '-na',
                'Google Chrome',
                '--args',
                '--remote-debugging-port=9222',
                '--remote-allow-origins=*',
                '--user-data-dir=/Users/tester/.chrome-pgc',
                '--no-first-run',
                '--no-default-browser-check',
                'https://chatgpt.com/',
            ],
            check=True,
        )

    def test_connect_launches_chrome_and_retries_default_cdp_after_initial_failure(self) -> None:
        interface = web.ChatGPTWeb()
        playwright = mock.Mock()
        browser = mock.Mock()
        playwright.chromium.connect_over_cdp.side_effect = [
            RuntimeError('connection refused'),
            RuntimeError('still starting'),
            browser,
        ]
        interface._start_playwright = mock.Mock(return_value=playwright)
        interface.launch_chrome_for_cdp = mock.Mock()

        with (
            mock.patch.object(web.time, 'monotonic', side_effect=[100.0, 100.0]),
            mock.patch.object(web.time, 'sleep') as sleep,
        ):
            interface.connect()

        interface.launch_chrome_for_cdp.assert_called_once_with()
        self.assertIs(interface._browser, browser)
        self.assertEqual(playwright.chromium.connect_over_cdp.call_count, 3)
        playwright.chromium.connect_over_cdp.assert_called_with(
            web.DEFAULT_CDP_URL,
            no_defaults=True,
        )
        sleep.assert_called_once_with(web.CHROME_CDP_RETRY_SECONDS)
        playwright.stop.assert_not_called()

    def test_connect_times_out_after_launch_and_detaches_playwright(self) -> None:
        interface = web.ChatGPTWeb()
        playwright = mock.Mock()
        playwright.chromium.connect_over_cdp.side_effect = RuntimeError('connection refused')
        interface._start_playwright = mock.Mock(return_value=playwright)
        interface.launch_chrome_for_cdp = mock.Mock()

        with (
            mock.patch.object(web.time, 'monotonic', side_effect=[100.0, 111.0]),
            mock.patch.object(web.time, 'sleep') as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, 'connection refused'):
                interface.connect()

        interface.launch_chrome_for_cdp.assert_called_once_with()
        self.assertEqual(playwright.chromium.connect_over_cdp.call_count, 2)
        sleep.assert_not_called()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(interface._playwright)
        self.assertIsNone(interface._browser)

    def test_connect_does_not_launch_local_chrome_for_custom_cdp_url(self) -> None:
        interface = web.ChatGPTWeb('http' + '://example.test:9333')
        playwright = mock.Mock()
        playwright.chromium.connect_over_cdp.side_effect = RuntimeError('connection refused')
        interface._start_playwright = mock.Mock(return_value=playwright)
        interface.launch_chrome_for_cdp = mock.Mock()

        with self.assertRaisesRegex(RuntimeError, 'connection refused'):
            interface.connect()

        interface.launch_chrome_for_cdp.assert_not_called()
        playwright.stop.assert_called_once_with()
        self.assertIsNone(interface._playwright)

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

    def test_conversations_response_429_starts_backoff_without_reading_headers(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        interface.instrument_conversations_responses(page)
        callback = page.on.call_args.args[1]
        response = mock.Mock()
        response.url = 'https' + '://chatgpt.com/backend-api/conversations?offset=0&limit=28'
        response.status = 429
        response.request.method = 'GET'

        interface.dismiss_rate_limit_dialog = mock.Mock(return_value=False)
        with (
            mock.patch.object(web.time, 'monotonic', return_value=100.0),
            mock.patch('builtins.print') as printed,
        ):
            callback(response)

        interface.dismiss_rate_limit_dialog.assert_called_once_with(page)
        output = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('[web:rate-limit] GET 429', output)
        self.assertIn('pausing PGC for 60s', output)
        self.assertEqual(interface.rate_limit_remaining(100.0), 60.0)
        response.all_headers.assert_not_called()

    def test_conversations_response_success_is_silent_and_does_not_read_headers(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        response = mock.Mock()
        response.url = 'https' + '://chatgpt.com/backend-api/conversations?offset=0'
        response.status = 200
        response.request.method = 'GET'

        with mock.patch('builtins.print') as printed:
            interface.log_conversations_response(page, response)

        printed.assert_not_called()
        response.all_headers.assert_not_called()

    def test_conversations_response_instrumentation_logs_singular_conversation_endpoint(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        response = mock.Mock()
        response.url = 'https' + '://chatgpt.com/backend-api/conversation/abcdef123456'
        response.status = 429
        response.request.method = 'GET'

        with (
            mock.patch.object(web.time, 'monotonic', return_value=100.0),
            mock.patch('builtins.print') as printed,
        ):
            interface.log_conversations_response(page, response)

        output = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('GET 429', output)
        self.assertIn('/backend-api/conversation/abcdef123456', output)
        response.all_headers.assert_not_called()

    def test_conversations_response_bare_singular_success_is_silent(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        response = mock.Mock()
        response.url = 'https' + '://chatgpt.com/backend-api/conversation'
        response.status = 200
        response.request.method = 'POST'

        with mock.patch('builtins.print') as printed:
            interface.log_conversations_response(page, response)

        printed.assert_not_called()
        response.all_headers.assert_not_called()

    def test_conversations_response_instrumentation_logs_f_conversation_endpoint(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        response = mock.Mock()
        response.url = 'https' + '://chatgpt.com/backend-api/f/conversation'
        response.status = 429
        response.request.method = 'POST'

        with (
            mock.patch.object(web.time, 'monotonic', return_value=100.0),
            mock.patch('builtins.print') as printed,
        ):
            interface.log_conversations_response(page, response)

        output = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('POST 429', output)
        self.assertIn('/backend-api/f/conversation', output)
        response.all_headers.assert_not_called()

    def test_rate_limit_backoff_coalesces_burst_and_doubles_after_retry(self) -> None:
        interface = web.ChatGPTWeb()

        with mock.patch.object(web.time, 'monotonic', return_value=100.0):
            interface.note_rate_limit(
                method='GET',
                url='https://chatgpt.com/backend-api/conversations',
            )
        self.assertEqual(interface.rate_limit_remaining(100.0), 60.0)

        # More 429s from the same burst must not extend or exponentiate the pause.
        with mock.patch.object(web.time, 'monotonic', return_value=110.0):
            interface.note_rate_limit(
                method='GET',
                url='https://chatgpt.com/backend-api/conversations',
            )
        self.assertEqual(interface.rate_limit_remaining(110.0), 50.0)

        # If traffic is still rejected after the pause expires, back off harder.
        with mock.patch.object(web.time, 'monotonic', return_value=161.0):
            interface.note_rate_limit(
                method='GET',
                url='https://chatgpt.com/backend-api/conversations',
            )
        self.assertEqual(interface.rate_limit_remaining(161.0), 120.0)

    def test_rate_limit_backoff_resets_after_quiet_period(self) -> None:
        interface = web.ChatGPTWeb()
        with mock.patch.object(web.time, 'monotonic', return_value=100.0):
            interface.note_rate_limit(method='GET', url='https://chatgpt.com/backend-api/conversations')
        with mock.patch.object(web.time, 'monotonic', return_value=161.0):
            interface.note_rate_limit(method='GET', url='https://chatgpt.com/backend-api/conversations')
        self.assertEqual(interface.rate_limit_remaining(161.0), 120.0)

        with mock.patch.object(web.time, 'monotonic', return_value=1_062.0):
            interface.note_rate_limit(method='GET', url='https://chatgpt.com/backend-api/conversations')
        self.assertEqual(interface.rate_limit_remaining(1_062.0), 60.0)

    def test_dismiss_rate_limit_dialog_clicks_got_it(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        dialog = page.get_by_role.return_value.filter.return_value
        button = dialog.get_by_role.return_value.last
        button.count.return_value = 1
        button.is_visible.return_value = True

        self.assertTrue(interface.dismiss_rate_limit_dialog(page))

        page.get_by_role.assert_called_once_with('dialog')
        page.get_by_role.return_value.filter.assert_called_once_with(has_text='Too many requests')
        dialog.get_by_role.assert_called_once_with('button', name='Got it', exact=True)
        button.evaluate.assert_called_once_with('element => element.click()')

    def test_dismiss_rate_limit_dialog_is_harmless_when_modal_is_absent(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        button = page.get_by_role.return_value.filter.return_value.get_by_role.return_value.last
        button.count.return_value = 0

        self.assertFalse(interface.dismiss_rate_limit_dialog(page))
        button.evaluate.assert_not_called()

    def test_conversations_response_instrumentation_attaches_once_per_page(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()

        interface.instrument_conversations_responses(page)
        interface.instrument_conversations_responses(page)

        page.on.assert_called_once_with('response', mock.ANY)

    def test_conversations_response_instrumentation_ignores_same_path_on_other_hosts(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/abcdef123456'
        response = mock.Mock()
        response.url = 'https' + '://example.com/backend-api/conversations?offset=0'

        with mock.patch('builtins.print') as printed:
            interface.log_conversations_response(page, response)

        printed.assert_not_called()
        response.all_headers.assert_not_called()

    def test_conversations_response_instrumentation_ignores_malformed_urls(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        response = mock.Mock()
        response.url = 'https://[broken/backend-api/conversations'

        with mock.patch('builtins.print') as printed:
            interface.log_conversations_response(page, response)

        printed.assert_not_called()
        response.all_headers.assert_not_called()

    def test_refresh_sessions_attaches_conversations_instrumentation_to_existing_pages(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
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

        interface.instrument_conversations_responses.assert_called_once_with(page)

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

        page.on.assert_called_once_with('response', mock.ANY)
        page.goto.assert_called_once_with('https' + '://chatgpt.com/', wait_until='domcontentloaded')
        self.assertLess(
            page.method_calls.index(mock.call.on('response', mock.ANY)),
            page.method_calls.index(mock.call.goto('https' + '://chatgpt.com/', wait_until='domcontentloaded')),
        )
        page.locator.assert_any_call(web.ASSISTANT_SELECTOR)
        page.locator.return_value.last.wait_for.assert_any_call(state='attached', timeout=30_000)
        self.assertEqual(child.conversation_id, 'child')

    def test_create_subagent_emulates_focused_active_page_with_cdp(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/child'
        page.locator.return_value.last.wait_for.return_value = None
        page.wait_for_url.return_value = None
        cdp_session = mock.Mock()
        context = mock.Mock()
        context.new_page.return_value = page
        context.new_cdp_session.return_value = cdp_session
        page.context = context
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

        context.new_cdp_session.assert_called_once_with(page)
        self.assertEqual(
            cdp_session.send.call_args_list,
            [
                mock.call('Emulation.setFocusEmulationEnabled', {'enabled': True}),
                mock.call('Page.setWebLifecycleState', {'state': 'active'}),
            ],
        )
        self.assertIs(child.cdp_session, cdp_session)

    def test_archive_conversation_uses_top_right_archive_menu_and_waits_for_navigation(self) -> None:
        interface = web.ChatGPTWeb()
        more_button = mock.Mock()
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        archive = mock.Mock()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = more
        page.get_by_role.return_value = archive
        session = web.WebSession('conversation-123', 'Child', page)

        interface.archive_conversation(session)

        page.locator.assert_called_once_with('button[data-testid="conversation-options-button"]')
        more_button.wait_for.assert_called_once_with(state='visible', timeout=5_000)
        more_button.evaluate.assert_called_once_with('element => element.click()')
        page.get_by_role.assert_called_once_with('menuitem', name='Archive', exact=True)
        archive.wait_for.assert_called_once_with(state='visible', timeout=5_000)
        archive.evaluate.assert_called_once_with('element => element.click()')
        predicate = page.wait_for_url.call_args.args[0]
        self.assertFalse(predicate('https' + '://chatgpt.com/c/conversation-123'))
        self.assertTrue(predicate('https' + '://chatgpt.com/'))
        self.assertEqual(page.wait_for_url.call_args.kwargs, {'timeout': 10_000})

    def test_archive_conversation_raises_when_options_button_is_missing(self) -> None:
        interface = web.ChatGPTWeb()
        more = mock.Mock()
        more.count.return_value = 0
        page = mock.Mock()
        page.locator.return_value = more
        session = web.WebSession('conversation-123', 'Child', page)

        with self.assertRaisesRegex(RuntimeError, 'conversation options button not found'):
            interface.archive_conversation(session)

    def test_archive_conversation_raises_when_archive_does_not_navigate_away(self) -> None:
        interface = web.ChatGPTWeb()
        more_button = mock.Mock()
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        archive = mock.Mock()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = more
        page.get_by_role.return_value = archive
        page.wait_for_url.side_effect = RuntimeError('timed out')
        session = web.WebSession('conversation-123', 'Child', page)

        with self.assertRaisesRegex(RuntimeError, 'archive did not navigate away'):
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

    def test_is_generating_uses_stop_button(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        stop = mock.Mock()
        stop.count.return_value = 1
        stop.last = stop
        stop.is_visible.return_value = True
        page.locator.return_value = stop
        session = web.WebSession('a', 'A', page)

        self.assertTrue(interface.is_generating(session))
        page.locator.assert_called_once_with(web.STOP_SELECTOR)
        page.get_by_role.assert_not_called()

    def test_is_generating_falls_back_to_accessible_stop_button(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        missing = mock.Mock()
        missing.count.return_value = 0
        fallback = mock.Mock()
        fallback.count.return_value = 1
        fallback.last = fallback
        fallback.is_visible.return_value = True
        page.locator.return_value = missing
        page.get_by_role.return_value = fallback
        session = web.WebSession('a', 'A', page)

        self.assertTrue(interface.is_generating(session))
        page.get_by_role.assert_called_once_with('button', name='Stop answering', exact=True)

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

    def test_invalid_json_candidate_reports_json_shaped_parse_error(self) -> None:
        interface = web.ChatGPTWeb()
        session = web.WebSession(
            'a',
            'A',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=['{"id":"x","tool":"status",}'],
                message_id='m1',
            ),
        )

        first = interface.invalid_json_candidate(session)
        self.assertIsNotNone(first)
        source, detail, fingerprint = first
        self.assertEqual(source, '{"id":"x","tool":"status",}')
        self.assertIn('line 1', detail)
        self.assertTrue(fingerprint)

        session.page.message_id = 'm2'
        second = interface.invalid_json_candidate(session)
        self.assertIsNotNone(second)
        self.assertNotEqual(fingerprint, second[2])

    def test_invalid_request_candidate_reports_semantic_validation_error(self) -> None:
        interface = web.ChatGPTWeb()
        source = '{"session":"root","calls":[{"id":"x","tool":"script","script":"echo hi"}]}'
        session = web.WebSession(
            'a',
            'A',
            FakePage('https' + '://chatgpt.com/c/a', code=[source], message_id='m1'),
        )

        candidate = interface.invalid_request_candidate(
            session,
            lambda request: (_ for _ in ()).throw(ValueError("call 0 has unsupported tool 'script'")),
        )

        self.assertIsNotNone(candidate)
        parsed_source, detail, fingerprint = candidate
        self.assertEqual(parsed_source, source)
        self.assertEqual(detail, "call 0 has unsupported tool 'script'")
        self.assertTrue(fingerprint)

    def test_invalid_json_candidate_ignores_non_json_code_and_valid_json(self) -> None:
        interface = web.ChatGPTWeb()
        prose_code = web.WebSession(
            'a',
            'A',
            FakePage('https' + '://chatgpt.com/c/a', code=['print("hello")']),
        )
        valid_json = web.WebSession(
            'b',
            'B',
            FakePage('https' + '://chatgpt.com/c/b', code=['{"not":"a tool call"}']),
        )
        malformed_non_tool_json = web.WebSession(
            'c',
            'C',
            FakePage('https' + '://chatgpt.com/c/c', code=['{"example": 1,}']),
        )

        self.assertIsNone(interface.invalid_json_candidate(prose_code))
        self.assertIsNone(interface.invalid_json_candidate(valid_json))
        self.assertIsNone(interface.invalid_json_candidate(malformed_non_tool_json))

    def test_latest_message_is_assistant_uses_actual_final_conversation_turn(self) -> None:
        interface = web.ChatGPTWeb()
        assistant_last = web.WebSession(
            'a',
            'A',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                message_roles=['user', 'assistant'],
            ),
        )
        user_last = web.WebSession(
            'b',
            'B',
            FakePage(
                'https' + '://chatgpt.com/c/b',
                message_roles=['assistant', 'user'],
            ),
        )

        self.assertTrue(interface.latest_message_is_assistant(assistant_last))
        self.assertFalse(interface.latest_message_is_assistant(user_last))

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

    def test_recent_valid_request_is_bounded_to_last_ten_assistant_messages(self) -> None:
        interface = web.ChatGPTWeb()
        newer_prose = [f'newer prose {index}' for index in range(10)]
        session = web.WebSession(
            'a',
            'A',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=[
                    '{"session":"root","id":"old","tool":"status"}',
                    *newer_prose,
                ],
                message_id=[f'm{index}' for index in range(11)],
            ),
        )

        self.assertIsNone(interface.recent_valid_request(session, lambda request: None))

    def test_refresh_ignores_page_that_navigates_away_after_enumeration(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/')
        interface.pages.return_value = [page]
        interface.describe_page.side_effect = RuntimeError(
            'not a ChatGPT conversation URL: https://chatgpt.com/'
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        existing = web.WebSession('a', 'Cats', page)
        watcher.sessions['a'] = existing

        watcher.refresh_sessions()

        self.assertEqual(watcher.sessions, {})
        interface.instrument_conversations_responses.assert_called_once_with(page)
        interface.latest_too_long_error_id.assert_not_called()

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

    def test_new_session_replays_final_assistant_toolcall_after_restart(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        recovered = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'visible-toolcall',
        )
        interface.recent_valid_request.return_value = recovered
        interface.latest_message_is_assistant.return_value = True
        interface.valid_request.return_value = recovered
        execute = mock.Mock(return_value={'ok': True})
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        watcher.refresh_sessions()

        session = watcher.sessions['a']
        self.assertEqual(session.routing_session_id, 'root')
        self.assertIs(watcher.sessions_by_routing_id['root'], session)
        self.assertEqual(session.seen_fingerprints, set())
        execute.assert_not_called()

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.0))
        execute.assert_called_once_with({'session': 'root', 'tool': 'status'}, announce=True)
        self.assertEqual(session.seen_fingerprints, {'visible-toolcall'})

    def test_new_session_suppresses_recovered_toolcall_when_later_user_turn_exists(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'completed-toolcall',
        )
        interface.latest_message_is_assistant.return_value = False
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        self.assertEqual(watcher.sessions['a'].seen_fingerprints, {'completed-toolcall'})
        interface.valid_request.assert_not_called()

    def test_new_session_suppresses_older_toolcall_when_latest_assistant_turn_is_prose(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'older-toolcall',
        )
        interface.latest_message_is_assistant.return_value = True
        interface.valid_request.return_value = None
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        self.assertEqual(watcher.sessions['a'].seen_fingerprints, {'older-toolcall'})

    def test_ensure_root_session_reuses_chat_with_recent_root_toolcall(self) -> None:
        interface = mock.Mock()
        existing = web.WebSession('a', 'Existing root', mock.Mock())
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'recent-root-toolcall',
        )
        interface.latest_message_is_assistant.return_value = True
        interface.valid_request.return_value = (
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
        self.assertEqual(existing.seen_fingerprints, set())
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

    def test_restart_binding_restores_active_emulation_for_recovered_root(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/root-chat', title='Root')
        cdp_session = mock.Mock()
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('root-chat', 'Root')
        interface.latest_too_long_error_id.return_value = None
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root', 'tool': 'status'},
            'root-toolcall',
        )
        interface.latest_message_is_assistant.return_value = False
        interface.emulate_active_page.return_value = cdp_session
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        root = watcher.sessions['root-chat']
        self.assertEqual(root.routing_session_id, 'root')
        self.assertIs(root.cdp_session, cdp_session)
        interface.emulate_active_page.assert_called_once_with(page)

    def test_restart_binding_restores_active_emulation_for_recovered_subagent(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/child', title='Child')
        cdp_session = mock.Mock()
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('child', 'Child')
        interface.latest_too_long_error_id.return_value = None
        interface.recent_valid_request.return_value = (
            '{}',
            {'session': 'root::worker', 'tool': 'status'},
            'child-toolcall',
        )
        interface.latest_message_is_assistant.return_value = False
        interface.emulate_active_page.return_value = cdp_session
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        child = watcher.sessions['child']
        self.assertEqual(child.routing_session_id, 'root::worker')
        self.assertIs(child.cdp_session, cdp_session)
        interface.emulate_active_page.assert_called_once_with(page)

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
            prompt=(
                'Investigate this\n\n---\n\nBOOTSTRAP root::worker2\n\n---\n\n'
                + web.SUBAGENT_COMPLETION_INSTRUCTIONS
            ),
        )
        prompt = interface.create_conversation.call_args.kwargs['prompt']
        self.assertIn('not complete until you call the `handoff` tool', prompt)
        self.assertIn('Do not finish with a prose-only response', prompt)
        self.assertIn('make `handoff` your final tool call', prompt)

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

    def test_retire_subagent_detaches_cdp_session_before_closing_page(self) -> None:
        interface = mock.Mock()
        cdp_session = mock.Mock()
        page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            page,
            routing_session_id='root::worker',
            cdp_session=cdp_session,
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.sessions = {'child-c': child}
        watcher.sessions_by_routing_id = {'root::worker': child}

        watcher.retire_subagent(child)

        interface.archive_conversation.assert_called_once_with(child)
        cdp_session.detach.assert_called_once_with()
        page.close.assert_called_once_with()
        self.assertIsNone(child.cdp_session)
        self.assertIsNone(child.routing_session_id)
        self.assertNotIn('child-c', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

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

    def test_foreign_session_invalid_request_is_inert(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = (
            '{"session":"other-root","tool":"script"}',
            'web tool request is for a different PGC session',
            'foreign-fp',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        with mock.patch('builtins.print') as printed:
            self.assertFalse(watcher.scan_session(session, 0.0))

        printed.assert_not_called()
        self.assertEqual(session.pending_responses, [])
        self.assertEqual(session.seen_fingerprints, set())
        self.assertIsNone(session.settling_fingerprint)

    def test_current_session_descendant_invalid_request_is_diagnosed(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = (
            '{"session":"root::worker","tool":"script"}',
            "call 0 has unsupported tool 'script'",
            'descendant-fp',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        with mock.patch('builtins.print') as printed:
            self.assertFalse(watcher.scan_session(session, 0.0))
            self.assertTrue(watcher.scan_session(session, 0.0))

        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn("invalid tool call: call 0 has unsupported tool 'script'", output)
        self.assertEqual(len(session.pending_responses), 1)

    def test_invalid_request_is_printed_and_sent_back_once_after_settling(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = (
            '{"tool":"script"}',
            "call 0 has unsupported tool 'script'",
            'bad-request-fp',
        )
        execute = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.35,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        with mock.patch('builtins.print') as printed:
            self.assertTrue(watcher.scan_session(session, 0.35))

        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn("invalid tool call: call 0 has unsupported tool 'script'", output)
        self.assertEqual(len(session.pending_responses), 1)
        self.assertIn('request was invalid', session.pending_responses[0].result)
        self.assertIn("unsupported tool 'script'", session.pending_responses[0].result)
        self.assertEqual(session.seen_fingerprints, {'bad-request-fp'})
        execute.assert_not_called()

        self.assertFalse(watcher.scan_session(session, 1.0))
        self.assertEqual(len(session.pending_responses), 1)

    def test_invalid_json_is_ignored_while_assistant_is_generating(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.return_value = None
        interface.is_generating.side_effect = [True, False, False]
        interface.invalid_json_candidate.return_value = (
            '{"tool":"status",}',
            'Expecting property name enclosed in double quotes at line 1, column 18',
            'bad-json-fp',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.35,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        interface.invalid_json_candidate.assert_not_called()
        self.assertFalse(watcher.scan_session(session, 1.0))
        self.assertTrue(watcher.scan_session(session, 1.35))
        self.assertEqual(len(session.pending_responses), 1)
        self.assertIn('JSON was invalid', session.pending_responses[0].result)

    def test_invalid_json_is_sent_back_once_after_settling(self) -> None:
        interface = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.invalid_json_candidate.return_value = (
            '{"tool":"status",}',
            'Expecting property name enclosed in double quotes at line 1, column 18',
            'bad-json-fp',
        )
        execute = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=execute,
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.35,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.35))
        self.assertEqual(len(session.pending_responses), 1)
        self.assertIn("JSON was invalid", session.pending_responses[0].result)
        self.assertIn("Please resend", session.pending_responses[0].result)
        self.assertEqual(session.seen_fingerprints, {'bad-json-fp'})
        execute.assert_not_called()

        self.assertFalse(watcher.scan_session(session, 1.0))
        self.assertEqual(len(session.pending_responses), 1)

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

    def test_rate_limit_suspends_and_restores_active_background_tabs(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.side_effect = [30.0, 20.0, 0.0]
        cdp_session = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock(), cdp_session=cdp_session)
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.sessions = {'a': session}

        self.assertTrue(watcher.sync_rate_limit_suspension())
        self.assertTrue(watcher.sync_rate_limit_suspension())
        self.assertFalse(watcher.sync_rate_limit_suspension())

        # One dismissal attempt when entering cooldown and one after thawing;
        # the middle cooldown tick must not poll the DOM continuously.
        self.assertEqual(interface.dismiss_rate_limit_dialog.call_count, 2)
        self.assertEqual(
            cdp_session.send.call_args_list,
            [
                mock.call('Emulation.setFocusEmulationEnabled', {'enabled': False}),
                mock.call('Page.setWebLifecycleState', {'state': 'frozen'}),
                mock.call('Emulation.setFocusEmulationEnabled', {'enabled': True}),
                mock.call('Page.setWebLifecycleState', {'state': 'active'}),
            ],
        )
        self.assertFalse(watcher.rate_limit_suspended)

    def test_step_does_not_refresh_scan_or_deliver_during_rate_limit_backoff(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.return_value = 30.0
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.refresh_sessions = mock.Mock()

        self.assertFalse(watcher.step())

        watcher.refresh_sessions.assert_not_called()
        interface.valid_request.assert_not_called()
        interface.submit.assert_not_called()

    def test_delivery_timeout_still_fires_during_rate_limit_suspension(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.return_value = 30.0
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK')],
            delivery_failure_started_at=100.0,
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            clock=lambda: 3_700.0,
        )
        watcher.sessions = {'a': session}

        with self.assertRaises(web.DeliveryFailureTimeout):
            watcher.step()

        interface.rate_limit_remaining.assert_not_called()

    def test_run_handles_first_sigint_cooperatively_and_restores_handler(self) -> None:
        interface = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            poll_seconds=0.0,
        )
        watcher.refresh_sessions = mock.Mock()
        previous_handler = object()
        installed_handlers = []

        def install_handler(signum, handler):
            installed_handlers.append((signum, handler))

        def step_once():
            installed_handlers[0][1](web.signal.SIGINT, None)
            return False

        watcher.step = mock.Mock(side_effect=step_once)
        with (
            mock.patch.object(web.signal, 'getsignal', return_value=previous_handler),
            mock.patch.object(web.signal, 'signal', side_effect=install_handler),
            mock.patch('builtins.print'),
        ):
            watcher.run()

        watcher.step.assert_called_once_with()
        self.assertEqual(installed_handlers[0][0], web.signal.SIGINT)
        self.assertEqual(installed_handlers[-1], (web.signal.SIGINT, previous_handler))

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

    def test_terminal_error_summary_omits_playwright_fill_payload(self) -> None:
        payload = 'SECRET-PAYLOAD-' * 500
        exc = RuntimeError(
            'Locator.fill: Timeout 30000ms exceeded.\n'
            'Call log:\n'
            '  - waiting for locator("#prompt-textarea").last\n'
            f'    - fill("{payload}")\n'
            '    - more payload-derived diagnostics'
        )

        summary = web.terminal_error_summary(exc)

        self.assertIn('Locator.fill: Timeout 30000ms exceeded.', summary)
        self.assertIn('waiting for locator("#prompt-textarea").last', summary)
        self.assertIn('fill(<payload omitted>)', summary)
        self.assertNotIn('SECRET-PAYLOAD', summary)
        self.assertNotIn('more payload-derived diagnostics', summary)
        self.assertLessEqual(len(summary), web.TERMINAL_ERROR_MAX_CHARS)

    def test_terminal_error_summary_caps_other_large_errors(self) -> None:
        summary = web.terminal_error_summary(RuntimeError('x' * 5000))
        self.assertLessEqual(len(summary), web.TERMINAL_ERROR_MAX_CHARS)
        self.assertTrue(summary.endswith('...'))

    def test_step_sanitizes_delivery_exception_before_printing(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.return_value = 0.0
        payload = 'SECRET-PAYLOAD-' * 500
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK')],
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            clock=lambda: 1.0,
        )
        watcher.sessions = {'a': session}
        watcher.refresh_sessions = mock.Mock()
        watcher.scan_session = mock.Mock(return_value=False)
        watcher.deliver_session = mock.Mock(side_effect=RuntimeError(
            'Locator.fill: Timeout 30000ms exceeded.\n'
            'Call log:\n'
            f'  - fill("{payload}")\n'
            '  - later diagnostics'
        ))

        with mock.patch('builtins.print') as print_mock:
            watcher.step()

        output = '\n'.join(str(call) for call in print_mock.call_args_list)
        self.assertIn('fill(<payload omitted>)', output)
        self.assertNotIn('SECRET-PAYLOAD', output)
        self.assertNotIn('later diagnostics', output)

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
        self.assertIsNone(a.delivery_failure_started_at)
        progress.set_status.assert_called_with('[sent]')

    def test_delivery_backoff_caps_without_huge_integer_float_conversion(self) -> None:
        interface = mock.Mock()
        interface.submit.side_effect = RuntimeError('busy')
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK')],
            delivery_attempt=100_000,
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        self.assertFalse(watcher.deliver_session(session, 10.0))
        self.assertEqual(session.next_delivery_at, 18.0)
        self.assertEqual(session.delivery_attempt, 100_001)
        self.assertEqual(session.delivery_failure_started_at, 10.0)

    def test_delivery_timeout_fires_at_one_hour_even_while_backoff_is_waiting(self) -> None:
        progress = mock.Mock(spec=web.ResponseProgress)
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK', progress=progress)],
            next_delivery_at=3_708.0,
            delivery_failure_started_at=100.0,
        )
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        with self.assertRaisesRegex(
            web.DeliveryFailureTimeout,
            'unable to deliver tool results for 1 hour',
        ):
            watcher.deliver_session(session, 3_700.0)
        progress.set_status.assert_called_with('Delivery failed for 1 hour; stopping PGC')

    def test_unexpected_delivery_exception_uses_retry_and_eventually_times_out(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.return_value = 0.0
        interface.submit.side_effect = OverflowError('int too large to convert to float')
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK')],
        )
        clock = mock.Mock(side_effect=[100.0, 3_700.0])
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            clock=clock,
        )
        watcher.sessions = {'a': session}
        watcher.refresh_sessions = mock.Mock()
        watcher.recover_rejected_submission = mock.Mock(return_value=False)
        watcher.scan_session = mock.Mock(return_value=False)

        with mock.patch('builtins.print'):
            self.assertFalse(watcher.step())
        self.assertEqual(session.delivery_failure_started_at, 100.0)
        self.assertEqual(session.next_delivery_at, 100.25)

        with self.assertRaises(web.DeliveryFailureTimeout):
            watcher.step()

    def test_run_stops_cleanly_on_delivery_timeout_and_restores_sigint_handler(self) -> None:
        watcher = web.WebSessionWatcher(
            mock.Mock(),
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            poll_seconds=0.0,
        )
        watcher.refresh_sessions = mock.Mock()
        watcher.step = mock.Mock(
            side_effect=web.DeliveryFailureTimeout(
                'Cats: unable to deliver tool results for 1 hour; stopping PGC'
            )
        )
        previous_handler = object()
        installed_handlers = []

        def install_handler(signum, handler):
            installed_handlers.append((signum, handler))

        with (
            mock.patch.object(web.signal, 'getsignal', return_value=previous_handler),
            mock.patch.object(web.signal, 'signal', side_effect=install_handler),
            mock.patch('builtins.print') as printed,
        ):
            watcher.run()

        output = '\n'.join(str(call.args[0]) for call in printed.call_args_list)
        self.assertIn('unable to deliver tool results for 1 hour; stopping PGC', output)
        self.assertIn("Poor Girl's Codex web watcher stopped.", output)
        self.assertEqual(installed_handlers[-1], (web.signal.SIGINT, previous_handler))

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
