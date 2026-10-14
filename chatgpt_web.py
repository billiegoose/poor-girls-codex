from __future__ import annotations

import hashlib
import json
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, unquote, urlparse

from progress_ui import ResponseProgress, color as progress_color


ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
MESSAGE_SELECTOR = '[data-message-author-role]'
USER_SELECTOR = '[data-message-author-role="user"]'
NEW_MESSAGE_SELECTOR = '[data-chatgpt-search-unit-key$=":user"], [data-chatgpt-search-unit-key$=":assistant"]'
NEW_ASSISTANT_SELECTOR = '[data-chatgpt-search-unit-key$=":assistant"]'
NEW_USER_SELECTOR = '[data-chatgpt-search-unit-key$=":user"]'
COMPOSER_SELECTOR = '#prompt-textarea'
NEW_COMPOSER_SELECTOR = '[data-chatgpt-composer] [contenteditable="true"][role="textbox"]'
SEND_SELECTOR = 'button[data-testid="send-button"]'
STOP_SELECTOR = 'button[data-testid="stop-button"]'
ASSISTANT_START_SELECTOR = (
    f'{ASSISTANT_SELECTOR}, {NEW_ASSISTANT_SELECTOR}, '
    f'{STOP_SELECTOR}, button[aria-label="Stop"]'
)
DEFAULT_CDP_URL = 'http' + '://127.0.0.1:9222'
CHROME_CDP_STARTUP_TIMEOUT_SECONDS = 10.0
CHROME_CDP_RETRY_SECONDS = 0.25
DEFAULT_SETTLE_SECONDS = 0.35
DEFAULT_POLL_SECONDS = 0.25
IDLE_RELOAD_SECONDS = 5.0 * 60.0
SUBMIT_SETTLE_TIMEOUT_SECONDS = 10.0
SUBMIT_SETTLE_POLL_SECONDS = 0.05
RECENT_ASSISTANT_MESSAGE_LIMIT = 10
DELIVERY_RETRY_INITIAL_SECONDS = 0.25
DELIVERY_RETRY_MAX_SECONDS = 8.0
DELIVERY_FAILURE_TIMEOUT_SECONDS = 60.0 * 60.0
RATE_LIMIT_BACKOFF_INITIAL_SECONDS = 60.0
RATE_LIMIT_BACKOFF_MAX_SECONDS = 300.0
RATE_LIMIT_BACKOFF_RESET_SECONDS = 900.0
MESSAGE_TOO_LONG_TEXT = 'The message you submitted was too long, please edit it and resubmit.'
TERMINAL_ERROR_MAX_CHARS = 1200
SUBAGENT_COLOR_CODES = ('1;34', '1;35', '1;36', '1;33', '1;32')


def terminal_error_summary(exc: BaseException, max_chars: int = TERMINAL_ERROR_MAX_CHARS) -> str:
    safe_lines: list[str] = []
    for line in str(exc).strip().splitlines():
        fill_index = line.find('fill(')
        if fill_index >= 0:
            safe_lines.append(line[:fill_index] + 'fill(<payload omitted>)')
            break
        safe_lines.append(line)

    summary = '\n'.join(safe_lines).strip()
    if len(summary) <= max_chars:
        return summary
    return summary[: max(0, max_chars - 3)].rstrip() + '...'
SUBAGENT_COMPLETION_INSTRUCTIONS = '''Subagent completion protocol:
- You are a subagent. Continue using tools while work remains.
- When your assigned work is complete, finish normally with a concise prose response.
- Poor Girl's Codex detects that terminal response after a tool-result turn, forwards it to your parent, and retires this conversation automatically.'''


class MessageTooLongError(RuntimeError):
    pass


class DeliveryFailureTimeout(RuntimeError):
    pass


@dataclass
class PendingResponse:
    result: str
    fallback: str
    progress: ResponseProgress | None = None
    compact: bool = False
    arms_terminal: bool = True

    def render(self) -> str:
        return self.fallback if self.compact else self.result

    def set_status(self, status: str) -> None:
        if self.progress is not None:
            self.progress.set_status(status)


@dataclass(frozen=True)
class AssistantSnapshot:
    message_id: str | None
    sources: tuple[str, ...]
    text: str = ''


@dataclass
class TerminalExpectation:
    submitted_text: str
    previous_user_message_id: str | None
    user_message_id: str | None = None


@dataclass(frozen=True)
class ConversationProtocolState:
    routing_session_id: str | None = None
    recent_request: tuple[str, Any, str] | None = None
    request_pending: bool = False
    terminal_response: tuple[str, str] | None = None


@dataclass
class WebSession:
    conversation_id: str
    label: str
    page: Any
    seen_fingerprints: set[str] = field(default_factory=set)
    pending_responses: list[PendingResponse] = field(default_factory=list)
    last_submitted_responses: list[PendingResponse] = field(default_factory=list)
    last_too_long_error_id: str | None = None
    settling_fingerprint: str | None = None
    settle_deadline: float = 0.0
    delivery_attempt: int = 0
    next_delivery_at: float = 0.0
    delivery_failure_started_at: float | None = None
    delivered: int = 0
    routing_session_id: str | None = None
    retire_requested: bool = False
    terminal_expectation: TerminalExpectation | None = None
    cdp_session: Any | None = None
    recovered_from_dom: bool = False
    next_idle_reload_at: float | None = None
    idle_terminal_user_message_id: str | None = None

    @property
    def url(self) -> str:
        return str(self.page.url)


