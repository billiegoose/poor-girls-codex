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
        message_role_ids=None,
        message_texts=None,
    ):
        self.url = url
        self._title = title
        self.code = code or []
        self.message_id = message_id
        self.message_roles = list(message_roles or [])
        self.message_role_ids = list(message_role_ids or [None] * len(self.message_roles))
        self.message_texts = list(message_texts or [''] * len(self.message_roles))

    def title(self):
        return self._title

    def close(self):
        self.closed = True

    def locator(self, selector):
        if selector == web.ASSISTANT_SELECTOR:
            return FakeLocator(self.code, message_id=self.message_id)
        if selector == '[data-message-author-role]':
            return FakeLocator(
                self.message_texts,
                message_id=self.message_role_ids,
                author_role=self.message_roles,
            )
        if selector == '[data-message-author-role="user"]':
            texts = []
            message_ids = []
            for role, message_id, text in zip(
                self.message_roles,
                self.message_role_ids,
                self.message_texts,
            ):
                if role == 'user':
                    texts.append(text)
                    message_ids.append(message_id)
            return FakeLocator(texts, message_id=message_ids, author_role=['user'] * len(texts))
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

    def test_sidebar_conversations_429_does_not_pause_pgc(self) -> None:
        for origin_param in ('conversation_origin', 'exclude_conversation_origin'):
            with self.subTest(origin_param=origin_param):
                interface = web.ChatGPTWeb()
                page = mock.Mock()
                page.url = 'https' + '://chatgpt.com/c/abcdef123456'
                response = mock.Mock()
                response.url = (
                    'https' + '://chatgpt.com/backend-api/conversations?'
                    'limit=20&offset=0&is_archived=false&is_starred=false&'
                    'order=updated&expand=false&hide_snorlax=false&'
                    f'{origin_param}=tpp'
                )
                response.status = 429
                response.request.method = 'GET'

                interface.dismiss_rate_limit_dialog = mock.Mock(return_value=False)
                with (
                    mock.patch.object(web.time, 'monotonic', return_value=100.0),
                    mock.patch('builtins.print') as printed,
                ):
                    interface.log_conversations_response(page, response)

                interface.dismiss_rate_limit_dialog.assert_called_once_with(page)
                printed.assert_not_called()
                self.assertEqual(interface.rate_limit_remaining(100.0), 0.0)
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
        url = 'https' + '://chatgpt.com/backend-api/conversations'

        with mock.patch.object(web.time, 'monotonic', return_value=100.0):
            interface.note_rate_limit(method='GET', url=url)
        self.assertEqual(interface.rate_limit_remaining(100.0), 60.0)

        # More 429s from the same burst must not extend or exponentiate the pause.
        with mock.patch.object(web.time, 'monotonic', return_value=110.0):
            interface.note_rate_limit(method='GET', url=url)
        self.assertEqual(interface.rate_limit_remaining(110.0), 50.0)

        # If traffic is still rejected exactly when the pause expires, back off harder.
        with mock.patch.object(web.time, 'monotonic', return_value=160.0):
            interface.note_rate_limit(method='GET', url=url)
        self.assertEqual(interface.rate_limit_remaining(160.0), 120.0)

    def test_rate_limit_backoff_decays_gradually_from_max_after_quiet_time(self) -> None:
        interface = web.ChatGPTWeb()
        url = 'https' + '://chatgpt.com/backend-api/conversations'

        # Repeated failures at each pause boundary reach the 300s ceiling.
        for now, expected in ((100.0, 60.0), (160.0, 120.0), (280.0, 240.0), (520.0, 300.0)):
            with mock.patch.object(web.time, 'monotonic', return_value=now):
                interface.note_rate_limit(method='GET', url=url)
            self.assertEqual(interface.rate_limit_remaining(now), expected)

        # The 300s pause ends at t=820. After 7.5 quiet minutes, the next pause has
        # decayed halfway from 300s back toward the 60s initial value: 180s.
        with mock.patch.object(web.time, 'monotonic', return_value=1270.0):
            interface.note_rate_limit(method='GET', url=url)
        self.assertEqual(interface.rate_limit_remaining(1270.0), 180.0)

        # That 180s pause ends at t=1450. After 15 quiet minutes, recovery is
        # complete and the next pause is back to the initial 60s.
        with mock.patch.object(web.time, 'monotonic', return_value=2350.0):
            interface.note_rate_limit(method='GET', url=url)
        self.assertEqual(interface.rate_limit_remaining(2350.0), 60.0)

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

    def test_refresh_sessions_does_not_re_read_title_for_existing_session(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Renamed')
        interface.pages.return_value = [page]
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        watcher.sessions['a'] = web.WebSession('a', 'Cached title', page)

        watcher.refresh_sessions()

        self.assertEqual(watcher.sessions['a'].label, 'Cached title')
        interface.describe_page.assert_not_called()

    def test_create_conversation_uses_plain_chatgpt_root_url(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/child'
        present = mock.Mock()
        present.count.return_value = 1
        present.last.wait_for.return_value = None
        page.locator.return_value = present
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
        page.goto.assert_called_once_with('https' + '://chatgpt.com/', wait_until='commit')
        self.assertLess(
            page.method_calls.index(mock.call.on('response', mock.ANY)),
            page.method_calls.index(mock.call.goto('https' + '://chatgpt.com/', wait_until='commit')),
        )
        page.locator.assert_any_call(web.ASSISTANT_START_SELECTOR)
        page.locator.return_value.last.wait_for.assert_any_call(state='attached', timeout=30_000)
        self.assertEqual(child.conversation_id, 'child')

    def test_create_subagent_emulates_focused_active_page_with_cdp(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/child'
        present = mock.Mock()
        present.count.return_value = 1
        present.last.wait_for.return_value = None
        page.locator.return_value = present
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
        page.get_by_role.return_value = more
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

    def test_set_composer_text_does_not_focus_when_background_fill_works(self) -> None:
        interface = web.ChatGPTWeb()
        composer_element = mock.Mock()
        composer = mock.Mock()
        composer.count.return_value = 1
        composer.last = composer_element
        page = mock.Mock()
        session = web.WebSession('a', 'Background', page)
        interface.composer = mock.Mock(return_value=composer)
        interface.emulate_active_page = mock.Mock(return_value=mock.Mock())

        interface.set_composer_text(session, 'result')

        page.bring_to_front.assert_not_called()
        composer_element.fill.assert_called_once_with('result')
        interface.emulate_active_page.assert_called_once_with(page)

    def test_set_composer_text_focuses_and_retries_only_after_background_fill_fails(self) -> None:
        interface = web.ChatGPTWeb()
        composer_element = mock.Mock()
        composer_element.fill.side_effect = [RuntimeError('background fill failed'), None]
        composer = mock.Mock()
        composer.count.return_value = 1
        composer.last = composer_element
        page = mock.Mock()
        session = web.WebSession('a', 'Background', page)
        interface.composer = mock.Mock(return_value=composer)
        interface.emulate_active_page = mock.Mock(return_value=mock.Mock())

        interface.set_composer_text(session, 'result')

        page.bring_to_front.assert_called_once_with()
        self.assertEqual(composer_element.fill.call_args_list, [mock.call('result'), mock.call('result')])

    def test_set_composer_text_focuses_only_after_background_composer_is_missing(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        composer_element = mock.Mock()
        present = mock.Mock()
        present.count.return_value = 1
        present.last = composer_element
        page = mock.Mock()
        session = web.WebSession('a', 'Background', page)
        interface.composer = mock.Mock(side_effect=[missing, present])
        interface.emulate_active_page = mock.Mock(return_value=mock.Mock())

        interface.set_composer_text(session, 'result')

        page.bring_to_front.assert_called_once_with()
        composer_element.fill.assert_called_once_with('result')

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
        interface.composer_text = mock.Mock(return_value=None)

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

    def test_submit_refills_once_when_new_dom_hydration_restores_stale_draft(self) -> None:
        interface = web.ChatGPTWeb()
        disabled_button = mock.Mock()
        disabled_button.is_enabled.return_value = False
        disabled = mock.Mock()
        disabled.count.return_value = 1
        disabled.last = disabled_button
        enabled_button = mock.Mock()
        enabled_button.is_enabled.return_value = True
        enabled = mock.Mock()
        enabled.count.return_value = 1
        enabled.last = enabled_button
        page = mock.Mock()
        session = web.WebSession('a', 'Hydrating', page)
        interface.set_composer_text = mock.Mock()
        interface.composer_text = mock.Mock(side_effect=['stale draft', 'result'])
        interface.send_button = mock.Mock(side_effect=[disabled, enabled])

        interface.submit(session, 'result')

        self.assertEqual(
            interface.set_composer_text.call_args_list,
            [mock.call(session, 'result'), mock.call(session, 'result')],
        )
        enabled_button.evaluate.assert_called_once_with('element => element.click()')
        page.bring_to_front.assert_not_called()

    def test_latest_too_long_error_id_uses_assistant_message_id_when_error_is_in_message(self) -> None:
        interface = web.ChatGPTWeb()
        match = mock.Mock()
        match.is_visible.return_value = True
        match.evaluate.return_value = 'assistant-message-id'
        matches = mock.Mock()
        matches.count.return_value = 1
        matches.nth.return_value = match
        page = mock.Mock()
        page.get_by_text.return_value = matches
        session = web.WebSession('a', 'Cats', page)

        self.assertEqual(
            interface.latest_too_long_error_id(session),
            'assistant:assistant-message-id',
        )
        interface.latest_user_message = mock.Mock()
        interface.latest_user_message.assert_not_called()

    def test_latest_too_long_error_id_uses_latest_user_for_standalone_banner(self) -> None:
        interface = web.ChatGPTWeb()
        match = mock.Mock()
        match.is_visible.return_value = True
        match.evaluate.return_value = None
        matches = mock.Mock()
        matches.count.return_value = 1
        matches.nth.return_value = match
        page = mock.Mock()
        page.get_by_text.return_value = matches
        session = web.WebSession('a', 'Cats', page)
        interface.latest_user_message = mock.Mock(return_value=('failed-user-message-id', 'payload'))

        self.assertEqual(
            interface.latest_too_long_error_id(session),
            'user:failed-user-message-id',
        )
        interface.latest_user_message.assert_called_once_with(session)

    def test_latest_too_long_error_id_has_stable_fallback_for_standalone_banner_without_user_id(self) -> None:
        interface = web.ChatGPTWeb()
        match = mock.Mock()
        match.is_visible.return_value = True
        match.evaluate.return_value = None
        matches = mock.Mock()
        matches.count.return_value = 1
        matches.nth.return_value = match
        page = mock.Mock()
        page.get_by_text.return_value = matches
        session = web.WebSession('a', 'Cats', page)
        interface.latest_user_message = mock.Mock(return_value=None)

        self.assertEqual(
            interface.latest_too_long_error_id(session),
            'visible-too-long-banner',
        )

    def test_disabled_send_without_explicit_error_is_not_called_message_too_long(self) -> None:
        interface = web.ChatGPTWeb()
        disabled_button = mock.Mock()
        disabled_button.is_enabled.return_value = False
        disabled = mock.Mock()
        disabled.count.return_value = 1
        disabled.last = disabled_button
        no_error = mock.Mock()
        no_error.count.return_value = 0
        page = mock.Mock()
        page.get_by_text.return_value = no_error
        session = web.WebSession('a', 'Disabled', page)
        interface.set_composer_text = mock.Mock()
        interface.composer_text = mock.Mock(return_value='result')
        interface.send_button = mock.Mock(return_value=disabled)

        with mock.patch.object(web, 'SUBMIT_SETTLE_TIMEOUT_SECONDS', 0.0):
            with self.assertRaisesRegex(RuntimeError, 'send button is not available'):
                interface.submit(session, 'result')

        page.get_by_text.assert_called_once_with(web.MESSAGE_TOO_LONG_TEXT, exact=True)

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

    def test_scan_session_reads_latest_assistant_once_for_valid_and_invalid_classification(self) -> None:
        interface = web.ChatGPTWeb()
        page = mock.Mock()
        assistant_messages = mock.Mock()
        message = mock.Mock()
        assistant_messages.count.return_value = 1
        assistant_messages.last = message
        message.inner_text.return_value = '{"session":"root","tool":"not-real"}'
        message.locator.return_value = FakeLocator([])
        page.locator.side_effect = lambda selector: (
            assistant_messages if selector == web.ASSISTANT_SELECTOR else FakeLocator([])
        )
        interface.json_candidates_for_message = mock.Mock(return_value=(
            'assistant-1',
            ['{"session":"root","tool":"not-real"}'],
        ))
        interface.is_generating = mock.Mock(return_value=False)
        session = web.WebSession('a', 'Cats', page)
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: (_ for _ in ()).throw(ValueError('unsupported tool')),
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.35,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))

        interface.json_candidates_for_message.assert_called_once_with(message)
        self.assertEqual(session.settling_fingerprint is not None, True)

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

    def test_normalize_message_text_matches_fenced_submit_to_rendered_code_block(self) -> None:
        interface = web.ChatGPTWeb()

        submitted = '```\n[\n  {"id": "status", "ok": true}\n]\n```\n'
        rendered = '[\n\u00a0 {"id": "status", "ok": true}\n]'

        self.assertEqual(
            interface.normalize_message_text(submitted),
            interface.normalize_message_text(rendered),
        )

    def test_terminal_response_candidate_requires_tool_result_user_turn_immediately_before_assistant(self) -> None:
        interface = web.ChatGPTWeb()
        session = web.WebSession(
            'a',
            'Cats',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=['Finished normally.'],
                message_id='assistant-final',
                message_roles=['user', 'assistant', 'user', 'assistant'],
                message_role_ids=['user-1', 'assistant-1', 'tool-user', 'assistant-final'],
            ),
        )

        candidate = interface.terminal_response_candidate(session, 'tool-user')

        self.assertIsNotNone(candidate)
        text, fingerprint = candidate
        self.assertEqual(text, 'Finished normally.')
        self.assertTrue(fingerprint)
        self.assertIsNone(interface.terminal_response_candidate(session, 'other-user'))

    def test_terminal_response_from_snapshot_requires_same_rendered_assistant(self) -> None:
        interface = web.ChatGPTWeb()
        session = web.WebSession(
            'a',
            'Cats',
            FakePage(
                'https' + '://chatgpt.com/c/a',
                code=['Finished normally.'],
                message_id='assistant-final',
                message_roles=['user', 'assistant', 'user', 'assistant'],
                message_role_ids=['user-1', 'assistant-1', 'tool-user', 'assistant-final'],
            ),
        )
        snapshot = web.AssistantSnapshot('assistant-final', (), 'Finished normally.')

        candidate = interface.terminal_response_from_snapshot(session, snapshot, 'tool-user')

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate[0], 'Finished normally.')
        stale = web.AssistantSnapshot('assistant-stale', (), 'Stale response.')
        self.assertIsNone(interface.terminal_response_from_snapshot(session, stale, 'tool-user'))

    def test_latest_user_message_queries_user_turns_directly(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/a',
            message_roles=['user', 'assistant', 'user', 'assistant'],
            message_role_ids=['user-1', 'assistant-1', 'user-2', 'assistant-2'],
        )
        session = web.WebSession('a', 'A', page)

        self.assertEqual(interface.latest_user_message(session), ('user-2', ''))

    def test_latest_user_message_excludes_collapsible_toggle_text(self) -> None:
        interface = web.ChatGPTWeb()
        toggle = mock.Mock()
        toggle.inner_text.return_value = 'Show more'
        toggles = mock.Mock()
        toggles.count.return_value = 1
        toggles.nth.return_value = toggle
        message = mock.Mock()
        message.get_attribute.return_value = 'tool-user'
        message.inner_text.return_value = 'RESULT\n\nShow more'
        message.locator.return_value = toggles
        messages = mock.Mock()
        messages.count.return_value = 1
        messages.last = message
        page = mock.Mock()
        page.locator.return_value = messages
        session = web.WebSession('a', 'A', page)

        self.assertEqual(interface.latest_user_message(session), ('tool-user', 'RESULT'))
        message.locator.assert_called_once_with(
            'button[data-testid="collapsible-user-message-toggle"]'
        )

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

    def test_binding_routing_session_remembers_attached_conversation_once(self) -> None:
        interface = mock.Mock()
        attached = mock.Mock()
        session = web.WebSession('a', 'Cats', mock.Mock())
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_session_attached=attached,
        )

        watcher.bind_routing_session(session, 'root')

        attached.assert_called_once_with(session)
        self.assertEqual(session.routing_session_id, 'root')

    def test_refresh_does_not_remember_unbound_open_conversation(self) -> None:
        interface = mock.Mock()
        page = FakePage('https' + '://chatgpt.com/c/a', title='Cats')
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('a', 'Cats')
        interface.latest_too_long_error_id.return_value = None
        attached = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            on_session_attached=attached,
        )

        watcher.refresh_sessions(prime_new=False)

        attached.assert_not_called()
        self.assertEqual(watcher.sessions['a'].label, 'Cats')

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

    def test_new_root_conversation_skips_existing_sessions(self) -> None:
        old_page = FakePage('https' + '://chatgpt.com/c/old', title='Older chat')
        new_page = FakePage('https' + '://chatgpt.com/c/fresh', title='New chat')
        created = web.WebSession('fresh', 'New chat', new_page)
        interface = mock.Mock()
        interface.pages.return_value = [old_page]
        interface.describe_page.side_effect = lambda page: (
            web.ChatGPTWeb.conversation_id_for_url(page.url), page.title(),
        )
        interface.latest_too_long_error_id.return_value = None
        interface.create_root_conversation.return_value = created
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            new_root_conversation=True,
        )
        installed_handlers = []

        def install_handler(signum, handler):
            installed_handlers.append((signum, handler))

        def stop_after_startup():
            installed_handlers[0][1](web.signal.SIGINT, None)
            return False

        watcher.step = mock.Mock(side_effect=stop_after_startup)
        with (
            mock.patch.object(web.signal, 'getsignal', return_value=None),
            mock.patch.object(web.signal, 'signal', side_effect=install_handler),
            mock.patch('builtins.print'),
        ):
            watcher.run(bootstrap_prompt='BOOTSTRAP')

        interface.create_root_conversation.assert_called_once_with('BOOTSTRAP')
        interface.open_conversation.assert_not_called()
        interface.recent_valid_request.assert_not_called()
        self.assertEqual(watcher.ignored_conversation_ids, {'old'})
        self.assertEqual(set(watcher.sessions), {'fresh'})
        self.assertIs(watcher.sessions_by_routing_id['root'], created)
        self.assertEqual(created.routing_session_id, 'root')

        # Subsequent polls must not discover or claim the preexisting chat.
        interface.pages.return_value = [old_page, new_page]
        watcher.refresh_sessions()
        self.assertEqual(set(watcher.sessions), {'fresh'})
        self.assertIs(watcher.sessions_by_routing_id['root'], created)

    def test_new_root_conversation_cannot_also_select_saved_root(self) -> None:
        with self.assertRaisesRegex(ValueError, 'cannot select an existing root'):
            web.WebSessionWatcher(
                mock.Mock(),
                validate_request=lambda request: None,
                execute_request=mock.Mock(),
                fenced_result=str,
                too_long_fallback=lambda request, result: 'FALLBACK',
                root_session_id='root',
                root_conversation_id='saved-chat',
                new_root_conversation=True,
            )

    def test_ensure_root_session_opens_saved_conversation_when_tab_is_closed(self) -> None:
        interface = mock.Mock()
        resumed = web.WebSession('saved-chat', 'Saved chat', mock.Mock())
        interface.open_conversation.return_value = resumed
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            root_conversation_id='saved-chat',
        )

        root = watcher.ensure_root_session('BOOTSTRAP')

        self.assertIs(root, resumed)
        interface.open_conversation.assert_called_once_with('saved-chat')
        interface.create_root_conversation.assert_not_called()
        self.assertIs(watcher.sessions['saved-chat'], resumed)
        self.assertEqual(resumed.routing_session_id, 'root')

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

    def test_restart_recovers_finished_subagent_forwards_and_retires_without_expectation(self) -> None:
        interface = web.ChatGPTWeb()
        interface._browser = object()
        parent_page = FakePage('https' + '://chatgpt.com/c/parent', title='Parent')
        child_page = FakePage(
            'https' + '://chatgpt.com/c/child',
            title='Child',
            code=['Finished work.'],
            message_id='assistant-final',
            message_roles=['user', 'assistant', 'user', 'assistant'],
            message_role_ids=['bootstrap', 'request', 'result', 'assistant-final'],
        )
        interface.pages = mock.Mock(return_value=[parent_page, child_page])
        interface.instrument_conversations_responses = mock.Mock()
        interface.describe_page = mock.Mock(
            side_effect=lambda page: (
                ('parent', 'Parent') if page is parent_page else ('child', 'Child')
            )
        )
        interface.latest_too_long_error_id = mock.Mock(return_value=None)
        interface.emulate_active_page = mock.Mock(side_effect=[mock.Mock(), mock.Mock()])
        interface.is_generating = mock.Mock(return_value=False)
        interface.invalid_json_from_snapshot = mock.Mock(return_value=None)
        interface.invalid_request_from_snapshot = mock.Mock(return_value=None)
        interface.archive_conversation = mock.Mock()
        parent_state = web.ConversationProtocolState(routing_session_id='root')
        child_state = web.ConversationProtocolState(
            routing_session_id='root::worker',
            recent_request=(
                '{}',
                {'session': 'root::worker', 'id': 's', 'tool': 'status'},
                'request-fingerprint',
            ),
            request_pending=False,
            terminal_response=('Finished work.', 'terminal-fingerprint'),
        )
        interface.conversation_protocol_state = mock.Mock(
            side_effect=lambda session, validate: (
                parent_state if session.conversation_id == 'parent' else child_state
            )
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        watcher.refresh_sessions()

        parent = watcher.sessions['parent']
        child = watcher.sessions['child']
        self.assertEqual(child.routing_session_id, 'root::worker')
        self.assertIsNone(child.terminal_expectation)
        self.assertIn('request-fingerprint', child.seen_fingerprints)

        self.assertFalse(watcher.scan_session(child, 0.0))
        self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('Finished work.', parent.pending_responses[0].result)
        self.assertIn('completed with terminal response', parent.pending_responses[0].result)
        interface.archive_conversation.assert_called_once_with(child)
        self.assertTrue(child_page.closed)
        self.assertNotIn('child', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

    def test_restart_step_processes_terminal_child_even_when_root_is_also_terminal(self) -> None:
        interface = web.ChatGPTWeb()
        parent_page = FakePage('https' + '://chatgpt.com/c/parent', title='Parent')
        child_page = FakePage('https' + '://chatgpt.com/c/child', title='Child')
        interface.pages = mock.Mock(return_value=[parent_page, child_page])
        interface.instrument_conversations_responses = mock.Mock()
        interface.describe_page = mock.Mock(
            side_effect=lambda page: (
                ('parent', 'Parent') if page is parent_page else ('child', 'Child')
            )
        )
        interface.latest_too_long_error_id = mock.Mock(return_value=None)
        interface.emulate_active_page = mock.Mock(side_effect=[mock.Mock(), mock.Mock()])
        interface.latest_assistant_snapshot = mock.Mock(
            return_value=web.AssistantSnapshot('final', (), 'Finished work.')
        )
        interface.is_generating = mock.Mock(return_value=False)
        interface.invalid_json_from_snapshot = mock.Mock(return_value=None)
        interface.invalid_request_from_snapshot = mock.Mock(return_value=None)
        interface.archive_conversation = mock.Mock()
        root_state = web.ConversationProtocolState(
            routing_session_id='root',
            terminal_response=('Root done.', 'root-terminal'),
        )
        child_state = web.ConversationProtocolState(
            routing_session_id='root::worker',
            terminal_response=('Child done.', 'child-terminal'),
        )
        interface.conversation_protocol_state = mock.Mock(
            side_effect=lambda session, validate: (
                root_state if session.conversation_id == 'parent' else child_state
            )
        )
        root_callback = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=root_callback,
            settle_seconds=0.0,
            clock=lambda: 0.0,
        )

        watcher.refresh_sessions()
        self.assertEqual(
            set(watcher.sessions_by_routing_id),
            {'root', 'root::worker'},
        )

        self.assertFalse(watcher.step())
        self.assertTrue(watcher.step())

        root_callback.assert_called_once()
        parent = watcher.sessions['parent']
        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('Child done.', parent.pending_responses[0].result)
        self.assertNotIn('child', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

    def test_recovered_terminal_ignores_stale_generating_ui_after_settling(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/child',
            code=['Finished work.'],
            message_id='assistant-final',
        )
        session = web.WebSession(
            'child',
            'Child',
            page,
            routing_session_id='root::worker',
            recovered_from_dom=True,
        )
        interface.is_generating = mock.Mock(return_value=True)
        interface.invalid_json_from_snapshot = mock.Mock()
        interface.invalid_request_from_snapshot = mock.Mock()
        interface.conversation_protocol_state = mock.Mock(
            return_value=web.ConversationProtocolState(
                routing_session_id='root::worker',
                terminal_response=('Finished work.', 'terminal-fingerprint'),
            )
        )
        parent = web.WebSession('parent', 'Parent', mock.Mock(), routing_session_id='root')
        interface.archive_conversation = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.35,
        )
        watcher.sessions = {'parent': parent, 'child': session}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': session}

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.35))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('Finished work.', parent.pending_responses[0].result)
        interface.invalid_json_from_snapshot.assert_not_called()
        interface.invalid_request_from_snapshot.assert_not_called()

    def test_recovered_root_still_waits_for_generation_to_finish(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/root',
            code=['Thinking'],
            message_id='assistant-final',
        )
        session = web.WebSession(
            'root',
            'Root',
            page,
            routing_session_id='root',
            recovered_from_dom=True,
        )
        interface.is_generating = mock.Mock(return_value=True)
        interface.conversation_protocol_state = mock.Mock(
            return_value=web.ConversationProtocolState(
                routing_session_id='root',
                terminal_response=('Thinking', 'terminal-fingerprint'),
            )
        )
        callback = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=callback,
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        callback.assert_not_called()
        interface.conversation_protocol_state.assert_not_called()

    def test_live_terminal_still_waits_for_generation_to_finish(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/child',
            code=['Still writing.'],
            message_id='assistant-final',
        )
        session = web.WebSession('child', 'Child', page, routing_session_id='root')
        interface.is_generating = mock.Mock(return_value=True)
        interface.conversation_protocol_state = mock.Mock(
            return_value=web.ConversationProtocolState(
                routing_session_id='root',
                terminal_response=('Still writing.', 'terminal-fingerprint'),
            )
        )
        callback = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=callback,
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        callback.assert_not_called()
        interface.conversation_protocol_state.assert_not_called()

    def test_restart_rejects_transcript_route_outside_root_tree(self) -> None:
        interface = web.ChatGPTWeb()
        interface._browser = object()
        page = FakePage('https' + '://chatgpt.com/c/foreign', title='Foreign')
        interface.pages = mock.Mock(return_value=[page])
        interface.instrument_conversations_responses = mock.Mock()
        interface.describe_page = mock.Mock(return_value=('foreign', 'Foreign'))
        interface.latest_too_long_error_id = mock.Mock(return_value=None)
        interface.conversation_protocol_state = mock.Mock(
            return_value=web.ConversationProtocolState(
                routing_session_id='some-other-root::worker',
                terminal_response=('Done.', 'foreign-terminal'),
            )
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
        )

        watcher.refresh_sessions()

        self.assertIsNone(watcher.sessions['foreign'].routing_session_id)
        self.assertNotIn('some-other-root::worker', watcher.sessions_by_routing_id)

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
        interface.latest_user_message.return_value = ('child-bootstrap-user', 'Investigate this')
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
        self.assertIsNotNone(child.terminal_expectation)
        self.assertEqual(child.terminal_expectation.user_message_id, 'child-bootstrap-user')
        self.assertEqual(child.terminal_expectation.previous_user_message_id, None)
        interface.create_conversation.assert_called_once_with(
            origin=parent,
            label='subagent:worker2',
            prompt=(
                'Investigate this\n\n---\n\nBOOTSTRAP root::worker2\n\n---\n\n'
                + web.SUBAGENT_COMPLETION_INSTRUCTIONS
            ),
        )
        prompt = interface.create_conversation.call_args.kwargs['prompt']
        self.assertIn('finish normally with a concise prose response', prompt)
        self.assertIn('forwards it to your parent', prompt)
        self.assertNotIn('not complete until you call the `handoff` tool', prompt)

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

    def test_seen_previous_toolcall_does_not_cancel_terminal_expectation(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            seen_fingerprints={'previous-tool-fp'},
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = (
            '{}',
            {'tool': 'status'},
            'previous-tool-fp',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        self.assertFalse(watcher.scan_session(session, 0.0))

        self.assertIsNotNone(session.terminal_expectation)
        self.assertEqual(session.terminal_expectation.user_message_id, 'tool-user')

    def test_real_interface_terminal_response_uses_single_assistant_snapshot(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/a',
            code=['Done.'],
            message_id='assistant-final',
            message_roles=['assistant', 'user', 'assistant'],
            message_role_ids=['assistant-request', 'tool-user', 'assistant-final'],
            message_texts=[
                '{"session":"root","id":"s","tool":"status"}',
                '{"id":"s","tool":"status","ok":true}',
                'Done.',
            ],
        )
        session = web.WebSession(
            'a',
            'Cats',
            page,
            routing_session_id='root',
        )
        interface.is_generating = mock.Mock(return_value=False)
        callback = mock.Mock()
        original_snapshot = interface.latest_assistant_snapshot
        interface.latest_assistant_snapshot = mock.Mock(side_effect=original_snapshot)
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=callback,
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.0))

        callback.assert_called_once_with(session, 'Done.')
        self.assertEqual(interface.latest_assistant_snapshot.call_count, 2)
        self.assertIsNone(session.terminal_expectation)

    def test_terminal_response_allows_multiple_assistant_nodes_after_tool_result(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/a',
            code=['Finished work.'],
            message_id='assistant-final',
            message_roles=['user', 'assistant', 'user', 'assistant', 'assistant'],
            message_role_ids=[
                'user-1',
                'assistant-1',
                'tool-user',
                'assistant-preamble',
                'assistant-final',
            ],
        )
        session = web.WebSession('a', 'Cats', page)
        snapshot = interface.latest_assistant_snapshot(session)

        terminal = interface.terminal_response_from_snapshot(
            session,
            snapshot,
            'tool-user',
        )

        self.assertIsNotNone(terminal)
        self.assertEqual(terminal[0], 'Finished work.')

    def test_terminal_response_rejects_newer_user_turn(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/a',
            code=['Not terminal for the old tool result.'],
            message_id='assistant-final',
            message_roles=['user', 'assistant', 'user', 'assistant', 'user', 'assistant'],
            message_role_ids=[
                'user-1',
                'assistant-1',
                'tool-user',
                'assistant-after-tool',
                'new-user',
                'assistant-final',
            ],
        )
        session = web.WebSession('a', 'Cats', page)
        snapshot = interface.latest_assistant_snapshot(session)

        self.assertIsNone(
            interface.terminal_response_from_snapshot(session, snapshot, 'tool-user')
        )

    def test_terminal_response_after_tool_result_prints_complete_without_repair(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            routing_session_id='root',
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('tool-user', 'RESULT\n')
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        interface.terminal_response_candidate.return_value = ('Done.', 'terminal-fp')
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        with mock.patch('builtins.print') as printed:
            self.assertFalse(watcher.scan_session(session, 0.0))
            self.assertTrue(watcher.scan_session(session, 0.0))

        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn('terminal response [complete]', output)
        self.assertIsNone(session.terminal_expectation)
        self.assertEqual(session.seen_fingerprints, {'terminal-fp'})
        self.assertEqual(
            interface.invalid_json_candidate.call_args_list,
            [mock.call(session), mock.call(session)],
        )
        self.assertEqual(
            interface.invalid_request_candidate.call_args_list,
            [
                mock.call(session, watcher.validate_request),
                mock.call(session, watcher.validate_request),
            ],
        )

    def test_root_terminal_response_invokes_completion_callback_once(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            routing_session_id='root',
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('tool-user', 'RESULT\n')
        interface.terminal_response_candidate.return_value = ('Done.', 'root-terminal-fp')
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        callback = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=callback,
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.0))
        self.assertFalse(watcher.scan_session(session, 1.0))

        callback.assert_called_once_with(session, 'Done.')

    def test_root_terminal_notification_failure_does_not_rearm_completion(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            routing_session_id='root',
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('tool-user', 'RESULT\n')
        interface.terminal_response_candidate.return_value = ('Done.', 'root-terminal-fp')
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        callback = mock.Mock(side_effect=RuntimeError('ntfy offline'))
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=callback,
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        with mock.patch('builtins.print') as printed:
            self.assertTrue(watcher.scan_session(session, 0.0))
        self.assertFalse(watcher.scan_session(session, 1.0))

        callback.assert_called_once_with(session, 'Done.')
        output = '\n'.join(str(call) for call in printed.call_args_list)
        self.assertIn('terminal notification error: ntfy offline', output)
        self.assertIsNone(session.terminal_expectation)

    def test_malformed_toolcall_after_tool_result_gets_repair_response(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            routing_session_id='root',
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('tool-user', 'RESULT\n')
        interface.invalid_json_candidate.return_value = (
            '{"tool":"status",}',
            'bad json',
            'invalid-fp',
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertTrue(watcher.scan_session(session, 0.0))

        self.assertEqual(session.seen_fingerprints, {'invalid-fp'})
        self.assertIsNone(session.terminal_expectation)
        self.assertEqual(len(session.pending_responses), 1)
        self.assertFalse(session.pending_responses[0].arms_terminal)
        self.assertIn("couldn't execute that tool call", session.pending_responses[0].result)
        interface.terminal_response_candidate.assert_not_called()

        self.assertTrue(watcher.deliver_session(session, 1.0))
        self.assertIsNone(session.terminal_expectation)

    def test_terminal_expectation_resolves_fenced_submit_against_unfenced_rendered_user_turn(self) -> None:
        interface = mock.Mock()
        interface.normalize_message_text.side_effect = web.ChatGPTWeb.normalize_message_text
        interface.latest_user_message.return_value = ('tool-user', '[\n  {"ok": true}\n]')
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            terminal_expectation=web.TerminalExpectation(
                '```\n[\n  {"ok": true}\n]\n```\n',
                'previous-user',
            ),
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        watcher.resolve_terminal_expectation(session)

        self.assertIsNotNone(session.terminal_expectation)
        self.assertEqual(session.terminal_expectation.user_message_id, 'tool-user')

    def test_terminal_expectation_resolves_collapsed_tool_result_without_ui_toggle(self) -> None:
        interface = mock.Mock()
        interface.normalize_message_text.side_effect = web.ChatGPTWeb.normalize_message_text
        interface.latest_user_message.return_value = ('tool-user', '[\n  {"ok": true}\n]')
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            terminal_expectation=web.TerminalExpectation(
                '```\n[\n  {"ok": true}\n]\n```\n',
                'previous-user',
            ),
        )
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )

        watcher.resolve_terminal_expectation(session)

        self.assertIsNotNone(session.terminal_expectation)
        self.assertEqual(session.terminal_expectation.user_message_id, 'tool-user')

    def test_terminal_expectation_survives_constant_rendered_user_count(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            routing_session_id='root',
            pending_responses=[web.PendingResponse('RESULT', 'FALLBACK')],
        )
        interface.latest_user_message.side_effect = [
            ('old-user', 'old prompt'),
            ('tool-user', 'RESULT\n'),
            ('tool-user', 'RESULT\n'),
            ('tool-user', 'RESULT\n'),
        ]
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        interface.terminal_response_candidate.return_value = ('Done.', 'terminal-fp')
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )

        self.assertTrue(watcher.deliver_session(session, 0.0))
        self.assertIsNotNone(session.terminal_expectation)
        self.assertIsNone(session.terminal_expectation.user_message_id)
        self.assertEqual(session.terminal_expectation.previous_user_message_id, 'old-user')

        self.assertFalse(watcher.scan_session(session, 0.0))
        self.assertEqual(session.terminal_expectation.user_message_id, 'tool-user')
        self.assertTrue(watcher.scan_session(session, 0.0))
        self.assertIsNone(session.terminal_expectation)

    def test_intervening_regular_user_turn_cancels_terminal_expectation(self) -> None:
        interface = mock.Mock()
        session = web.WebSession(
            'a',
            'Cats',
            mock.Mock(),
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('human-user', 'hello')
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            settle_seconds=0.0,
        )

        self.assertFalse(watcher.scan_session(session, 0.0))

        self.assertIsNone(session.terminal_expectation)
        interface.terminal_response_candidate.assert_not_called()

    def test_zero_tool_subagent_terminal_response_forwards_to_parent_and_retires(self) -> None:
        interface = web.ChatGPTWeb()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = FakePage(
            'https' + '://chatgpt.com/c/child-c',
            code=['Terminal handoff works.'],
            message_id='assistant-final',
            message_roles=['user', 'assistant'],
            message_role_ids=['child-bootstrap-user', 'assistant-final'],
            message_texts=[
                'Every executable Poor Girl\'s Codex request MUST include the exact top-level field '
                '"session": "root::worker".',
                'Terminal handoff works.',
            ],
        )
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
        )
        interface.is_generating = mock.Mock(return_value=False)
        interface.invalid_json_from_snapshot = mock.Mock(return_value=None)
        interface.invalid_request_from_snapshot = mock.Mock(return_value=None)
        interface.archive_conversation = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: (_ for _ in ()).throw(ValueError('not a tool call')),
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )
        watcher.sessions = {'parent-c': parent, 'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        self.assertFalse(watcher.scan_session(child, 0.0))
        self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('Terminal handoff works.', parent.pending_responses[0].result)
        self.assertIn('completed with terminal response', parent.pending_responses[0].result)
        interface.archive_conversation.assert_called_once_with(child)
        self.assertTrue(child_page.closed)
        self.assertNotIn('child-c', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

    def test_subagent_terminal_response_forwards_to_parent_and_retires(self) -> None:
        interface = mock.Mock()
        parent = web.WebSession('parent-c', 'Parent', mock.Mock(), routing_session_id='root')
        child_page = mock.Mock()
        child = web.WebSession(
            'child-c',
            'Child',
            child_page,
            routing_session_id='root::worker',
            terminal_expectation=web.TerminalExpectation('RESULT\n', 'previous-user', 'tool-user'),
        )
        interface.valid_request.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('tool-user', 'RESULT\n')
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        interface.terminal_response_candidate.return_value = ('Finished work.', 'child-terminal-fp')
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            settle_seconds=0.0,
        )
        watcher.sessions = {'parent-c': parent, 'child-c': child}
        watcher.sessions_by_routing_id = {'root': parent, 'root::worker': child}

        self.assertFalse(watcher.scan_session(child, 0.0))
        self.assertTrue(watcher.scan_session(child, 0.0))

        self.assertEqual(len(parent.pending_responses), 1)
        self.assertIn('Finished work.', parent.pending_responses[0].result)
        self.assertIn('completed with terminal response', parent.pending_responses[0].result)
        interface.archive_conversation.assert_called_once_with(child)
        child_page.close.assert_called_once_with()
        self.assertNotIn('child-c', watcher.sessions)
        self.assertNotIn('root::worker', watcher.sessions_by_routing_id)

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

    def test_rate_limit_suspension_does_not_freeze_browser_tabs(self) -> None:
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

        # One dismissal attempt when entering cooldown and one when leaving it;
        # the middle cooldown tick must not poll the DOM continuously. Rate-limit
        # suspension is purely logical and must not alter Chromium lifecycle/focus.
        self.assertEqual(interface.dismiss_rate_limit_dialog.call_count, 2)
        cdp_session.send.assert_not_called()
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
            callback = mock.Mock()
            attached = mock.Mock()
            web.run_web_watcher(
                cdp_url='http' + '://127.0.0.1:9222',
                session_id='root',
                bootstrap_prompt='BOOTSTRAP',
                validate_request=lambda request: None,
                execute_request=mock.Mock(),
                fenced_result=str,
                too_long_fallback=lambda request, result: 'FALLBACK',
                on_root_terminal=callback,
                root_conversation_id='saved-chat',
                on_session_attached=attached,
            )

        interface.connect.assert_called_once_with()
        interface.close.assert_called_once_with()
        watcher_type.assert_called_once()
        self.assertIs(watcher_type.call_args.kwargs['on_root_terminal'], callback)
        self.assertEqual(watcher_type.call_args.kwargs['root_conversation_id'], 'saved-chat')
        self.assertIs(watcher_type.call_args.kwargs['on_session_attached'], attached)
        watcher.run.assert_called_once_with(bootstrap_prompt='BOOTSTRAP')

    def test_focus_open_root_conversation_focuses_matching_root_tab(self) -> None:
        root_page = FakePage(
            'https' + '://chatgpt.com/c/root-chat',
            title='Root',
            code=['{"session":"root","id":"x","tool":"status"}'],
        )
        other_page = FakePage(
            'https' + '://chatgpt.com/c/other-chat',
            title='Other',
            code=['{"session":"other","id":"x","tool":"status"}'],
        )
        root_page.bring_to_front = mock.Mock()
        other_page.bring_to_front = mock.Mock()
        interface = mock.Mock()
        interface.pages.return_value = [other_page, root_page]
        interface.describe_page.side_effect = [('other-chat', 'Other'), ('root-chat', 'Root')]
        interface.recent_valid_request.side_effect = [
            ('source', {'session': 'other'}, 'fp-other'),
            ('source', {'session': 'root'}, 'fp-root'),
        ]
        with mock.patch.object(web, 'ChatGPTWeb', return_value=interface):
            conversation_id = web.focus_open_root_conversation(
                cdp_url='http' + '://127.0.0.1:9222',
                session_id='root',
                validate_request=lambda request: None,
            )
        self.assertEqual(conversation_id, 'root-chat')
        root_page.bring_to_front.assert_called_once_with()
        other_page.bring_to_front.assert_not_called()
        interface.close.assert_called_once_with()

    def test_focus_open_root_conversation_returns_none_without_match(self) -> None:
        page = FakePage('https' + '://chatgpt.com/c/other-chat', title='Other')
        page.bring_to_front = mock.Mock()
        interface = mock.Mock()
        interface.pages.return_value = [page]
        interface.describe_page.return_value = ('other-chat', 'Other')
        interface.recent_valid_request.return_value = ('source', {'session': 'other'}, 'fp')
        with mock.patch.object(web, 'ChatGPTWeb', return_value=interface):
            conversation_id = web.focus_open_root_conversation(
                cdp_url='http' + '://127.0.0.1:9222',
                session_id='root',
                validate_request=lambda request: None,
            )
        self.assertIsNone(conversation_id)
        page.bring_to_front.assert_not_called()
        interface.close.assert_called_once_with()

    def test_protocol_state_recovers_terminal_after_matching_tool_result(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/child',
            message_roles=['user', 'assistant', 'user', 'assistant'],
            message_role_ids=['bootstrap', 'request', 'result', 'final'],
            message_texts=[
                'Every executable Poor Girl\'s Codex request MUST include the exact top-level field '
                '"session": "root::worker".',
                '{"session":"root::worker","id":"s","tool":"status"}',
                '{"id":"s","tool":"status","ok":true}',
                'Finished work.',
            ],
        )
        session = web.WebSession('child', 'Child', page)

        state = interface.conversation_protocol_state(session, lambda request: None)

        self.assertEqual(state.routing_session_id, 'root::worker')
        self.assertFalse(state.request_pending)
        self.assertIsNotNone(state.terminal_response)
        self.assertEqual(state.terminal_response[0], 'Finished work.')

    def test_protocol_state_does_not_treat_human_reply_as_tool_result(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/child',
            message_roles=['assistant', 'user', 'assistant'],
            message_role_ids=['request', 'human', 'final'],
            message_texts=[
                '{"session":"root::worker","id":"s","tool":"status"}',
                'Actually, do something else.',
                'Okay.',
            ],
        )
        session = web.WebSession('child', 'Child', page)

        state = interface.conversation_protocol_state(session, lambda request: None)

        self.assertTrue(state.request_pending)
        self.assertIsNone(state.terminal_response)

    def test_protocol_state_recovers_zero_tool_subagent_terminal_from_bootstrap(self) -> None:
        interface = web.ChatGPTWeb()
        page = FakePage(
            'https' + '://chatgpt.com/c/child',
            message_roles=['user', 'assistant'],
            message_role_ids=['bootstrap', 'final'],
            message_texts=[
                'assignment\n\nEvery executable Poor Girl\'s Codex request MUST include the exact top-level field '
                '"session": "root::worker".',
                'Terminal handoff works.',
            ],
        )
        session = web.WebSession('child', 'Child', page)

        state = interface.conversation_protocol_state(session, lambda request: None)

        self.assertEqual(state.routing_session_id, 'root::worker')
        self.assertIsNone(state.recent_request)
        self.assertIsNotNone(state.terminal_response)
        self.assertEqual(state.terminal_response[0], 'Terminal handoff works.')

    def test_protocol_state_supports_new_chatgpt_turn_units(self) -> None:
        class NewMessage:
            def __init__(self, role, message_id, text, turn):
                self.role = role
                self.message_id = message_id
                self.text = text
                self.turn = turn

            def inner_text(self):
                return self.text

            def get_attribute(self, name):
                if name == 'data-chatgpt-search-unit-key':
                    suffix = '0:user' if self.role == 'user' else '2:assistant'
                    return f'fallback-turn-{self.turn}:{suffix}'
                if name == 'data-chatgpt-search-message-ids':
                    return self.message_id
                return None

            def locator(self, selector):
                return FakeLocator([])

        class Collection:
            def __init__(self, items):
                self.items = list(items)

            @property
            def last(self):
                return self.items[-1] if self.items else self

            def count(self):
                return len(self.items)

            def nth(self, index):
                return self.items[index]

        class NewPage:
            def __init__(self, messages):
                self.messages = messages

            def locator(self, selector):
                if selector in {
                    web.MESSAGE_SELECTOR,
                    web.USER_SELECTOR,
                    web.ASSISTANT_SELECTOR,
                }:
                    return Collection([])
                if selector == web.NEW_MESSAGE_SELECTOR:
                    return Collection(self.messages)
                if selector == web.NEW_USER_SELECTOR:
                    return Collection([m for m in self.messages if m.role == 'user'])
                if selector == web.NEW_ASSISTANT_SELECTOR:
                    return Collection([m for m in self.messages if m.role == 'assistant'])
                return Collection([])

        messages = [
            NewMessage(
                'user',
                'bootstrap',
                "Every executable Poor Girl's Codex request MUST include the exact top-level field "
                '"session": "root::worker".',
                0,
            ),
            NewMessage(
                'assistant',
                'request',
                'ChatGPT said:\nJSON\n'
                '{"session":"root::worker","id":"s","tool":"status"}',
                0,
            ),
            NewMessage(
                'user',
                'result',
                '{"id":"s","tool":"status","ok":true}',
                1,
            ),
            NewMessage('assistant', 'final', 'ChatGPT said:\nFinished work.', 1),
        ]
        interface = web.ChatGPTWeb()
        session = web.WebSession('child', 'Child', NewPage(messages))

        snapshot = interface.latest_assistant_snapshot(session)
        state = interface.conversation_protocol_state(session, lambda request: None)

        self.assertEqual(snapshot.message_id, 'final')
        self.assertEqual(snapshot.text, 'Finished work.')
        self.assertEqual(interface.latest_user_message(session), ('result', messages[2].text))
        self.assertTrue(interface.latest_message_is_assistant(session))
        self.assertEqual(state.routing_session_id, 'root::worker')
        self.assertIsNotNone(state.recent_request)
        self.assertEqual(state.recent_request[1]['tool'], 'status')
        self.assertFalse(state.request_pending)
        self.assertIsNotNone(state.terminal_response)
        self.assertEqual(state.terminal_response[0], 'Finished work.')

    def test_new_dom_control_fallbacks_use_semantic_labels(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        composer = mock.Mock()
        composer.count.return_value = 1
        send = mock.Mock()
        send.count.return_value = 1
        stop = mock.Mock()
        stop.count.return_value = 1
        page = mock.Mock()

        def locator(selector):
            if selector == web.NEW_COMPOSER_SELECTOR:
                return composer
            return missing

        def by_role(role, *, name, exact=True):
            if role == 'button' and name == 'Send':
                return send
            if role == 'button' and name == 'Stop':
                return stop
            return missing

        page.locator.side_effect = locator
        page.get_by_role.side_effect = by_role

        self.assertIs(interface.composer(page), composer)
        self.assertIs(interface.send_button(page), send)
        self.assertIs(interface.stop_button(page), stop)

    def test_archive_conversation_falls_back_to_new_header_more_button(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        more_button = mock.Mock()
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        archive = mock.Mock()
        archive.count.return_value = 1
        archive.last = archive
        archive.is_visible.return_value = True
        unarchive = mock.Mock()
        unarchive.count.side_effect = [0, 1]
        unarchive.last = unarchive
        unarchive.is_visible.return_value = True
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = missing

        def by_role(role, *, name, exact=True):
            if role == 'button' and name == 'More':
                return more
            if role == 'menuitem' and name == 'Archive':
                return archive
            if role == 'menuitem' and name == 'Unarchive':
                return unarchive
            return missing

        page.get_by_role.side_effect = by_role
        session = web.WebSession('conversation-123', 'Child', page)

        interface.archive_conversation(session)

        page.get_by_role.assert_any_call('button', name='More', exact=True)
        page.get_by_role.assert_any_call('menuitem', name='Archive', exact=True)
        page.get_by_role.assert_any_call('menuitem', name='Unarchive', exact=True)
        self.assertEqual(more_button.wait_for.call_count, 2)
        more_button.wait_for.assert_has_calls([
            mock.call(state='visible', timeout=5_000),
            mock.call(state='visible', timeout=5_000),
        ])
        self.assertGreaterEqual(more_button.click.call_count, 2)
        archive.click.assert_called_once_with()
        page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=30_000)
        page.keyboard.press.assert_called_once_with('Escape')
        page.wait_for_url.assert_not_called()

    def test_new_header_archive_reopens_menu_while_archive_state_hydrates(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        missing.is_visible.return_value = False
        more_button = mock.Mock()
        more_button.get_attribute.return_value = 'false'
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        archive_item = mock.Mock()
        archive_item.is_visible.side_effect = [False, True]
        archive = mock.Mock()
        archive.count.return_value = 1
        archive.last = archive_item
        unarchive_item = mock.Mock()
        unarchive_item.is_visible.side_effect = [False, False, True]
        unarchive = mock.Mock()
        unarchive.count.return_value = 1
        unarchive.last = unarchive_item
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = missing

        def by_role(role, *, name, exact=True):
            if role == 'button' and name == 'More':
                return more
            if role == 'menuitem' and name == 'Archive':
                return archive
            if role == 'menuitem' and name == 'Unarchive':
                return unarchive
            return missing

        page.get_by_role.side_effect = by_role
        session = web.WebSession('conversation-123', 'Child', page)

        interface.archive_conversation(session)

        self.assertGreaterEqual(more_button.click.call_count, 2)
        archive_item.click.assert_called_once_with()
        page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=30_000)
        self.assertGreaterEqual(page.keyboard.press.call_count, 2)

    def test_new_header_archive_is_idempotent_when_already_archived(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        more_button = mock.Mock()
        more_button.get_attribute.return_value = 'false'
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        unarchive_item = mock.Mock()
        unarchive_item.is_visible.return_value = True
        unarchive = mock.Mock()
        unarchive.count.return_value = 1
        unarchive.last = unarchive_item
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = missing

        def by_role(role, *, name, exact=True):
            if role == 'button' and name == 'More':
                return more
            if role == 'menuitem' and name == 'Unarchive':
                return unarchive
            return missing

        page.get_by_role.side_effect = by_role
        session = web.WebSession('conversation-123', 'Child', page)

        interface.archive_conversation(session)

        unarchive_item.evaluate.assert_not_called()
        page.reload.assert_not_called()
        page.keyboard.press.assert_called_once_with('Escape')

    def test_new_header_archive_raises_when_state_does_not_flip_to_unarchive(self) -> None:
        interface = web.ChatGPTWeb()
        missing = mock.Mock()
        missing.count.return_value = 0
        more_button = mock.Mock()
        more_button.get_attribute.return_value = 'false'
        more = mock.Mock()
        more.count.return_value = 1
        more.last = more_button
        archive = mock.Mock()
        archive.count.return_value = 1
        archive.last = archive
        archive.is_visible.return_value = True
        unarchive = mock.Mock()
        unarchive.count.return_value = 0
        page = mock.Mock()
        page.url = 'https' + '://chatgpt.com/c/conversation-123'
        page.locator.return_value = missing

        def by_role(role, *, name, exact=True):
            if role == 'button' and name == 'More':
                return more
            if role == 'menuitem' and name == 'Archive':
                return archive
            if role == 'menuitem' and name == 'Unarchive':
                return unarchive
            return missing

        page.get_by_role.side_effect = by_role
        session = web.WebSession('conversation-123', 'Child', page)

        with self.assertRaisesRegex(RuntimeError, 'archive state was not confirmed'):
            interface.archive_conversation(session)

    def test_conversation_url_filter_rejects_transient_local_chatgpt_id(self) -> None:
        transient = 'https' + '://chatgpt.com/c/local-chatgpt%3A1234'
        self.assertIsNone(web.ChatGPTWeb.conversation_id_for_url(transient))
        self.assertEqual(
            web.ChatGPTWeb.conversation_id_for_url('https' + '://chatgpt.com/c/real-id'),
            'real-id',
        )

    def test_conversation_id_from_reference_accepts_url_or_bare_id(self) -> None:
        self.assertEqual(
            web.conversation_id_from_reference('https' + '://chatgpt.com/c/abc123'),
            'abc123',
        )
        self.assertEqual(web.conversation_id_from_reference('abc123'), 'abc123')
        with self.assertRaises(ValueError):
            web.conversation_id_from_reference('https' + '://chatgpt.com/share/abc123')
        with self.assertRaises(ValueError):
            web.conversation_id_from_reference('https' + '://example.com/c/abc123')
        with self.assertRaises(ValueError):
            web.conversation_id_from_reference('abc123?foo=bar')
        with self.assertRaises(ValueError):
            web.conversation_id_from_reference('abc123#fragment')

    def test_active_conversation_messages_preserves_visible_branch_order_and_text(self) -> None:
        payload = {
            'current_node': 'result',
            'mapping': {
                'root': {'parent': None, 'message': None},
                'user': {
                    'parent': 'root',
                    'message': {
                        'id': 'm-user',
                        'author': {'role': 'user'},
                        'create_time': 1.0,
                        'content': {'content_type': 'text', 'parts': ['hello  ', '\nworld']},
                        'metadata': {'x': 1},
                    },
                },
                'reasoning-recap': {
                    'parent': 'user',
                    'message': {
                        'id': 'm-reasoning',
                        'author': {'role': 'assistant'},
                        'create_time': 1.5,
                        'content': {'content_type': 'reasoning_recap'},
                        'metadata': {'cot_version': 'hidden'},
                    },
                },
                'request': {
                    'parent': 'reasoning-recap',
                    'message': {
                        'id': 'm-request',
                        'author': {'role': 'assistant'},
                        'create_time': 2.0,
                        'content': {
                            'content_type': 'text',
                            'parts': ['```json\n{"tool": nope,}\n```\ntrailing UI text'],
                        },
                        'metadata': {},
                    },
                },
                'result': {
                    'parent': 'request',
                    'message': {
                        'id': 'm-result',
                        'author': {'role': 'user'},
                        'create_time': 3.0,
                        'content': {
                            'content_type': 'text',
                            'parts': ['```\n{"ok":false,"error":"timeout"}\n```'],
                        },
                        'metadata': {'pgc': True},
                    },
                },
                'hidden-regeneration': {
                    'parent': 'user',
                    'message': {
                        'id': 'hidden',
                        'author': {'role': 'assistant'},
                        'create_time': 2.5,
                        'content': {'content_type': 'text', 'parts': ['not on active branch']},
                        'metadata': {},
                    },
                },
            },
        }
        records = web.active_conversation_messages(payload)
        self.assertEqual([record['message_id'] for record in records], ['m-user', 'm-request', 'm-result'])
        self.assertEqual([record['position'] for record in records], [0, 1, 2])
        self.assertEqual(records[0]['text'], 'hello  \nworld')
        self.assertEqual(
            records[1]['text'],
            '```json\n{"tool": nope,}\n```\ntrailing UI text',
        )
        self.assertEqual(records[2]['message_id'], 'm-result')

    def test_export_conversation_supports_text_and_markdown_without_normalizing_raw_text(self) -> None:
        import tempfile
        from pathlib import Path

        payload = {
            'current_node': 'message',
            'mapping': {
                'message': {
                    'parent': None,
                    'message': {
                        'id': 'm1',
                        'author': {'role': 'assistant'},
                        'create_time': 4.0,
                        'content': {
                            'content_type': 'text',
                            'parts': ['first  line\n', '```json\n{bad json}\n```  '],
                        },
                        'metadata': {},
                    },
                }
            },
        }
        interface = mock.Mock()
        interface.fetch_conversation_payload.return_value = payload
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            web, 'ChatGPTWeb', return_value=interface
        ):
            for format_name in ('txt', 'md'):
                output = Path(tmp) / f'conversation.{format_name}'
                web.export_conversation('abc', str(output), format=format_name)
                rendered = output.read_text(encoding='utf-8')
                self.assertIn('first  line\n```json\n{bad json}\n```  ', rendered)

    def test_fetch_conversation_payload_uses_access_token_without_opening_page(self) -> None:
        interface = web.ChatGPTWeb()
        session_response = mock.Mock(ok=True, status=200)
        session_response.json.return_value = {'accessToken': 'secret-token'}
        conversation_response = mock.Mock(ok=True, status=200)
        conversation_response.json.return_value = {'current_node': 'x', 'mapping': {}}
        context = mock.Mock()
        context.request.get.side_effect = [session_response, conversation_response]
        context.new_page = mock.Mock()
        interface._browser = mock.Mock(contexts=[context])

        payload = interface.fetch_conversation_payload('abc')

        self.assertEqual(payload, {'current_node': 'x', 'mapping': {}})
        self.assertEqual(
            context.request.get.call_args_list,
            [
                mock.call('https' + '://chatgpt.com/api/auth/session'),
                mock.call(
                    'https' + '://chatgpt.com/backend-api/conversation/abc',
                    headers={'Authorization': 'Bearer secret-token'},
                ),
            ],
        )
        context.new_page.assert_not_called()

    def test_fetch_conversation_payload_rejects_missing_access_token(self) -> None:
        interface = web.ChatGPTWeb()
        session_response = mock.Mock(ok=True, status=200)
        session_response.json.return_value = {'user': {'id': 'u'}}
        context = mock.Mock()
        context.request.get.return_value = session_response
        interface._browser = mock.Mock(contexts=[context])

        with self.assertRaisesRegex(RuntimeError, 'did not provide an access token'):
            interface.fetch_conversation_payload('abc')

        context.request.get.assert_called_once_with(
            'https' + '://chatgpt.com/api/auth/session'
        )

    def test_fetch_conversation_payload_reports_session_http_failure(self) -> None:
        interface = web.ChatGPTWeb()
        session_response = mock.Mock(ok=False, status=401)
        context = mock.Mock()
        context.request.get.return_value = session_response
        interface._browser = mock.Mock(contexts=[context])

        with self.assertRaisesRegex(RuntimeError, 'session fetch failed with HTTP 401'):
            interface.fetch_conversation_payload('abc')

    def test_export_conversation_writes_compact_jsonl_without_opening_page(self) -> None:
        import json
        import tempfile
        from pathlib import Path

        payload = {
            'current_node': 'message',
            'mapping': {
                'message': {
                    'parent': None,
                    'message': {
                        'id': 'm1',
                        'author': {'role': 'assistant'},
                        'create_time': 4.0,
                        'content': {'content_type': 'text', 'parts': ['raw text']},
                        'metadata': {},
                    },
                }
            },
        }
        interface = mock.Mock()
        interface.fetch_conversation_payload.return_value = payload
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            web, 'ChatGPTWeb', return_value=interface
        ):
            output = str(Path(tmp) / 'conversation.jsonl')
            result = web.export_conversation(
                'https' + '://chatgpt.com/c/abc',
                output,
            )
            records = [json.loads(line) for line in Path(output).read_text(encoding='utf-8').splitlines()]

        self.assertEqual(result['conversation_id'], 'abc')
        self.assertEqual(result['message_count'], 1)
        self.assertEqual(records[0]['text'], 'raw text')
        interface.connect.assert_called_once_with()
        interface.fetch_conversation_payload.assert_called_once_with('abc')
        interface.close.assert_called_once_with()

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

    def test_rejected_submission_skips_dom_search_without_submitted_responses(self) -> None:
        interface = mock.Mock()
        watcher = web.WebSessionWatcher(
            interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(),
            fenced_result=str,
            too_long_fallback=lambda request, result: 'FALLBACK',
        )
        session = web.WebSession('a', 'Cats', mock.Mock())

        self.assertFalse(watcher.recover_rejected_submission(session))
        interface.latest_too_long_error_id.assert_not_called()

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


class IdleReloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.interface = mock.Mock(spec=web.ChatGPTWeb)
        self.interface.latest_assistant_snapshot.return_value = web.AssistantSnapshot('a1', (), 'Done.')
        self.interface.valid_request_from_snapshot.return_value = None
        self.interface.invalid_json_from_snapshot.return_value = None
        self.interface.invalid_request_from_snapshot.return_value = None
        self.interface.is_generating.return_value = False
        self.interface.latest_user_message.return_value = ('u1', 'RESULT')
        self.interface.composer_text.return_value = ''
        self.interface.rate_limit_remaining.return_value = 0.0
        self.interface.conversation_protocol_state.return_value = web.ConversationProtocolState(
            routing_session_id='root', terminal_response=('Done.', 'terminal-fp'),
        )
        self.callback = mock.Mock()
        self.now = 0.0
        self.watcher = web.WebSessionWatcher(
            self.interface,
            validate_request=lambda request: None,
            execute_request=mock.Mock(return_value={'ok': True}),
            fenced_result=lambda result: 'RESULT',
            too_long_fallback=lambda request, result: 'FALLBACK',
            root_session_id='root',
            on_root_terminal=self.callback,
            settle_seconds=0.0,
            clock=lambda: self.now,
        )
        self.session = web.WebSession('a', 'Cats', mock.Mock(), routing_session_id='root')
        self.watcher.sessions = {'a': self.session}
        self.watcher.refresh_sessions = mock.Mock()

    def complete(self) -> None:
        self.assertFalse(self.watcher.step())
        self.assertTrue(self.watcher.step())
        self.assertEqual(self.session.next_idle_reload_at, 300.0)

    def test_reloads_every_five_minutes_without_repeating_completion(self) -> None:
        self.complete()
        for self.now in (299.0, 300.0, 599.0, 600.0):
            self.watcher.step()
        self.assertEqual(self.interface.reload_session.call_args_list, [
            mock.call(self.session), mock.call(self.session),
        ])
        self.callback.assert_called_once_with(self.session, 'Done.')
        self.watcher.execute_request.assert_not_called()
        self.assertEqual(self.session.seen_fingerprints, {'terminal-fp'})

    def test_remote_tool_call_after_reload_executes_once(self) -> None:
        self.complete()
        self.now = 300.0
        self.watcher.step()
        self.interface.latest_user_message.return_value = ('phone-user', 'Keep going')
        self.interface.valid_request_from_snapshot.return_value = (
            '{}', {'session': 'root', 'tool': 'status'}, 'remote-tool-fp',
        )
        for self.now in (301.0, 302.0, 601.0):
            self.watcher.step()
        self.watcher.execute_request.assert_called_once()
        self.interface.reload_session.assert_called_once_with(self.session)
        self.assertIsNone(self.session.next_idle_reload_at)

    def test_generation_cancels_timer_and_restarts_after_completion(self) -> None:
        self.complete()
        self.interface.is_generating.return_value = True
        self.now = 300.0
        self.watcher.step()
        self.assertIsNone(self.session.next_idle_reload_at)
        self.interface.is_generating.return_value = False
        self.now = 310.0
        self.watcher.step()
        self.assertEqual(self.session.next_idle_reload_at, 610.0)
        self.interface.reload_session.assert_not_called()

    def test_new_user_turn_cancels_idle_reload_while_waiting_for_reply(self) -> None:
        self.complete()
        self.interface.latest_user_message.return_value = ('u2', 'Next task')
        self.interface.conversation_protocol_state.return_value = web.ConversationProtocolState(
            routing_session_id='root',
        )
        self.now = 300.0
        self.watcher.step()
        self.assertIsNone(self.session.next_idle_reload_at)
        self.interface.reload_session.assert_not_called()

    def test_draft_defers_reload_until_composer_is_empty(self) -> None:
        self.complete()
        self.interface.composer_text.return_value = 'Unsent draft'
        self.now = 300.0
        self.watcher.step()
        self.interface.reload_session.assert_not_called()
        self.interface.composer_text.return_value = ''
        self.now = 301.0
        self.watcher.step()
        self.interface.reload_session.assert_called_once_with(self.session)

    def test_busy_sessions_never_reload(self) -> None:
        for state in (
            {'pending_responses': [web.PendingResponse('RESULT', 'FALLBACK')]},
            {'terminal_expectation': web.TerminalExpectation('RESULT', 'u0')},
            {'settling_fingerprint': 'new-terminal'},
            {'retire_requested': True},
        ):
            with self.subTest(state=state):
                session = web.WebSession('b', 'Dogs', mock.Mock(), next_idle_reload_at=300.0, **state)
                self.assertFalse(self.watcher.reload_idle_session(session, 300.0))
                self.assertIsNone(session.next_idle_reload_at)
        self.interface.reload_session.assert_not_called()

    def test_reload_failure_waits_another_five_minutes(self) -> None:
        self.complete()
        self.interface.reload_session.side_effect = RuntimeError('navigation timed out')
        for self.now in (300.0, 301.0, 599.0):
            with mock.patch('builtins.print'):
                self.watcher.step()
        self.interface.reload_session.assert_called_once_with(self.session)
        self.assertEqual(self.session.next_idle_reload_at, 600.0)

    def test_playwright_reload_waits_for_page(self) -> None:
        web.ChatGPTWeb().reload_session(self.session)
        self.session.page.reload.assert_called_once_with(wait_until='domcontentloaded', timeout=30_000)

    def test_idle_without_a_terminal_response_does_not_reload(self) -> None:
        self.interface.conversation_protocol_state.return_value = web.ConversationProtocolState(
            routing_session_id='root',
        )
        for self.now in (0.0, 300.0, 600.0):
            self.watcher.step()
        self.interface.reload_session.assert_not_called()

    def test_each_session_has_its_own_reload_deadline(self) -> None:
        self.complete()
        other = web.WebSession('b', 'Dogs', mock.Mock(), routing_session_id='root')
        self.watcher.sessions['b'] = other
        for self.now in (100.0, 101.0, 300.0):
            self.watcher.step()
        self.interface.reload_session.assert_called_once_with(self.session)
        self.now = 401.0
        self.watcher.step()
        self.assertEqual(self.interface.reload_session.call_args_list, [
            mock.call(self.session), mock.call(other),
        ])

    def test_terminal_browser_stays_idle_after_terminal_expectation_clears(self) -> None:
        interface = mock.Mock()
        interface.rate_limit_remaining.return_value = 0.0
        interface.valid_request.return_value = None
        interface.invalid_json_candidate.return_value = None
        interface.invalid_request_candidate.return_value = None
        interface.is_generating.return_value = False
        interface.latest_user_message.return_value = ('u1', 'RESULT')
        interface.terminal_response_candidate.return_value = ('Done.', 'terminal-fp')
        interface.composer_text.return_value = ''
        self.watcher.interface = interface
        self.session.terminal_expectation = web.TerminalExpectation('RESULT', 'u0', 'u1')
        self.complete()
        self.assertIsNone(self.session.terminal_expectation)
        for self.now in (300.0, 600.0):
            self.watcher.step()
        self.assertEqual(interface.reload_session.call_count, 2)
        interface.latest_user_message.return_value = ('u2', 'Next task')
        self.now = 900.0
        self.watcher.step()
        self.assertEqual(interface.reload_session.call_count, 2)
        self.assertIsNone(self.session.next_idle_reload_at)


if __name__ == '__main__':
    unittest.main()
