from __future__ import annotations

import hashlib
import json
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from progress_ui import ResponseProgress, color as progress_color


ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
COMPOSER_SELECTOR = '#prompt-textarea'
SEND_SELECTOR = 'button[data-testid="send-button"]'
DEFAULT_CDP_URL = 'http' + '://127.0.0.1:9222'
DEFAULT_SETTLE_SECONDS = 0.35
DEFAULT_POLL_SECONDS = 0.25
RECENT_ASSISTANT_MESSAGE_LIMIT = 10
DELIVERY_RETRY_INITIAL_SECONDS = 0.25
DELIVERY_RETRY_MAX_SECONDS = 8.0
MESSAGE_TOO_LONG_TEXT = 'The message you submitted was too long, please edit it and resubmit.'
SUBAGENT_COLOR_CODES = ('1;34', '1;35', '1;36', '1;33', '1;32')


class MessageTooLongError(RuntimeError):
    pass


@dataclass
class PendingResponse:
    result: str
    fallback: str
    progress: ResponseProgress | None = None
    compact: bool = False

    def render(self) -> str:
        return self.fallback if self.compact else self.result

    def set_status(self, status: str) -> None:
        if self.progress is not None:
            self.progress.set_status(status)


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
    delivered: int = 0
    routing_session_id: str | None = None
    retire_requested: bool = False
    cdp_session: Any | None = None

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

    def connect(self) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ModuleNotFoundError as exc:
            raise RuntimeError(
                'chatgpt-web requires Playwright; install project dependencies with uv sync'
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.connect_over_cdp(
                self.cdp_url,
                no_defaults=True,
            )
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
        return parts[1]

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

    @staticmethod
    def json_candidates_for_message(message: Any) -> tuple[str | None, list[str]]:
        message_id = message.get_attribute('data-message-id')
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
        return message_id, candidates

    def latest_json_candidates(self, session: WebSession) -> tuple[str | None, list[str]]:
        messages = session.page.locator(ASSISTANT_SELECTOR)
        if messages.count() == 0:
            return None, []
        return self.json_candidates_for_message(messages.last)

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

    def valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        messages = session.page.locator(ASSISTANT_SELECTOR)
        if messages.count() == 0:
            return None
        return self.valid_request_from_message(messages.last, validate_request)

    def recent_valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
        *,
        limit: int = RECENT_ASSISTANT_MESSAGE_LIMIT,
    ) -> tuple[str, Any, str] | None:
        messages = session.page.locator(ASSISTANT_SELECTOR)
        count = messages.count()
        start = max(0, count - limit)
        for index in range(count - 1, start - 1, -1):
            request = self.valid_request_from_message(messages.nth(index), validate_request)
            if request is not None:
                return request
        return None

    def set_composer_text(self, session: WebSession, text: str) -> None:
        composer = session.page.locator(COMPOSER_SELECTOR).last
        if composer.count() == 0:
            raise RuntimeError(f'{session.label}: ChatGPT composer not found')
        composer.fill(text)

    def can_submit(self, session: WebSession) -> bool:
        button = session.page.locator(SEND_SELECTOR)
        if button.count() == 0:
            button = session.page.get_by_role('button', name='Send prompt')
        return button.count() > 0 and button.last.is_enabled()

    def latest_too_long_error_id(self, session: WebSession) -> str | None:
        matches = session.page.get_by_text(MESSAGE_TOO_LONG_TEXT, exact=True)
        for index in range(matches.count() - 1, -1, -1):
            match = matches.nth(index)
            if not match.is_visible():
                continue
            return match.evaluate(
                "e => e.closest('[data-message-author-role=\\\"assistant\\\"]')?.getAttribute('data-message-id') || null"
            )
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
            button = session.page.locator(SEND_SELECTOR)
            if button.count() == 0:
                button = session.page.get_by_role('button', name='Send prompt')
            return button

        button = send_button()
        if button.count() == 0 or not button.last.is_enabled():
            # ChatGPT sometimes does not materialize an enabled send button for a
            # genuinely background Chrome tab. Focusing that tab is enough to make
            # the composer controls catch up, so automate that only as a fallback.
            session.page.bring_to_front()
            button = send_button()

        if button.count() == 0:
            raise RuntimeError(f'{session.label}: ChatGPT send button is not available')
        if not button.last.is_enabled():
            if button.last.get_attribute('aria-disabled') == 'true' and text:
                raise MessageTooLongError(f'{session.label}: ChatGPT message is too long')
            raise RuntimeError(f'{session.label}: ChatGPT send button is not available')
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
        cdp_session = self.emulate_active_page(page) if emulate_active else None
        try:
            page.goto('https' + '://chatgpt.com/', wait_until='domcontentloaded')
            page.locator(COMPOSER_SELECTOR).last.wait_for(state='visible', timeout=15_000)
            pending = WebSession('', label or 'new chat', page, cdp_session=cdp_session)
            self.submit(pending, prompt)
            page.wait_for_url(
                lambda url: self.conversation_id_for_url(str(url)) is not None,
                timeout=30_000,
            )
            # Do not let the next subagent steal foreground focus until this
            # conversation's assistant turn has actually started. ChatGPT can
            # otherwise leave a just-submitted background tab dormant forever.
            page.locator(ASSISTANT_SELECTOR).last.wait_for(state='attached', timeout=30_000)
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

    def archive_conversation(self, session: WebSession) -> None:
        result = session.page.evaluate(
            """async conversationId => {
                const response = await fetch(`/backend-api/conversation/${conversationId}`, {
                    method: 'PATCH',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({is_archived: true}),
                });
                return {ok: response.ok, status: response.status, text: await response.text()};
            }""",
            session.conversation_id,
        )
        if not result.get('ok'):
            status = result.get('status', '?')
            text = str(result.get('text', '')).strip()
            detail = f': {text}' if text else ''
            raise RuntimeError(
                f'{session.label}: failed to archive ChatGPT conversation ({status}){detail}'
            )


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
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sessions: dict[str, WebSession] = {}
        self.sessions_by_routing_id: dict[str, WebSession] = {}

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

        for session in self.sessions.values():
            recent = self.interface.recent_valid_request(session, self.validate_request)
            if recent is None:
                continue
            _, request, fingerprint = recent
            routing_session_id = self.request_session_id(request)
            if routing_session_id != self.root_session_id:
                continue
            self.bind_routing_session(session, routing_session_id)
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
            )
            child = self.interface.create_conversation(
                origin=origin,
                label=f'subagent:{name}',
                prompt=prompt,
            )
            child.routing_session_id = child_session_id
            self.sessions[child.conversation_id] = child
            self.sessions_by_routing_id[child_session_id] = child
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
            conversation_id, label = self.interface.describe_page(page)
            live.add(conversation_id)
            session = self.sessions.get(conversation_id)
            if session is None:
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
                    if prime_new:
                        existing = self.interface.recent_valid_request(session, self.validate_request)
                        if existing is not None:
                            _, request, fingerprint = existing
                            routing_session_id = self.request_session_id(request)
                            if routing_session_id is not None:
                                self.bind_routing_session(session, routing_session_id)
                            session.seen_fingerprints.add(fingerprint)
            else:
                session.page = page
                session.label = label

        for conversation_id in list(self.sessions):
            if conversation_id not in live:
                session = self.sessions.pop(conversation_id)
                if session.routing_session_id is not None:
                    self.sessions_by_routing_id.pop(session.routing_session_id, None)

    def scan_session(self, session: WebSession, now: float) -> bool:
        candidate = self.interface.valid_request(session, self.validate_request)
        if candidate is None:
            session.settling_fingerprint = None
            return False

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
        error_id = self.interface.latest_too_long_error_id(session)
        if error_id is None or error_id == session.last_too_long_error_id:
            return False
        session.last_too_long_error_id = error_id
        if not session.last_submitted_responses:
            return False

        restored = session.last_submitted_responses
        session.last_submitted_responses = []
        for response in restored:
            response.compact = True
            response.set_status('Message too large [retry with summary]')
        session.pending_responses = restored + session.pending_responses
        return True

    def deliver_session(self, session: WebSession, now: float) -> bool:
        if not session.pending_responses or now < session.next_delivery_at:
            return False

        rendered = '\n'.join(
            response.render().rstrip('\n') for response in session.pending_responses
        ) + '\n'
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
            except RuntimeError:
                delay = min(
                    DELIVERY_RETRY_INITIAL_SECONDS * (2**session.delivery_attempt),
                    DELIVERY_RETRY_MAX_SECONDS,
                )
                session.delivery_attempt += 1
                session.next_delivery_at = now + delay
                for response in session.pending_responses:
                    response.set_status(
                        f'ChatGPT send button is not available [retry in {delay:g}s]'
                    )
                return False
            for response in session.pending_responses:
                response.set_status('[sent summary]')
            session.last_submitted_responses = []
        except RuntimeError:
            delay = min(
                DELIVERY_RETRY_INITIAL_SECONDS * (2**session.delivery_attempt),
                DELIVERY_RETRY_MAX_SECONDS,
            )
            session.delivery_attempt += 1
            session.next_delivery_at = now + delay
            for response in session.pending_responses:
                response.set_status(
                    f'ChatGPT send button is not available [retry in {delay:g}s]'
                )
            return False
        else:
            for response in session.pending_responses:
                response.set_status('[sent summary]' if response.compact else '[sent]')
            session.last_submitted_responses = list(session.pending_responses)

        session.pending_responses.clear()
        session.delivery_attempt = 0
        session.next_delivery_at = 0.0
        session.delivered += 1
        return True

    def step(self) -> bool:
        self.refresh_sessions()
        now = self.clock()
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
            except Exception as exc:
                print(f'  [web:{session.label}] scan error: {exc}', flush=True)

        for session in list(self.sessions.values()):
            try:
                progressed = self.deliver_session(session, now) or progressed
            except Exception as exc:
                print(f'  [web:{session.label}] delivery error: {exc}', flush=True)

        return progressed

    def run(self, *, bootstrap_prompt: str = '') -> None:
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
        except KeyboardInterrupt:
            # A second Ctrl+C deliberately forces an immediate exit.
            pass
        finally:
            signal.signal(signal.SIGINT, previous_sigint)

        print("\nPoor Girl's Codex web watcher stopped.")


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