class ChatGPTWeb:
    """Playwright/CDP transport for existing ChatGPT conversation tabs."""

    name = 'chatgpt-web'

    def __init__(self, cdp_url: str = DEFAULT_CDP_URL) -> None:
        self.cdp_url = cdp_url
        self._playwright = None
        self._browser = None
        self._instrumented_page_ids: set[int] = set()
        self._rate_limit_until = 0.0
        self._rate_limit_backoff_seconds = RATE_LIMIT_BACKOFF_INITIAL_SECONDS
        self._last_rate_limit_at: float | None = None

    @staticmethod
    def _start_playwright() -> Any:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                'chatgpt-web requires Playwright; install project dependencies with uv sync'
            ) from exc
        return sync_playwright().start()

    @staticmethod
    def launch_chrome_for_cdp() -> None:
        subprocess.run(
            [
                'open',
                '-na',
                'Google Chrome',
                '--args',
                '--remote-debugging-port=9222',
                '--remote-allow-origins=*',
                f'--user-data-dir={Path.home() / ".chrome-pgc"}',
                '--no-first-run',
                '--no-default-browser-check',
                'https://chatgpt.com/',
            ],
            check=True,
        )

    def _connect_browser(self) -> Any:
        return self._playwright.chromium.connect_over_cdp(
            self.cdp_url,
            no_defaults=True,
        )

    def _connect_browser_after_chrome_launch(self) -> Any:
        deadline = time.monotonic() + CHROME_CDP_STARTUP_TIMEOUT_SECONDS
        while True:
            try:
                return self._connect_browser()
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(CHROME_CDP_RETRY_SECONDS)

    def connect(self) -> None:
        self._playwright = self._start_playwright()
        try:
            self._browser = self._connect_browser()
            return
        except Exception:
            if self.cdp_url != DEFAULT_CDP_URL:
                self._playwright.stop()
                self._playwright = None
                raise

        try:
            self.launch_chrome_for_cdp()
            self._browser = self._connect_browser_after_chrome_launch()
        except Exception:
            self._playwright.stop()
            self._playwright = None
            raise

    def close(self) -> None:
        # The browser is user-owned. Stopping Playwright detaches; never browser.close().
        if self._playwright is not None:
            self._playwright.stop()
        self._playwright = None
        self._browser = None

    @staticmethod
    def conversation_id_for_url(url: str) -> str | None:
        parsed = urlparse(url)
        if parsed.hostname != 'chatgpt.com':
            return None
        parts = [part for part in parsed.path.split('/') if part]
        if len(parts) != 2 or parts[0] != 'c' or not parts[1]:
            return None
        conversation_id = unquote(parts[1])
        if conversation_id.startswith('local-chatgpt:'):
            return None
        return conversation_id

    def pages(self) -> list[Any]:
        if self._browser is None:
            raise RuntimeError('chatgpt-web is not connected')
        pages: list[Any] = []
        for context in self._browser.contexts:
            for page in context.pages:
                if self.conversation_id_for_url(str(page.url)) is not None:
                    pages.append(page)
        return pages

    def describe_page(self, page: Any) -> tuple[str, str]:
        conversation_id = self.conversation_id_for_url(str(page.url))
        if conversation_id is None:
            raise RuntimeError(f'not a ChatGPT conversation URL: {page.url}')
        try:
            title = page.title().strip()
        except Exception:
            title = ''
        label = title or f'chat-{conversation_id[:8]}'
        return conversation_id, label

    def fetch_conversation_payload(self, conversation_id: str) -> dict[str, Any]:
        """Fetch one conversation through the authenticated browser context without opening a page."""
        if self._browser is None:
            raise RuntimeError('chatgpt-web is not connected')
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError('connected Chrome browser has no usable context')

        request = contexts[0].request
        session_response = request.get('https' + '://chatgpt.com/api/auth/session')
        if not session_response.ok:
            raise RuntimeError(
                f'ChatGPT session fetch failed with HTTP {session_response.status}'
            )
        session_payload = session_response.json()
        if not isinstance(session_payload, dict):
            raise RuntimeError('ChatGPT session fetch returned a non-object payload')
        access_token = session_payload.get('accessToken')
        if not isinstance(access_token, str) or not access_token:
            raise RuntimeError('ChatGPT session did not provide an access token')

        response = request.get(
            'https' + f'://chatgpt.com/backend-api/conversation/{conversation_id}',
            headers={'Authorization': f'Bearer {access_token}'},
        )
        if not response.ok:
            raise RuntimeError(
                f'conversation fetch failed with HTTP {response.status}: {conversation_id}'
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError('conversation fetch returned a non-object payload')
        return payload

    @staticmethod
    def is_conversations_response_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            if parsed.hostname != 'chatgpt.com':
                return False
            return (
                parsed.path == '/backend-api/conversations'
                or parsed.path == '/backend-api/conversation'
                or parsed.path.startswith('/backend-api/conversation/')
                or parsed.path == '/backend-api/f/conversation'
                or parsed.path.startswith('/backend-api/f/conversation/')
            )
        except ValueError:
            return False

    @staticmethod
    def is_rate_limit_pause_exempt_url(url: str) -> bool:
        try:
            parsed = urlparse(url)
            if (
                parsed.hostname != 'chatgpt.com'
                or parsed.path != '/backend-api/conversations'
            ):
                return False
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            origin = query.pop('conversation_origin', None)
            excluded_origin = query.pop('exclude_conversation_origin', None)
            if origin != 'tpp' and excluded_origin != 'tpp':
                return False
            if origin is not None and excluded_origin is not None:
                return False
            return query == {
                'expand': 'false',
                'hide_snorlax': 'false',
                'is_archived': 'false',
                'is_starred': 'false',
                'limit': '20',
                'order': 'updated',
                'offset': '0',
            }
        except ValueError:
            return False

    def rate_limit_remaining(self, now: float | None = None) -> float:
        if now is None:
            now = time.monotonic()
        return max(0.0, self._rate_limit_until - now)

    def note_rate_limit(self, *, method: str, url: str) -> None:
        now = time.monotonic()
        already_paused = now < self._rate_limit_until
        if not already_paused:
            if self._last_rate_limit_at is None:
                next_backoff = RATE_LIMIT_BACKOFF_INITIAL_SECONDS
            else:
                # A retry that is rejected as soon as the previous pause ends should
                # back off harder. As PGC then runs successfully, decay that next
                # pause continuously back toward the initial 60s over the recovery
                # window. Measure quiet time from the end of the previous pause, not
                # from the last 429 in its burst, so repeated responses cannot pin the
                # backoff at 300s forever.
                quiet_seconds = max(0.0, now - self._rate_limit_until)
                escalated_backoff = min(
                    self._rate_limit_backoff_seconds * 2,
                    RATE_LIMIT_BACKOFF_MAX_SECONDS,
                )
                recovery = min(1.0, quiet_seconds / RATE_LIMIT_BACKOFF_RESET_SECONDS)
                next_backoff = (
                    RATE_LIMIT_BACKOFF_INITIAL_SECONDS
                    + (escalated_backoff - RATE_LIMIT_BACKOFF_INITIAL_SECONDS)
                    * (1.0 - recovery)
                )
            self._rate_limit_backoff_seconds = next_backoff
            self._rate_limit_until = now + self._rate_limit_backoff_seconds
            print(
                f'  [web:rate-limit] {method} 429 {url} -- '
                f'pausing PGC for {self._rate_limit_backoff_seconds:g}s',
                flush=True,
            )
        self._last_rate_limit_at = now

    def dismiss_rate_limit_dialog(self, page: Any) -> bool:
        try:
            dialog = page.get_by_role('dialog').filter(has_text='Too many requests')
            button = dialog.get_by_role('button', name='Got it', exact=True).last
            if button.count() == 0 or not button.is_visible():
                return False
            button.evaluate('element => element.click()')
            return True
        except Exception:
            return False

    def log_conversations_response(self, page: Any, response: Any) -> None:
        if not self.is_conversations_response_url(str(response.url)):
            return
        try:
            status = int(response.status)
            if status != 429:
                return
            response_url = str(response.url)
            if not self.is_rate_limit_pause_exempt_url(response_url):
                self.note_rate_limit(
                    method=str(response.request.method),
                    url=response_url,
                )
            # Even an exempt conversations-list 429 can surface ChatGPT's modal;
            # dismiss it without turning that request into a global PGC pause.
            self.dismiss_rate_limit_dialog(page)
        except Exception as exc:
            print(f'  [web:conversations] instrumentation error: {exc}', flush=True)

    def instrument_conversations_responses(self, page: Any) -> None:
        page_id = id(page)
        if page_id in self._instrumented_page_ids:
            return
        page.on(
            'response',
            lambda response: self.log_conversations_response(page, response),
        )
        self._instrumented_page_ids.add(page_id)

    @staticmethod
    def message_id(message: Any) -> str | None:
        message_id = message.get_attribute('data-message-id')
        if message_id:
            return message_id
        search_ids = message.get_attribute('data-chatgpt-search-message-ids')
        if search_ids:
            return search_ids.split()[0]
        selected = message.locator('[data-chatgpt-selection-message-id]')
        if selected.count() > 0:
            return selected.last.get_attribute('data-chatgpt-selection-message-id')
        return None

    @staticmethod
    def message_role(message: Any) -> str | None:
        role = message.get_attribute('data-message-author-role')
        if role in {'user', 'assistant'}:
            return role
        search_key = message.get_attribute('data-chatgpt-search-unit-key') or ''
        if search_key.endswith(':user'):
            return 'user'
        if search_key.endswith(':assistant'):
            return 'assistant'
        return None

    @staticmethod
    def message_locator(page: Any, role: str | None = None) -> Any:
        old_selector = {
            None: MESSAGE_SELECTOR,
            'user': USER_SELECTOR,
            'assistant': ASSISTANT_SELECTOR,
        }[role]
        old_messages = page.locator(old_selector)
        if old_messages.count() > 0:
            return old_messages
        new_selector = {
            None: NEW_MESSAGE_SELECTOR,
            'user': NEW_USER_SELECTOR,
            'assistant': NEW_ASSISTANT_SELECTOR,
        }[role]
        return page.locator(new_selector)

    @staticmethod
    def json_candidates_for_message(message: Any) -> tuple[str | None, list[str]]:
        message_id = ChatGPTWeb.message_id(message)
        candidates: list[str] = []
        code = message.locator('pre code')
        for index in range(code.count()):
            text = code.nth(index).inner_text().strip()
            if text:
                candidates.append(text)
        if candidates:
            return message_id, candidates
        pre = message.locator('pre')
        for index in range(pre.count()):
            text = pre.nth(index).inner_text().strip()
            if text:
                candidates.append(text)
        if not candidates:
            code_blocks = message.locator('[data-markdown-copy="code-block"]')
            for index in range(code_blocks.count()):
                text = code_blocks.nth(index).inner_text().strip()
                if text.lower().startswith('json\n'):
                    text = text.split('\n', 1)[1].strip()
                if text:
                    candidates.append(text)
        if not candidates:
            candidates.extend(
                ChatGPTWeb.json_candidates_from_text(
                    ChatGPTWeb.rendered_message_text(message)
                )
            )
        return message_id, candidates

    def latest_assistant_snapshot(self, session: WebSession) -> AssistantSnapshot:
        messages = self.message_locator(session.page, 'assistant')
        if messages.count() == 0:
            return AssistantSnapshot(None, ())
        message = messages.last
        message_id, sources = self.json_candidates_for_message(message)
        return AssistantSnapshot(
            message_id,
            tuple(sources),
            self.rendered_message_text(message).strip(),
        )

    def latest_json_candidates(self, session: WebSession) -> tuple[str | None, list[str]]:
        snapshot = self.latest_assistant_snapshot(session)
        return snapshot.message_id, list(snapshot.sources)

    def valid_request_from_message(
        self,
        message: Any,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        message_id, sources = self.json_candidates_for_message(message)
        candidates: list[tuple[int, str, Any]] = []
        for source in sources:
            source = source.strip()
            if not source:
                continue
            try:
                request = json.loads(source)
                validate_request(request)
            except (ValueError, json.JSONDecodeError):
                continue
            candidates.append((len(source), source, request))
        if not candidates:
            return None
        _, source, request = min(candidates, key=lambda item: item[0])
        identity = (message_id or '') + '\0' + source
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return source, request, fingerprint

    @staticmethod
    def json_candidates_from_text(text: str) -> list[str]:
        candidates = [
            match.group(1).replace('\u00a0', ' ').strip()
            for match in re.finditer(r'```[^\n]*\n(.*?)```', text, flags=re.DOTALL)
            if match.group(1).strip()
        ]
        if candidates:
            return candidates
        stripped = text.replace('\u00a0', ' ').strip()
        if stripped.lower().startswith('json\n'):
            stripped = stripped.split('\n', 1)[1].strip()
        if stripped.startswith(('{', '[')):
            return [stripped]
        return []

    def valid_request_from_text(
        self,
        text: str,
        validate_request: Callable[[Any], None],
        *,
        message_id: str | None = None,
    ) -> tuple[str, Any, str] | None:
        candidates: list[tuple[int, str, Any]] = []
        for source in self.json_candidates_from_text(text):
            try:
                request = json.loads(source)
                validate_request(request)
            except (ValueError, json.JSONDecodeError):
                continue
            candidates.append((len(source), source, request))
        if not candidates:
            return None
        _, source, request = min(candidates, key=lambda item: item[0])
        identity = (message_id or '') + '\0' + source
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return source, request, fingerprint

    def valid_request_from_snapshot(
        self,
        snapshot: AssistantSnapshot,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        candidates: list[tuple[int, str, Any]] = []
        for source in snapshot.sources:
            source = source.strip()
            if not source:
                continue
            try:
                request = json.loads(source)
                validate_request(request)
            except (ValueError, json.JSONDecodeError):
                continue
            candidates.append((len(source), source, request))
        if not candidates:
            return None
        _, source, request = min(candidates, key=lambda item: item[0])
        identity = (snapshot.message_id or '') + '\0' + source
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return source, request, fingerprint

    def valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        return self.valid_request_from_snapshot(
            self.latest_assistant_snapshot(session),
            validate_request,
        )

    def invalid_json_from_snapshot(self, snapshot: AssistantSnapshot) -> tuple[str, str, str] | None:
        candidates: list[tuple[int, str, str]] = []
        for source in snapshot.sources:
            source = source.strip()
            looks_like_toolcall = any(marker in source for marker in ('"tool"', '"calls"', '"session"'))
            if not source or source[0] not in '[{' or not looks_like_toolcall:
                continue
            try:
                json.loads(source)
            except json.JSONDecodeError as exc:
                detail = f'{exc.msg} at line {exc.lineno}, column {exc.colno}'
                candidates.append((len(source), source, detail))
        if not candidates:
            return None
        _, source, detail = min(candidates, key=lambda item: item[0])
        identity = (snapshot.message_id or '') + '\0invalid-json\0' + source
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return source, detail, fingerprint

    def invalid_json_candidate(self, session: WebSession) -> tuple[str, str, str] | None:
        return self.invalid_json_from_snapshot(self.latest_assistant_snapshot(session))

    def invalid_request_from_snapshot(
        self,
        snapshot: AssistantSnapshot,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, str, str] | None:
        candidates: list[tuple[int, str, str]] = []
        for source in snapshot.sources:
            source = source.strip()
            looks_like_toolcall = any(marker in source for marker in ('"tool"', '"calls"', '"session"'))
            if not source or source[0] not in '[{' or not looks_like_toolcall:
                continue
            try:
                request = json.loads(source)
            except json.JSONDecodeError:
                continue
            try:
                validate_request(request)
            except ValueError as exc:
                candidates.append((len(source), source, str(exc)))
        if not candidates:
            return None
        _, source, detail = min(candidates, key=lambda item: item[0])
        identity = (snapshot.message_id or '') + '\0invalid-request\0' + source
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return source, detail, fingerprint

    def invalid_request_candidate(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, str, str] | None:
        return self.invalid_request_from_snapshot(
            self.latest_assistant_snapshot(session),
            validate_request,
        )

    def latest_message_is_assistant(self, session: WebSession) -> bool:
        messages = self.message_locator(session.page)
        if messages.count() == 0:
            return False
        return self.message_role(messages.last) == 'assistant'

    @staticmethod
    def normalize_message_text(text: str) -> str:
        normalized = text.replace('\u00a0', ' ').strip()
        if normalized.startswith('```') and normalized.endswith('```'):
            first_newline = normalized.find('\n')
            if first_newline != -1:
                normalized = normalized[first_newline + 1 : -3].strip()
        return normalized

    def latest_user_message(self, session: WebSession) -> tuple[str | None, str] | None:
        messages = self.message_locator(session.page, 'user')
        if messages.count() == 0:
            return None
        message = messages.last
        return self.message_id(message), self.rendered_message_text(message)

    def terminal_response_from_snapshot(
        self,
        session: WebSession,
        snapshot: AssistantSnapshot,
        expected_user_message_id: str,
    ) -> tuple[str, str] | None:
        if snapshot.message_id is None:
            return None
        messages = self.message_locator(session.page)
        count = messages.count()
        expected_user_index = None
        snapshot_index = None
        for index in range(count - 1, -1, -1):
            message = messages.nth(index)
            message_id = self.message_id(message)
            role = self.message_role(message)
            if snapshot_index is None and role == 'assistant' and message_id == snapshot.message_id:
                snapshot_index = index
            if role == 'user':
                if message_id == expected_user_message_id:
                    expected_user_index = index
                    break
                # A newer user turn means this terminal expectation is stale.
                return None

        if expected_user_index is None or snapshot_index is None:
            return None
        if snapshot_index <= expected_user_index:
            return None

        text = snapshot.text
        identity = (snapshot.message_id or '') + '\0terminal-response\0' + text
        fingerprint = hashlib.sha256(identity.encode('utf-8')).hexdigest()
        return text, fingerprint

    def terminal_response_candidate(
        self,
        session: WebSession,
        expected_user_message_id: str,
    ) -> tuple[str, str] | None:
        return self.terminal_response_from_snapshot(
            session,
            self.latest_assistant_snapshot(session),
            expected_user_message_id,
        )

    def recent_valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
        *,
        limit: int = RECENT_ASSISTANT_MESSAGE_LIMIT,
    ) -> tuple[str, Any, str] | None:
        messages = self.message_locator(session.page, 'assistant')
        count = messages.count()
        start = max(0, count - limit)
        for index in range(count - 1, start - 1, -1):
            request = self.valid_request_from_message(messages.nth(index), validate_request)
            if request is not None:
                return request
        return None

    @staticmethod
    def bootstrap_routing_session_id(text: str) -> str | None:
        match = re.search(
            r'Every executable Poor Girl\'s Codex request MUST include the exact top-level field '
            r'"session": "([^"]+)"',
            text,
        )
        return match.group(1) if match is not None else None

    def protocol_tool_result_matches_request(self, text: str, request: Any) -> bool:
        calls = (
            request.get('calls')
            if isinstance(request, dict) and isinstance(request.get('calls'), list)
            else [request]
        )

        stripped = text.strip()
        if stripped.startswith("Poor Girl's Codex delivery error: the full tool results were too large for ChatGPT."):
            for index, call in enumerate(calls):
                if not isinstance(call, dict):
                    return False
                call_id = str(call.get('id', index))
                tool = str(call.get('tool', '?'))
                if f'- {call_id}: {tool} (' not in stripped:
                    return False
            return True

        for source in self.json_candidates_from_text(text):
            try:
                payload = json.loads(source)
            except json.JSONDecodeError:
                continue
            results = payload if isinstance(payload, list) else [payload]
            if len(results) != len(calls):
                continue
            matched = True
            for index, (call, result) in enumerate(zip(calls, results, strict=True)):
                if not isinstance(call, dict) or not isinstance(result, dict):
                    matched = False
                    break
                expected_id = str(call.get('id', index))
                expected_tool = call.get('tool')
                if str(result.get('id')) != expected_id or result.get('tool') != expected_tool:
                    matched = False
                    break
            if matched:
                return True
        return False

    @staticmethod
    def rendered_message_text(message: Any) -> str:
        text = message.inner_text()
        toggles = message.locator('button[data-testid="collapsible-user-message-toggle"]')
        if toggles.count() == 0:
            toggles = message.locator('button')
        for index in range(toggles.count()):
            toggle_text = toggles.nth(index).inner_text().strip()
            if toggle_text not in {'Show more', 'Show less'}:
                continue
            stripped = text.rstrip()
            if stripped.endswith(toggle_text):
                text = stripped[: -len(toggle_text)].rstrip()
        stripped = text.strip()
        for prefix in ('ChatGPT said:', 'You said:'):
            if stripped == prefix:
                return ''
            if stripped.startswith(prefix + '\n'):
                return stripped[len(prefix) + 1 :].strip()
        return text

    def conversation_protocol_state(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
        *,
        limit: int = RECENT_ASSISTANT_MESSAGE_LIMIT * 2,
    ) -> ConversationProtocolState:
        messages = self.message_locator(session.page)
        count = messages.count()
        start = max(0, count - limit)
        records: list[dict[str, Any]] = []

        for dom_index in range(start, count):
            message = messages.nth(dom_index)
            role = self.message_role(message)
            if role not in {'user', 'assistant'}:
                continue
            message_id = self.message_id(message)
            text = self.rendered_message_text(message).strip()
            request = (
                self.valid_request_from_message(message, validate_request)
                if role == 'assistant'
                else None
            )
            records.append(
                {
                    'role': role,
                    'message_id': message_id,
                    'text': text,
                    'request': request,
                }
            )

        latest_request: tuple[str, Any, str] | None = None
        latest_request_index: int | None = None
        bootstrap_session_id: str | None = None
        bootstrap_index: int | None = None
        for index, record in enumerate(records):
            if record['role'] == 'user':
                recovered = self.bootstrap_routing_session_id(record['text'])
                if recovered is not None:
                    bootstrap_session_id = recovered
                    bootstrap_index = index
                continue
            if record['request'] is not None:
                latest_request = record['request']
                latest_request_index = index

        routing_session_id = bootstrap_session_id
        if latest_request is not None:
            request_session = latest_request[1].get('session') if isinstance(latest_request[1], dict) else None
            if isinstance(request_session, str):
                routing_session_id = request_session

        request_result_index: int | None = None
        if latest_request_index is not None and latest_request is not None:
            for index in range(latest_request_index + 1, len(records)):
                record = records[index]
                if record['role'] != 'user':
                    continue
                if self.protocol_tool_result_matches_request(record['text'], latest_request[1]):
                    request_result_index = index
                break

        request_pending = latest_request_index is not None and request_result_index is None
        terminal_response: tuple[str, str] | None = None
        if routing_session_id is not None and records:
            latest = records[-1]
            if latest['role'] == 'assistant' and latest['request'] is None:
                terminal = False
                if latest_request_index is not None:
                    terminal = (
                        request_result_index is not None
                        and request_result_index < len(records) - 1
                        and not any(
                            record['role'] == 'user'
                            for record in records[request_result_index + 1 :]
                        )
                    )
                elif bootstrap_index is not None:
                    terminal = not any(
                        record['role'] == 'user'
                        for record in records[bootstrap_index + 1 :]
                    )
                if terminal:
                    text = latest['text']
                    identity = (latest['message_id'] or '') + '\0terminal-response\0' + text
                    terminal_response = (
                        text,
                        hashlib.sha256(identity.encode('utf-8')).hexdigest(),
                    )

        return ConversationProtocolState(
            routing_session_id=routing_session_id,
            recent_request=latest_request,
            request_pending=request_pending,
            terminal_response=terminal_response,
        )

    @staticmethod
    def composer(page: Any) -> Any:
        composer = page.locator(COMPOSER_SELECTOR)
        if composer.count() > 0:
            return composer
        composer = page.locator(NEW_COMPOSER_SELECTOR)
        if composer.count() > 0:
            return composer
        return page.get_by_role('textbox', name='Ask ChatGPT', exact=True)

    @staticmethod
    def send_button(page: Any) -> Any:
        button = page.locator(SEND_SELECTOR)
        if button.count() > 0:
            return button
        button = page.get_by_role('button', name='Send prompt')
        if button.count() > 0:
            return button
        return page.get_by_role('button', name='Send', exact=True)

    @staticmethod
    def stop_button(page: Any) -> Any:
        button = page.locator(STOP_SELECTOR)
        if button.count() > 0:
            return button
        button = page.get_by_role('button', name='Stop answering', exact=True)
        if button.count() > 0:
            return button
        return page.get_by_role('button', name='Stop', exact=True)

    def set_composer_text(self, session: WebSession, text: str) -> None:
        if session.cdp_session is None:
            session.cdp_session = self.emulate_active_page(session.page)
        else:
            try:
                session.cdp_session.send(
                    'Emulation.setFocusEmulationEnabled',
                    {'enabled': True},
                )
                session.cdp_session.send(
                    'Page.setWebLifecycleState',
                    {'state': 'active'},
                )
            except Exception as exc:
                print(f'  [web:{session.label}] active-page emulation error: {exc}', flush=True)

        composer = self.composer(session.page)
        if composer.count() == 0:
            # Foreground activation is a recovery path only. Normal background
            # delivery should not steal the user's active browser tab.
            session.page.bring_to_front()
            composer = self.composer(session.page)
            if composer.count() == 0:
                raise RuntimeError(f'{session.label}: ChatGPT composer not found')

        try:
            composer.last.fill(text)
        except Exception:
            # Some Chromium/ChatGPT states still refuse to edit a background
            # contenteditable despite focus emulation. Retry once in foreground.
            session.page.bring_to_front()
            composer = self.composer(session.page)
            if composer.count() == 0:
                raise RuntimeError(f'{session.label}: ChatGPT composer not found')
            composer.last.fill(text)

    def composer_text(self, session: WebSession) -> str | None:
        composer = self.composer(session.page)
        if composer.count() == 0:
            return None
        # ProseMirror renders each logical line as a <p>. Its innerText inserts
        # *display* spacing between paragraphs, so it does not equal the text
        # passed to fill(), even though the actual draft is correct. Reconstruct
        # logical newlines from the paragraph nodes instead.
        value = composer.last.evaluate(
            """element => {
                if (element.matches('[contenteditable="true"]')) {
                    const children = Array.from(element.children);
                    if (children.length && children.every(child => child.tagName === 'P')) {
                        return children.map(child => child.textContent || '').join('\\n');
                    }
                }
                return element.innerText;
            }"""
        )
        return value if isinstance(value, str) else None

    def can_submit(self, session: WebSession) -> bool:
        button = self.send_button(session.page)
        return button.count() > 0 and button.last.is_enabled()

    def is_generating(self, session: WebSession) -> bool:
        button = self.stop_button(session.page)
        return button.count() > 0 and button.last.is_visible()

    def reload_session(self, session: WebSession) -> None:
        session.page.reload(wait_until='domcontentloaded', timeout=30_000)

    def latest_too_long_error_id(self, session: WebSession) -> str | None:
        matches = session.page.get_by_text(MESSAGE_TOO_LONG_TEXT, exact=True)
        for index in range(matches.count() - 1, -1, -1):
            match = matches.nth(index)
            if not match.is_visible():
                continue
            message_id = match.evaluate(
                """e => {
                    const oldMessage = e.closest('[data-message-author-role="assistant"]');
                    if (oldMessage) return oldMessage.getAttribute('data-message-id');
                    const unit = e.closest('[data-chatgpt-search-unit-key$=":assistant"]');
                    const ids = unit?.getAttribute('data-chatgpt-search-message-ids');
                    return ids ? ids.trim().split(/\\s+/)[0] : null;
                }"""
            )
            if message_id:
                return f'assistant:{message_id}'

            # The redesigned ChatGPT UI renders submission failures as a
            # standalone banner beside the composer rather than as an assistant
            # message. Associate that banner with the most recent user turn so
            # one failed submission has a stable dedup key, while a later failed
            # submission naturally receives a different key.
            latest_user = self.latest_user_message(session)
            if latest_user is not None and latest_user[0] is not None:
                return f'user:{latest_user[0]}'

            # Last-resort stable key for a visible banner on a transcript where
            # ChatGPT has not exposed a usable message id. Attachment baselining
            # still prevents an already-visible banner from being replayed.
            return 'visible-too-long-banner'
        return None

    def emulate_active_page(self, page: Any) -> Any | None:
        try:
            cdp_session = page.context.new_cdp_session(page)
            cdp_session.send('Emulation.setFocusEmulationEnabled', {'enabled': True})
            cdp_session.send('Page.setWebLifecycleState', {'state': 'active'})
            return cdp_session
        except Exception as exc:
            print(f'  [web] CDP active-page emulation unavailable: {exc}', flush=True)
            try:
                cdp_session.detach()
            except (Exception, UnboundLocalError):
                pass
            return None

    def submit(self, session: WebSession, text: str) -> None:
        self.set_composer_text(session, text)

        def send_button():
            return self.send_button(session.page)

        deadline = time.monotonic() + SUBMIT_SETTLE_TIMEOUT_SECONDS
        refilled = False
        focused_for_missing_send = False
        button = send_button()
        if button.count() == 0:
            # Preserve the old background-tab recovery for a genuinely missing
            # control; there is no benefit in waiting for composer hydration first.
            session.page.bring_to_front()
            focused_for_missing_send = True
            button = send_button()

        while True:
            current_text = self.composer_text(session)
            enabled = button.count() > 0 and button.last.is_enabled()
            if current_text == text and enabled:
                break
            if not refilled and (
                (current_text is not None and current_text not in ('', text))
                or (current_text == '' and enabled)
            ):
                # Hydration may restore an old draft, or finish with an empty
                # editor after the initial fill. Do not refill while the editor
                # is still empty *and* Send is disabled: the first fill may just
                # be waiting for React to hydrate.
                self.set_composer_text(session, text)
                refilled = True
                button = send_button()
                continue
            if time.monotonic() >= deadline:
                break
            session.page.wait_for_timeout(int(SUBMIT_SETTLE_POLL_SECONDS * 1000))
            button = send_button()

        if current_text != text or button.count() == 0 or not button.last.is_enabled():
            # A disabled Send button is no longer sufficient evidence that the
            # payload is oversized: the new UI also uses it while restoring drafts
            # and synchronizing composer state. Only the explicit ChatGPT error is
            # treated as a length failure.
            if session.page.get_by_text(MESSAGE_TOO_LONG_TEXT, exact=True).count() > 0:
                raise MessageTooLongError(f'{session.label}: ChatGPT message is too long')

            # Preserve the old background-tab recovery after the state-settle pass.
            if not focused_for_missing_send:
                session.page.bring_to_front()
                button = send_button()
            # Foreground activation can enable Send even if the editor still
            # contains an old or incompletely hydrated draft. Never click it
            # unless we can verify the exact payload.
            if (button.count() == 0 or not button.last.is_enabled()
                    or self.composer_text(session) != text):
                raise RuntimeError(
                    f'{session.label}: ChatGPT send button is not available or composer text does not match'
                )

        # Playwright's normal click waits for the element to be visible and stable.
        # ChatGPT can leave the enabled send button perpetually "unstable" in a
        # background tab, so invoke the DOM click directly instead of requiring
        # foreground-tab actionability.
        button.last.evaluate('element => element.click()')

    def _create_conversation(
        self,
        context: Any,
        *,
        label: str | None,
        prompt: str,
        emulate_active: bool = False,
    ) -> WebSession:
        page = context.new_page()
        self.instrument_conversations_responses(page)
        cdp_session = self.emulate_active_page(page) if emulate_active else None
        try:
            # ChatGPT can keep DOMContentLoaded pending long after the new-chat
            # shell is usable. Navigation commit plus the composer visibility wait
            # below is the actual readiness boundary we care about.
            page.goto('https' + '://chatgpt.com/', wait_until='commit')
            self.composer(page).last.wait_for(state='visible', timeout=15_000)
            pending = WebSession('', label or 'new chat', page, cdp_session=cdp_session)
            self.submit(pending, prompt)
            page.wait_for_url(
                lambda url: self.conversation_id_for_url(str(url)) is not None,
                timeout=30_000,
            )
            # Do not let the next subagent steal foreground focus until this
            # conversation's assistant turn has actually started. The new DOM
            # exposes Stop before it mounts the assistant message unit.
            page.locator(ASSISTANT_START_SELECTOR).last.wait_for(
                state='attached',
                timeout=30_000,
            )
            conversation_id, page_label = self.describe_page(page)
            return WebSession(
                conversation_id,
                label or page_label,
                page,
                cdp_session=cdp_session,
            )
        except Exception:
            if cdp_session is not None:
                try:
                    cdp_session.detach()
                except Exception:
                    pass
            page.close()
            raise

    def create_conversation(
        self,
        *,
        origin: WebSession,
        label: str,
        prompt: str,
    ) -> WebSession:
        return self._create_conversation(
            origin.page.context,
            label=label,
            prompt=prompt,
            emulate_active=True,
        )

    def create_root_conversation(self, prompt: str) -> WebSession:
        if self._browser is None:
            raise RuntimeError('chatgpt-web is not connected')
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError('connected Chrome browser has no usable context')
        return self._create_conversation(contexts[0], label=None, prompt=prompt)

    def open_conversation(self, conversation_id: str) -> WebSession:
        if self._browser is None:
            raise RuntimeError('chatgpt-web is not connected')
        contexts = self._browser.contexts
        if not contexts:
            raise RuntimeError('connected Chrome browser has no usable context')
        page = contexts[0].new_page()
        self.instrument_conversations_responses(page)
        try:
            page.goto(
                'https' + f'://chatgpt.com/c/{conversation_id}',
                wait_until='commit',
            )
            self.composer(page).last.wait_for(state='visible', timeout=15_000)
            actual_id, label = self.describe_page(page)
            if actual_id != conversation_id:
                raise RuntimeError(
                    f'expected ChatGPT conversation {conversation_id}, opened {actual_id}'
                )
            return WebSession(actual_id, label, page)
        except Exception:
            page.close()
            raise

    def archive_conversation(self, session: WebSession) -> None:
        page = session.page
        more = page.locator('button[data-testid="conversation-options-button"]')
        new_header_menu = more.count() == 0

        if new_header_menu:
            more_button = page.get_by_role('button', name='More', exact=True)
            try:
                more_button.last.wait_for(state='visible', timeout=5_000)
            except Exception as exc:
                raise RuntimeError(
                    f'{session.label}: ChatGPT conversation options button not found'
                ) from exc
            if more_button.count() == 0:
                raise RuntimeError(
                    f'{session.label}: ChatGPT conversation options button not found'
                )

            def open_menu_until_archive_state(*, timeout_seconds: float = 5.0) -> tuple[str, Any]:
                deadline = time.monotonic() + timeout_seconds
                while time.monotonic() < deadline:
                    if more_button.last.get_attribute('aria-expanded') != 'true':
                        more_button.last.click()
                    page.wait_for_timeout(100)

                    unarchive = page.get_by_role('menuitem', name='Unarchive', exact=True)
                    if unarchive.count() > 0 and unarchive.last.is_visible():
                        return 'unarchive', unarchive.last
                    archive = page.get_by_role('menuitem', name='Archive', exact=True)
                    if archive.count() > 0 and archive.last.is_visible():
                        return 'archive', archive.last

                    try:
                        page.keyboard.press('Escape')
                    except Exception:
                        pass
                    page.wait_for_timeout(100)

                raise RuntimeError(
                    f'{session.label}: ChatGPT Archive/Unarchive menu state did not become available'
                )

            # The redesigned header/menu hydrates after the composer. Once the
            # conversation-specific state appears, Unarchive means this operation
            # is already complete and Archive is the action we need to invoke.
            state, item = open_menu_until_archive_state()
            if state == 'unarchive':
                page.keyboard.press('Escape')
                return
            item.click()
            page.wait_for_timeout(250)
            if self.conversation_id_for_url(str(page.url)) != session.conversation_id:
                return

            # The redesigned page does not refresh its mounted menu model after
            # archiving: it can continue to show Archive indefinitely even though
            # the server-side state has changed. Reload the same conversation and
            # confirm the persisted state from the freshly hydrated menu.
            try:
                page.reload(wait_until='domcontentloaded', timeout=30_000)
                more_button.last.wait_for(state='visible', timeout=5_000)
                state, _ = open_menu_until_archive_state()
                if state != 'unarchive':
                    raise RuntimeError('Archive remained available after reload')
                page.keyboard.press('Escape')
                return
            except Exception as exc:
                if self.conversation_id_for_url(str(page.url)) == session.conversation_id:
                    raise RuntimeError(
                        f'{session.label}: ChatGPT archive state was not confirmed'
                    ) from exc
                return

        more.last.wait_for(state='visible', timeout=5_000)
        more.last.evaluate('element => element.click()')
        archive = page.get_by_role('menuitem', name='Archive', exact=True)
        archive.wait_for(state='visible', timeout=5_000)
        archive.evaluate('element => element.click()')

        try:
            page.wait_for_url(
                lambda url: self.conversation_id_for_url(str(url)) != session.conversation_id,
                timeout=10_000,
            )
        except Exception as exc:
            if self.conversation_id_for_url(str(page.url)) == session.conversation_id:
                raise RuntimeError(
                    f'{session.label}: ChatGPT archive did not navigate away from the conversation'
                ) from exc


class WebSessionWatcher:
    """Round-robin scheduler with isolated durable state for each ChatGPT tab."""

    def __init__(
        self,
        interface: ChatGPTWeb,
        *,
        validate_request: Callable[[Any], None],
        execute_request: Callable[..., Any],
        fenced_result: Callable[[Any], str],
        too_long_fallback: Callable[[Any, Any], str],
        root_session_id: str | None = None,
        bootstrap_prompt_for_session: Callable[[str], str] | None = None,
        on_root_terminal: Callable[[WebSession, str], None] | None = None,
        root_conversation_id: str | None = None,
        new_root_conversation: bool = False,
        on_session_attached: Callable[[WebSession], None] | None = None,
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interface = interface
        self.validate_request = validate_request
        self.execute_request = execute_request
        self.fenced_result = fenced_result
        self.too_long_fallback = too_long_fallback
        self.root_session_id = root_session_id
        self.bootstrap_prompt_for_session = bootstrap_prompt_for_session
        self.on_root_terminal = on_root_terminal
        if new_root_conversation and root_conversation_id is not None:
            raise ValueError('cannot select an existing root conversation with --new')
        self.root_conversation_id = root_conversation_id
        self.new_root_conversation = new_root_conversation
        self.ignored_conversation_ids: set[str] = set()
        self.on_session_attached = on_session_attached
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sessions: dict[str, WebSession] = {}
        self.sessions_by_routing_id: dict[str, WebSession] = {}
        self.rate_limit_suspended = False

    def note_session_attached(self, session: WebSession) -> None:
        if self.on_session_attached is not None:
            self.on_session_attached(session)

    @staticmethod
    def request_session_id(request: Any) -> str | None:
        if isinstance(request, dict) and isinstance(request.get('session'), str):
            return request['session']
        return None

    @staticmethod
    def request_calls(request: Any) -> list[Any]:
        if isinstance(request, dict) and isinstance(request.get('calls'), list):
            return request['calls']
        return [request]

    def accepts_routing_session_id(self, routing_session_id: str | None) -> bool:
        if self.root_session_id is None:
            return True
        if routing_session_id is None:
            return False
        return (
            routing_session_id == self.root_session_id
            or routing_session_id.startswith(self.root_session_id + '::')
        )

    def subagent_name(self, routing_session_id: str | None) -> str | None:
        if routing_session_id is None or self.root_session_id is None:
            return None
        if routing_session_id == self.root_session_id:
            return None
        root_prefix = self.root_session_id + '::'
        if not routing_session_id.startswith(root_prefix):
            return None
        return routing_session_id.rsplit('::', 1)[-1]

    @staticmethod
    def color_subagent(name: str) -> str:
        label = f'[{name}]'
        if not sys.stdout.isatty():
            return label
        digest = hashlib.sha256(name.encode('utf-8')).digest()
        code = SUBAGENT_COLOR_CODES[digest[0] % len(SUBAGENT_COLOR_CODES)]
        return f'\033[{code}m{label}\033[0m'

    def progress_prefix(self, routing_session_id: str | None) -> str | None:
        name = self.subagent_name(routing_session_id)
        return self.color_subagent(name) if name is not None else None

    def should_replay_recovered_request(self, session: WebSession, fingerprint: str) -> bool:
        if self.interface.latest_message_is_assistant(session) is not True:
            return False
        latest = self.interface.valid_request(session, self.validate_request)
        return latest is not None and latest[2] == fingerprint

    def bind_routing_session(self, session: WebSession, routing_session_id: str) -> None:
        existing = self.sessions_by_routing_id.get(routing_session_id)
        if existing is not None and existing is not session:
            raise RuntimeError(
                f'PGC session {routing_session_id!r} is already bound to another conversation'
            )
        if session.routing_session_id not in (None, routing_session_id):
            raise RuntimeError(
                f'{session.label} changed PGC session from {session.routing_session_id!r} '
                f'to {routing_session_id!r}'
            )
        session.routing_session_id = routing_session_id
        self.sessions_by_routing_id[routing_session_id] = session
        self.note_session_attached(session)
        if (
            self.root_session_id is not None
            and (
                routing_session_id == self.root_session_id
                or routing_session_id.startswith(self.root_session_id + '::')
            )
            and session.cdp_session is None
        ):
            session.cdp_session = self.interface.emulate_active_page(session.page)

    def retire_subagent(self, session: WebSession) -> None:
        self.interface.archive_conversation(session)
        if session.cdp_session is not None:
            session.cdp_session.detach()
            session.cdp_session = None
        session.page.close()
        routing_session_id = session.routing_session_id
        if routing_session_id is not None:
            self.sessions_by_routing_id.pop(routing_session_id, None)
            session.routing_session_id = None
        self.sessions.pop(session.conversation_id, None)

    def ensure_root_session(self, bootstrap_prompt: str) -> WebSession | None:
        if self.root_session_id is None:
            return None

        attached = self.sessions_by_routing_id.get(self.root_session_id)
        if attached is not None:
            return attached

        if self.new_root_conversation:
            created = self.interface.create_root_conversation(bootstrap_prompt)
            self.sessions[created.conversation_id] = created
            self.bind_routing_session(created, self.root_session_id)
            return created

        if self.root_conversation_id is not None:
            preferred = self.sessions.get(self.root_conversation_id)
            if preferred is None:
                preferred = self.interface.open_conversation(self.root_conversation_id)
                self.sessions[preferred.conversation_id] = preferred
            self.bind_routing_session(preferred, self.root_session_id)
            return preferred

        for session in self.sessions.values():
            recent = self.interface.recent_valid_request(session, self.validate_request)
            if recent is None:
                continue
            _, request, fingerprint = recent
            routing_session_id = self.request_session_id(request)
            if routing_session_id != self.root_session_id:
                continue
            self.bind_routing_session(session, routing_session_id)
            if not self.should_replay_recovered_request(session, fingerprint):
                session.seen_fingerprints.add(fingerprint)
            return session

        created = self.interface.create_root_conversation(bootstrap_prompt)
        self.sessions[created.conversation_id] = created
        self.bind_routing_session(created, self.root_session_id)
        return created

    def execute_orchestration_tool(
        self,
        origin: WebSession,
        routing_session_id: str,
        call: dict[str, Any],
        index: int,
    ) -> dict[str, Any]:
        tool = call.get('tool')
        call_id = call.get('id', str(index))
        base = {'id': call_id, 'tool': tool}

        if origin.retire_requested:
            return {
                **base,
                'ok': False,
                'skipped': True,
                'error': 'skipped because this subagent already handed off',
            }

        if tool == 'subagent':
            if self.bootstrap_prompt_for_session is None:
                return {**base, 'ok': False, 'error': 'subagents are unavailable in this watcher'}
            name = call['name']
            child_session_id = f'{routing_session_id}::{name}'
            if child_session_id in self.sessions_by_routing_id:
                return {
                    **base,
                    'ok': False,
                    'error': f'subagent session already exists: {child_session_id}',
                }
            prompt = (
                call['prompt']
                + '\n\n---\n\n'
                + self.bootstrap_prompt_for_session(child_session_id)
                + '\n\n---\n\n'
                + SUBAGENT_COMPLETION_INSTRUCTIONS
            )
            child = self.interface.create_conversation(
                origin=origin,
                label=f'subagent:{name}',
                prompt=prompt,
            )
            self.sessions[child.conversation_id] = child
            self.bind_routing_session(child, child_session_id)
            initial_user = self.interface.latest_user_message(child)
            initial_user_message_id = (
                initial_user[0]
                if isinstance(initial_user, tuple) and len(initial_user) == 2
                else None
            )
            child.terminal_expectation = TerminalExpectation(
                submitted_text=prompt,
                previous_user_message_id=None,
                user_message_id=initial_user_message_id,
            )
            return {
                **base,
                'ok': True,
                'name': name,
                'session': child_session_id,
                'conversation_id': child.conversation_id,
                'url': child.url,
            }

        if tool == 'handoff':
            if '::' not in routing_session_id:
                return {**base, 'ok': False, 'error': 'root session has no parent to hand off to'}
            parent_session_id = routing_session_id.rsplit('::', 1)[0]
            parent = self.sessions_by_routing_id.get(parent_session_id)
            if parent is None:
                return {
                    **base,
                    'ok': False,
                    'error': f'parent session is not attached: {parent_session_id}',
                }
            message = (
                f'Subagent {routing_session_id} handed off its result:\n\n'
                + call['result'].rstrip()
                + '\n'
            )
            parent.pending_responses.append(PendingResponse(message, message))
            origin.retire_requested = True
            return {**base, 'ok': True, 'parent_session': parent_session_id}

        from toolcall_lib import execute

        return execute(call, index)

    def refresh_sessions(self, *, prime_new: bool = True) -> None:
        live: set[str] = set()
        for page in self.interface.pages():
            self.interface.instrument_conversations_responses(page)
            conversation_id = ChatGPTWeb.conversation_id_for_url(str(page.url))
            label = None
            if conversation_id is None:
                try:
                    conversation_id, label = self.interface.describe_page(page)
                except RuntimeError:
                    # A tab can navigate away from /c/<id> between pages() filtering it
                    # and this refresh pass examining it (for example back to '/').
                    # Treat it as no longer live instead of crashing the watcher.
                    continue
            if conversation_id in self.ignored_conversation_ids:
                continue
            live.add(conversation_id)
            session = self.sessions.get(conversation_id)
            if session is None:
                if label is None:
                    _, label = self.interface.describe_page(page)
                previous_id = next(
                    (
                        existing_id
                        for existing_id, existing_session in self.sessions.items()
                        if existing_session.page == page
                    ),
                    None,
                )
                if previous_id is not None:
                    session = self.sessions.pop(previous_id)
                    session.conversation_id = conversation_id
                    session.page = page
                    session.label = label
                    self.sessions[conversation_id] = session
                else:
                    session = WebSession(conversation_id=conversation_id, label=label, page=page)
                    session.last_too_long_error_id = self.interface.latest_too_long_error_id(session)
                    self.sessions[conversation_id] = session
                if previous_id is None and prime_new:
                    if isinstance(self.interface, ChatGPTWeb):
                        protocol = self.interface.conversation_protocol_state(
                            session,
                            self.validate_request,
                        )
                        if self.accepts_routing_session_id(protocol.routing_session_id):
                            assert protocol.routing_session_id is not None
                            session.recovered_from_dom = True
                            self.bind_routing_session(session, protocol.routing_session_id)
                            if protocol.recent_request is not None and not protocol.request_pending:
                                session.seen_fingerprints.add(protocol.recent_request[2])
                    else:
                        existing = self.interface.recent_valid_request(session, self.validate_request)
                        if existing is not None:
                            _, request, fingerprint = existing
                            routing_session_id = self.request_session_id(request)
                            if routing_session_id is not None:
                                self.bind_routing_session(session, routing_session_id)
                            if not self.should_replay_recovered_request(session, fingerprint):
                                session.seen_fingerprints.add(fingerprint)
            else:
                session.page = page

        for conversation_id in list(self.sessions):
            if conversation_id not in live:
                session = self.sessions.pop(conversation_id)
                if session.routing_session_id is not None:
                    self.sessions_by_routing_id.pop(session.routing_session_id, None)

    def resolve_terminal_expectation(self, session: WebSession) -> None:
        expectation = session.terminal_expectation
        if expectation is None:
            return
        latest = self.interface.latest_user_message(session)
        if not isinstance(latest, tuple) or len(latest) != 2:
            return
        message_id, text = latest
        if expectation.user_message_id is None:
            if message_id == expectation.previous_user_message_id:
                return
            if message_id is None or self.interface.normalize_message_text(text) != self.interface.normalize_message_text(expectation.submitted_text):
                session.terminal_expectation = None
                return
            expectation.user_message_id = message_id
            return
        if message_id != expectation.user_message_id:
            session.terminal_expectation = None

    def arm_idle_reload(self, session: WebSession, now: float) -> None:
        if session.next_idle_reload_at is not None:
            return
        latest_user = self.interface.latest_user_message(session)
        if isinstance(latest_user, tuple) and latest_user[0] is not None:
            session.idle_terminal_user_message_id = latest_user[0]
            session.next_idle_reload_at = now + IDLE_RELOAD_SECONDS

    def reload_idle_session(self, session: WebSession, now: float) -> bool:
        if (
            session.pending_responses
            or session.terminal_expectation is not None
            or session.settling_fingerprint is not None
            or session.retire_requested
        ):
            session.next_idle_reload_at = None
            return False
        if session.next_idle_reload_at is None or now < session.next_idle_reload_at:
            return False
        if self.interface.is_generating(session):
            session.next_idle_reload_at = None
            return False
        # Leave a draft alone, including while the user is typing at the deadline.
        draft = self.interface.composer_text(session)
        if draft is None or draft.strip():
            return False
        # Space failures out as well, rather than retrying on every watcher tick.
        session.next_idle_reload_at = now + IDLE_RELOAD_SECONDS
        self.interface.reload_session(session)
        return True

    def scan_session(self, session: WebSession, now: float) -> bool:
        if session.next_idle_reload_at is not None:
            latest_user = self.interface.latest_user_message(session)
            if not isinstance(latest_user, tuple) or latest_user[0] != session.idle_terminal_user_message_id:
                session.next_idle_reload_at = None
        if not isinstance(self.interface, ChatGPTWeb):
            self.resolve_terminal_expectation(session)
        snapshot = (
            self.interface.latest_assistant_snapshot(session)
            if isinstance(self.interface, ChatGPTWeb)
            else None
        )
        candidate = (
            self.interface.valid_request_from_snapshot(snapshot, self.validate_request)
            if snapshot is not None
            else self.interface.valid_request(session, self.validate_request)
        )
        if candidate is None:
            generating = self.interface.is_generating(session) is True
            recover_terminal_while_generating = (
                generating
                and isinstance(self.interface, ChatGPTWeb)
                and session.recovered_from_dom
                and self.subagent_name(session.routing_session_id) is not None
            )
            if generating and not recover_terminal_while_generating:
                session.next_idle_reload_at = None
                session.settling_fingerprint = None
                return False

            # A recovered tab can retain ChatGPT's streaming UI indefinitely after
            # its final prose is already rendered. In that case, skip malformed-call
            # diagnostics but still let the DOM protocol classifier and normal
            # fingerprint settling decide whether the response is terminal.
            invalid_json = None if generating else (
                self.interface.invalid_json_from_snapshot(snapshot)
                if snapshot is not None
                else self.interface.invalid_json_candidate(session)
            )
            invalid_request = None
            foreign_invalid_request = False
            if invalid_json is None and not generating:
                invalid_request = (
                    self.interface.invalid_request_from_snapshot(snapshot, self.validate_request)
                    if snapshot is not None
                    else self.interface.invalid_request_candidate(session, self.validate_request)
                )
                if invalid_request is not None and self.root_session_id is not None:
                    source, _, _ = invalid_request
                    try:
                        parsed_request = json.loads(source)
                    except json.JSONDecodeError:
                        parsed_request = None
                    routing_session_id = self.request_session_id(parsed_request)
                    if routing_session_id is not None and not self.accepts_routing_session_id(routing_session_id):
                        foreign_invalid_request = True
                        invalid_request = None

            if invalid_json is not None or invalid_request is not None:
                session.next_idle_reload_at = None
                session.terminal_expectation = None
                if invalid_json is not None:
                    _, detail, fingerprint = invalid_json
                    diagnostic = f'invalid JSON: {detail}'
                    message = (
                        "I couldn't execute that tool call because its JSON was invalid "
                        f"({detail}). Please resend the tool call as valid JSON in a single fenced `json` block."
                    )
                else:
                    assert invalid_request is not None
                    _, detail, fingerprint = invalid_request
                    diagnostic = f'invalid tool call: {detail}'
                    message = (
                        "I couldn't execute that tool call because the request was invalid "
                        f"({detail}). Please correct the tool call and resend it."
                    )
                if fingerprint in session.seen_fingerprints:
                    session.settling_fingerprint = None
                    return False
                if session.settling_fingerprint != fingerprint:
                    session.settling_fingerprint = fingerprint
                    session.settle_deadline = now + self.settle_seconds
                    return False
                if now < session.settle_deadline:
                    return False

                session.seen_fingerprints.add(fingerprint)
                session.settling_fingerprint = None
                print(f'  [web:{session.label}] {diagnostic}', flush=True)
                response_progress = ResponseProgress(prefix=self.progress_prefix(session.routing_session_id))
                session.pending_responses.append(
                    PendingResponse(
                        message,
                        message,
                        progress=response_progress,
                        arms_terminal=False,
                    )
                )
                response_progress.set_status('[pending]')
                return True

            if foreign_invalid_request:
                session.next_idle_reload_at = None
                session.settling_fingerprint = None
                return False

            terminal = None
            if isinstance(self.interface, ChatGPTWeb):
                protocol = self.interface.conversation_protocol_state(
                    session,
                    self.validate_request,
                )
                if not self.accepts_routing_session_id(protocol.routing_session_id):
                    session.next_idle_reload_at = None
                    session.settling_fingerprint = None
                    return False
                if session.routing_session_id is None and protocol.routing_session_id is not None:
                    self.bind_routing_session(session, protocol.routing_session_id)
                terminal = protocol.terminal_response
            else:
                expectation = session.terminal_expectation
                if expectation is not None and expectation.user_message_id is not None:
                    terminal = (
                        self.interface.terminal_response_from_snapshot(
                            session,
                            snapshot,
                            expectation.user_message_id,
                        )
                        if snapshot is not None
                        else self.interface.terminal_response_candidate(
                            session,
                            expectation.user_message_id,
                        )
                    )

            if terminal is not None:
                text, fingerprint = terminal
                if fingerprint in session.seen_fingerprints:
                    session.terminal_expectation = None
                    session.settling_fingerprint = None
                    self.arm_idle_reload(session, now)
                    return False
                if session.settling_fingerprint != fingerprint:
                    session.settling_fingerprint = fingerprint
                    session.settle_deadline = now + self.settle_seconds
                    return False
                if now < session.settle_deadline:
                    return False

                routing_session_id = session.routing_session_id
                parent = None
                if routing_session_id is not None and '::' in routing_session_id:
                    parent_session_id = routing_session_id.rsplit('::', 1)[0]
                    parent = self.sessions_by_routing_id.get(parent_session_id)
                    if parent is None:
                        raise RuntimeError(
                            f'parent session is not attached: {parent_session_id}'
                        )

                session.seen_fingerprints.add(fingerprint)
                session.terminal_expectation = None
                session.settling_fingerprint = None
                progress_prefix = self.progress_prefix(routing_session_id)
                header_prefix = f'{progress_prefix} ' if progress_prefix is not None else ''
                ResponseProgress.commit_live()
                print(flush=True)
                print(
                    f"{header_prefix}{progress_color('terminal response [complete]', '1;32')}",
                    flush=True,
                )

                if parent is not None:
                    message = (
                        f'Subagent {routing_session_id} completed with terminal response:\n\n'
                        + text.rstrip()
                        + '\n'
                    )
                    parent.pending_responses.append(PendingResponse(message, message))
                    session.retire_requested = True
                    self.retire_subagent(session)
                elif self.on_root_terminal is not None:
                    try:
                        self.on_root_terminal(session, text)
                    except Exception as exc:
                        print(
                            f'  [web:{session.label}] terminal notification error: '
                            f'{terminal_error_summary(exc)}',
                            flush=True,
                        )
                if parent is None:
                    self.arm_idle_reload(session, now)
                return True

            if isinstance(self.interface, ChatGPTWeb):
                session.next_idle_reload_at = None
            session.settling_fingerprint = None
            return False

        session.next_idle_reload_at = None
        _, request, fingerprint = candidate
        routing_session_id = self.request_session_id(request)
        if routing_session_id is None:
            if self.root_session_id is not None:
                return False
        else:
            self.bind_routing_session(session, routing_session_id)
        if fingerprint in session.seen_fingerprints:
            session.settling_fingerprint = None
            return False
        session.terminal_expectation = None

        if session.settling_fingerprint != fingerprint:
            session.settling_fingerprint = fingerprint
            session.settle_deadline = now + self.settle_seconds
            return False

        if now < session.settle_deadline:
            return False

        # Claim before execution. A failed delivery or repeated poll must never replay effects.
        session.seen_fingerprints.add(fingerprint)
        session.settling_fingerprint = None
        calls = self.request_calls(request)
        has_orchestration = any(
            isinstance(call, dict) and call.get('tool') in {'subagent', 'handoff'}
            for call in calls
        )
        progress_prefix = self.progress_prefix(routing_session_id)
        ResponseProgress.commit_live()
        print(flush=True)
        header_prefix = f'{progress_prefix} ' if progress_prefix is not None else ''
        print(f"{header_prefix}{progress_color('tool calls:', '1;35')}", flush=True)
        if has_orchestration:
            if routing_session_id is None:
                raise RuntimeError('web orchestration tool call is missing its PGC session id')
            execute_kwargs = {
                'announce': True,
                'tool_executor': lambda call, index: self.execute_orchestration_tool(
                    session,
                    routing_session_id,
                    call,
                    index,
                ),
            }
            if progress_prefix is not None:
                execute_kwargs['progress_prefix'] = progress_prefix
            result = self.execute_request(request, **execute_kwargs)
        else:
            execute_kwargs = {'announce': True}
            if progress_prefix is not None:
                execute_kwargs['progress_prefix'] = progress_prefix
            result = self.execute_request(request, **execute_kwargs)

        results = result if isinstance(result, list) else [result]
        successful_handoff = any(
            isinstance(call, dict)
            and call.get('tool') == 'handoff'
            and index < len(results)
            and isinstance(results[index], dict)
            and results[index].get('ok') is True
            for index, call in enumerate(calls)
        )
        if successful_handoff:
            session.retire_requested = True
            self.retire_subagent(session)
            return True

        response_progress = ResponseProgress(prefix=progress_prefix)
        session.pending_responses.append(
            PendingResponse(
                result=self.fenced_result(result),
                fallback=self.too_long_fallback(request, result),
                progress=response_progress,
            )
        )
        response_progress.set_status('[pending]')
        return True

    def recover_rejected_submission(self, session: WebSession) -> bool:
        if not session.last_submitted_responses:
            return False
        error_id = self.interface.latest_too_long_error_id(session)
        if error_id is None or error_id == session.last_too_long_error_id:
            return False
        session.last_too_long_error_id = error_id

        restored = session.last_submitted_responses
        session.last_submitted_responses = []
        session.terminal_expectation = None
        for response in restored:
            response.compact = True
            response.set_status('Message too large [retry with summary]')
        session.pending_responses = restored + session.pending_responses
        return True

    @staticmethod
    def delivery_retry_delay(attempt: int) -> float:
        delay = DELIVERY_RETRY_INITIAL_SECONDS
        remaining = max(0, attempt)
        while remaining > 0 and delay < DELIVERY_RETRY_MAX_SECONDS:
            delay = min(delay * 2, DELIVERY_RETRY_MAX_SECONDS)
            remaining -= 1
        return min(delay, DELIVERY_RETRY_MAX_SECONDS)

    @staticmethod
    def raise_if_delivery_timed_out(session: WebSession, now: float) -> None:
        started_at = session.delivery_failure_started_at
        if started_at is None or now - started_at < DELIVERY_FAILURE_TIMEOUT_SECONDS:
            return
        for response in session.pending_responses:
            response.set_status('Delivery failed for 1 hour; stopping PGC')
        raise DeliveryFailureTimeout(
            f'{session.label}: unable to deliver tool results for 1 hour; stopping PGC'
        )

    def schedule_delivery_retry(self, session: WebSession, now: float) -> float:
        if session.delivery_failure_started_at is None:
            session.delivery_failure_started_at = now
        else:
            self.raise_if_delivery_timed_out(session, now)

        delay = self.delivery_retry_delay(session.delivery_attempt)
        session.delivery_attempt += 1
        session.next_delivery_at = now + delay
        for response in session.pending_responses:
            response.set_status(
                f'ChatGPT send button is not available [retry in {delay:g}s]'
            )
        return delay

    def deliver_session(self, session: WebSession, now: float) -> bool:
        if not session.pending_responses:
            return False
        session.next_idle_reload_at = None
        self.raise_if_delivery_timed_out(session, now)
        if now < session.next_delivery_at:
            return False

        latest_user_before = self.interface.latest_user_message(session)
        previous_user_message_id = (
            latest_user_before[0]
            if isinstance(latest_user_before, tuple) and len(latest_user_before) == 2
            else None
        )
        arms_terminal = any(response.arms_terminal for response in session.pending_responses)
        rendered = '\n'.join(
            response.render().rstrip('\n') for response in session.pending_responses
        ) + '\n'
        submitted_text = rendered
        try:
            self.interface.submit(session, rendered)
        except MessageTooLongError:
            for response in session.pending_responses:
                response.compact = True
                response.set_status('Message too large [retry with summary]')
            fallback = '\n'.join(
                response.fallback.rstrip('\n') for response in session.pending_responses
            ) + '\n'
            try:
                self.interface.submit(session, fallback)
                submitted_text = fallback
            except RuntimeError:
                self.schedule_delivery_retry(session, now)
                return False
            for response in session.pending_responses:
                response.set_status('[sent summary]')
            session.last_submitted_responses = []
        except RuntimeError:
            self.schedule_delivery_retry(session, now)
            return False
        else:
            for response in session.pending_responses:
                response.set_status('[sent summary]' if response.compact else '[sent]')
            session.last_submitted_responses = list(session.pending_responses)

        session.pending_responses.clear()
        session.delivery_attempt = 0
        session.next_delivery_at = 0.0
        session.delivery_failure_started_at = None
        session.delivered += 1
        session.terminal_expectation = (
            TerminalExpectation(
                submitted_text=submitted_text,
                previous_user_message_id=previous_user_message_id,
            )
            if arms_terminal
            else None
        )
        return True

    def rate_limit_remaining(self) -> float:
        callback = getattr(self.interface, 'rate_limit_remaining', None)
        if not callable(callback):
            return 0.0
        remaining = callback()
        if not isinstance(remaining, (int, float)):
            return 0.0
        return max(0.0, float(remaining))

    def sync_rate_limit_suspension(self) -> bool:
        remaining = self.rate_limit_remaining()
        if remaining > 0.0:
            if not self.rate_limit_suspended:
                # The response callback already made one dismissal attempt. Try once
                # more immediately in case React painted the dialog between the
                # network event and this watcher iteration. The cooldown pauses PGC
                # work only; do not freeze Chromium pages or alter focus emulation.
                # Page.setWebLifecycleState('frozen') can leave a visible tab with a
                # stale/blank compositor surface until Chrome receives a real focus or
                # visibility transition.
                for session in self.sessions.values():
                    self.interface.dismiss_rate_limit_dialog(session.page)
                self.rate_limit_suspended = True
            return True

        if self.rate_limit_suspended:
            for session in self.sessions.values():
                # Clear any modal that appeared during the logical cooldown before
                # normal watcher work resumes.
                self.interface.dismiss_rate_limit_dialog(session.page)
            self.rate_limit_suspended = False
            print('  [web:rate-limit] cooldown complete; resuming PGC', flush=True)
        return False

    def step(self) -> bool:
        now = self.clock()
        for session in self.sessions.values():
            if session.pending_responses:
                self.raise_if_delivery_timed_out(session, now)
        if self.sync_rate_limit_suspension():
            return False
        self.refresh_sessions()
        progressed = False

        # Scan every conversation before delivering anything. A slow/busy tab cannot
        # redirect another tab's state, and simultaneous model turns are observed fairly.
        for session in list(self.sessions.values()):
            if session.retire_requested:
                try:
                    self.retire_subagent(session)
                    progressed = True
                except Exception as exc:
                    print(f'  [web:{session.label}] retirement error: {exc}', flush=True)
                continue
            try:
                progressed = self.recover_rejected_submission(session) or progressed
                progressed = self.scan_session(session, now) or progressed
                progressed = self.reload_idle_session(session, now) or progressed
            except Exception as exc:
                print(f'  [web:{session.label}] scan error: {exc}', flush=True)

        for session in list(self.sessions.values()):
            try:
                progressed = self.deliver_session(session, now) or progressed
            except DeliveryFailureTimeout:
                raise
            except Exception as exc:
                delay = self.schedule_delivery_retry(session, now)
                print(
                    f'  [web:{session.label}] delivery error: {terminal_error_summary(exc)} [retry in {delay:g}s]',
                    flush=True,
                )

        return progressed

    def run(self, *, bootstrap_prompt: str = '') -> None:
        if self.new_root_conversation:
            # Do not reattach or execute requests from tabs that predate --new.
            # They may share this directory's persistent routing id.
            self.refresh_sessions(prime_new=False)
            self.ignored_conversation_ids.update(self.sessions)
            self.sessions.clear()
        else:
            self.refresh_sessions()
        root = self.ensure_root_session(bootstrap_prompt) if self.root_session_id is not None else None
        if self.root_session_id is not None:
            print(
                f"  {progress_color('[session]', '1;35')} {self.root_session_id}",
                flush=True,
            )
            if root is not None:
                print(
                    f"  {progress_color('[attached]', '1;36')} {root.label} ({root.conversation_id[:8]})",
                    flush=True,
                )
        print(
            f"  {progress_color('[ready]', '1;32')} Waiting for ChatGPT tool calls...",
            flush=True,
        )

        stop_requested = False
        previous_sigint = signal.getsignal(signal.SIGINT)

        def request_stop(signum: int, frame: Any) -> None:
            nonlocal stop_requested
            if stop_requested:
                raise KeyboardInterrupt
            stop_requested = True

        signal.signal(signal.SIGINT, request_stop)
        try:
            while not stop_requested:
                progressed = self.step()
                if not progressed and not stop_requested:
                    time.sleep(self.poll_seconds)
        except DeliveryFailureTimeout as exc:
            print(f'  [web] {exc}', flush=True)
        except KeyboardInterrupt:
            # A second Ctrl+C deliberately forces an immediate exit.
            pass
        finally:
            signal.signal(signal.SIGINT, previous_sigint)

        print("\nPoor Girl's Codex web watcher stopped.")


def conversation_id_from_reference(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    if parsed.hostname == 'chatgpt.com':
        parts = [part for part in parsed.path.split('/') if part]
        if len(parts) == 2 and parts[0] == 'c' and parts[1]:
            return parts[1]
        raise ValueError(f'invalid ChatGPT conversation reference: {value!r}')
    if parsed.scheme or not value or '/' in value or '?' in value or '#' in value:
        raise ValueError(f'invalid ChatGPT conversation reference: {value!r}')
    return value


def conversation_raw_text(message: dict[str, Any]) -> tuple[str, list[Any]]:
    content = message.get('content')
    if not isinstance(content, dict):
        return '', []
    parts = content.get('parts')
    if not isinstance(parts, list):
        return '', []
    raw_parts = list(parts)
    return ''.join(part for part in raw_parts if isinstance(part, str)), raw_parts


def active_conversation_protocol_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    mapping = payload.get('mapping')
    current_node = payload.get('current_node')
    if not isinstance(mapping, dict) or not isinstance(current_node, str):
        raise RuntimeError('conversation payload is missing mapping/current_node')

    node_ids: list[str] = []
    seen: set[str] = set()
    node_id: str | None = current_node
    while node_id is not None:
        if node_id in seen:
            raise RuntimeError('conversation mapping contains a parent cycle')
        seen.add(node_id)
        node = mapping.get(node_id)
        if not isinstance(node, dict):
            raise RuntimeError(f'conversation mapping is missing node {node_id!r}')
        node_ids.append(node_id)
        parent = node.get('parent')
        node_id = parent if isinstance(parent, str) else None
    node_ids.reverse()

    records: list[dict[str, Any]] = []
    for node_id in node_ids:
        node = mapping[node_id]
        message = node.get('message')
        if not isinstance(message, dict):
            continue
        author = message.get('author')
        role = author.get('role') if isinstance(author, dict) else None
        if role not in {'user', 'assistant'}:
            continue
        content = message.get('content')
        if not isinstance(content, dict) or content.get('content_type') != 'text':
            continue
        parts = content.get('parts')
        if not isinstance(parts, list) or not all(isinstance(part, str) for part in parts):
            continue
        text = ''.join(parts)
        if not text:
            continue
        metadata = message.get('metadata')
        records.append(
            {
                'position': len(records),
                'message_id': message.get('id'),
                'role': role,
                'timestamp': message.get('create_time'),
                'text': text,
                'metadata': metadata if isinstance(metadata, dict) else {},
            }
        )
    return records


def active_conversation_messages(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: value for key, value in record.items() if key != 'metadata'}
        for record in active_conversation_protocol_messages(payload)
    ]


def export_conversation(
    conversation_reference: str,
    output_path: str,
    *,
    cdp_url: str = DEFAULT_CDP_URL,
    format: str = 'jsonl',
) -> dict[str, Any]:
    conversation_id = conversation_id_from_reference(conversation_reference)
    interface = ChatGPTWeb(cdp_url)
    interface.connect()
    try:
        payload = interface.fetch_conversation_payload(conversation_id)
    finally:
        interface.close()

    messages = active_conversation_messages(payload)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if format == 'jsonl':
        with path.open('w', encoding='utf-8') as handle:
            for message in messages:
                handle.write(json.dumps(message, ensure_ascii=False) + '\n')
    elif format in {'txt', 'md'}:
        blocks = []
        for record in messages:
            blocks.append(f"[{record['position']}] {record['role']} {record['message_id']}\n{record['text']}")
        path.write_text('\n\n'.join(blocks) + '\n', encoding='utf-8')
    else:
        raise ValueError(f'unsupported conversation export format: {format}')
    return {
        'conversation_id': conversation_id,
        'output_path': str(path),
        'format': format,
        'message_count': len(messages),
    }


def focus_open_root_conversation(
    *,
    cdp_url: str,
    session_id: str,
    validate_request: Callable[[Any], None],
) -> str | None:
    """Focus and return an already-open conversation bound to this PGC root."""
    interface = ChatGPTWeb(cdp_url)
    interface.connect()
    try:
        for page in interface.pages():
            conversation_id, label = interface.describe_page(page)
            session = WebSession(conversation_id, label, page)
            recent = interface.recent_valid_request(session, validate_request)
            if recent is None:
                continue
            _, request, _ = recent
            if WebSessionWatcher.request_session_id(request) != session_id:
                continue
            page.bring_to_front()
            return conversation_id
        return None
    finally:
        interface.close()


def inspect_web_sessions(*, cdp_url: str) -> list[tuple[str, str, str]]:
    """Return conversation id, title/label, and URL without modifying any page."""
    interface = ChatGPTWeb(cdp_url)
    interface.connect()
    try:
        rows: list[tuple[str, str, str]] = []
        for page in interface.pages():
            conversation_id, label = interface.describe_page(page)
            rows.append((conversation_id, label, str(page.url)))
        return rows
    finally:
        interface.close()


def run_web_watcher(
    *,
    cdp_url: str,
    session_id: str,
    bootstrap_prompt: str,
    validate_request: Callable[[Any], None],
    execute_request: Callable[..., Any],
    fenced_result: Callable[[Any], str],
    too_long_fallback: Callable[[Any, Any], str],
    bootstrap_prompt_for_session: Callable[[str], str] | None = None,
    on_root_terminal: Callable[[WebSession, str], None] | None = None,
    root_conversation_id: str | None = None,
    new_root_conversation: bool = False,
    on_session_attached: Callable[[WebSession], None] | None = None,
) -> None:
    interface = ChatGPTWeb(cdp_url)
    interface.connect()
    try:
        WebSessionWatcher(
            interface,
            validate_request=validate_request,
            execute_request=execute_request,
            fenced_result=fenced_result,
            too_long_fallback=too_long_fallback,
            root_session_id=session_id,
            bootstrap_prompt_for_session=bootstrap_prompt_for_session,
            on_root_terminal=on_root_terminal,
            root_conversation_id=root_conversation_id,
            new_root_conversation=new_root_conversation,
            on_session_attached=on_session_attached,
        ).run(bootstrap_prompt=bootstrap_prompt)
    finally:
        interface.close()


def run_two_tab_poc(
    *,
    cdp_url: str,
    bootstrap_prompt: str,
    validate_request: Callable[[Any], None],
    execute_request: Callable[..., Any],
    fenced_result: Callable[[Any], str],
    too_long_fallback: Callable[[Any, Any], str],
) -> None:
    """Retained smoke test: drive two existing tabs through the real scheduler."""
    interface = ChatGPTWeb(cdp_url)
    interface.connect()
    try:
        watcher = WebSessionWatcher(
            interface,
            validate_request=validate_request,
            execute_request=execute_request,
            fenced_result=fenced_result,
            too_long_fallback=too_long_fallback,
            settle_seconds=0.0,
        )
        watcher.refresh_sessions()
        sessions = list(watcher.sessions.values())[:2]
        if len(sessions) < 2:
            raise RuntimeError(f'chatgpt-web POC needs at least 2 conversation tabs; found {len(sessions)}')

        prompts = [
            bootstrap_prompt
            + '\n\nPOC session marker: tab-1. First make exactly one harmless `status {}` tool call.',
            bootstrap_prompt
            + '\n\nPOC session marker: tab-2. First make exactly one harmless `tree {"path":".","depth":1}` tool call.',
        ]
        baseline = {session.conversation_id: session.delivered for session in sessions}
        for session, prompt in zip(sessions, prompts, strict=True):
            interface.submit(session, prompt)
            print(f'  {session.label}: submitted distinct POC prompt', flush=True)

        while any(session.delivered == baseline[session.conversation_id] for session in sessions):
            watcher.step()
            time.sleep(0.25)
        print('chatgpt-web POC PASS: independent round trip completed in both tabs', flush=True)
    finally:
        interface.close()
