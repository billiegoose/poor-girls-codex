from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse


ASSISTANT_SELECTOR = '[data-message-author-role="assistant"]'
COMPOSER_SELECTOR = '#prompt-textarea'
SEND_SELECTOR = 'button[data-testid="send-button"]'
DEFAULT_CDP_URL = 'http' + '://127.0.0.1:9222'
DEFAULT_SETTLE_SECONDS = 0.35
DEFAULT_POLL_SECONDS = 0.25
DELIVERY_RETRY_INITIAL_SECONDS = 0.25
DELIVERY_RETRY_MAX_SECONDS = 8.0


class MessageTooLongError(RuntimeError):
    pass


@dataclass
class WebSession:
    conversation_id: str
    label: str
    page: Any
    seen_fingerprints: set[str] = field(default_factory=set)
    pending_results: list[str] = field(default_factory=list)
    pending_fallbacks: list[str] = field(default_factory=list)
    settling_fingerprint: str | None = None
    settle_deadline: float = 0.0
    delivery_attempt: int = 0
    next_delivery_at: float = 0.0
    delivered: int = 0

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

    def latest_json_candidates(self, session: WebSession) -> tuple[str | None, list[str]]:
        messages = session.page.locator(ASSISTANT_SELECTOR)
        if messages.count() == 0:
            return None, []
        latest = messages.last
        message_id = latest.get_attribute('data-message-id')
        candidates: list[str] = []
        code = latest.locator('pre code')
        for index in range(code.count()):
            text = code.nth(index).inner_text().strip()
            if text:
                candidates.append(text)
        if candidates:
            return message_id, candidates
        pre = latest.locator('pre')
        for index in range(pre.count()):
            text = pre.nth(index).inner_text().strip()
            if text:
                candidates.append(text)
        return message_id, candidates

    def valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        message_id, sources = self.latest_json_candidates(session)
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

    def submit(self, session: WebSession, text: str) -> None:
        self.set_composer_text(session, text)
        button = session.page.locator(SEND_SELECTOR)
        if button.count() == 0:
            button = session.page.get_by_role('button', name='Send prompt')
        if button.count() == 0:
            raise RuntimeError(f'{session.label}: ChatGPT send button is not available')
        if not button.last.is_enabled():
            if button.last.get_attribute('aria-disabled') == 'true' and text:
                raise MessageTooLongError(f'{session.label}: ChatGPT message is too long')
            raise RuntimeError(f'{session.label}: ChatGPT send button is not available')
        button.last.click()


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
        settle_seconds: float = DEFAULT_SETTLE_SECONDS,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interface = interface
        self.validate_request = validate_request
        self.execute_request = execute_request
        self.fenced_result = fenced_result
        self.too_long_fallback = too_long_fallback
        self.settle_seconds = settle_seconds
        self.poll_seconds = poll_seconds
        self.clock = clock
        self.sessions: dict[str, WebSession] = {}

    def refresh_sessions(self, *, prime_new: bool = True) -> None:
        live: set[str] = set()
        for page in self.interface.pages():
            conversation_id, label = self.interface.describe_page(page)
            live.add(conversation_id)
            session = self.sessions.get(conversation_id)
            if session is None:
                session = WebSession(conversation_id=conversation_id, label=label, page=page)
                self.sessions[conversation_id] = session
                if prime_new:
                    existing = self.interface.valid_request(session, self.validate_request)
                    if existing is not None:
                        session.seen_fingerprints.add(existing[2])
                print(f'  [web] attached {session.label} ({conversation_id[:8]})', flush=True)
            else:
                session.page = page
                session.label = label

        for conversation_id in list(self.sessions):
            if conversation_id not in live:
                session = self.sessions.pop(conversation_id)
                print(f'  [web] detached {session.label} ({conversation_id[:8]})', flush=True)

    def scan_session(self, session: WebSession, now: float) -> bool:
        candidate = self.interface.valid_request(session, self.validate_request)
        if candidate is None:
            session.settling_fingerprint = None
            return False

        _, request, fingerprint = candidate
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
        print(f'  [web:{session.label}] tool calls:', flush=True)
        result = self.execute_request(request, announce=True)
        session.pending_results.append(self.fenced_result(result))
        session.pending_fallbacks.append(self.too_long_fallback(request, result))
        return True

    def deliver_session(self, session: WebSession, now: float) -> bool:
        if not session.pending_results or now < session.next_delivery_at:
            return False
        rendered = '\n'.join(part.rstrip('\n') for part in session.pending_results) + '\n'
        try:
            self.interface.submit(session, rendered)
        except MessageTooLongError:
            fallback = '\n'.join(part.rstrip('\n') for part in session.pending_fallbacks) + '\n'
            print(
                f'  [web:{session.label}] full results too large; sending compact summary',
                flush=True,
            )
            try:
                self.interface.submit(session, fallback)
            except RuntimeError as exc:
                delay = min(
                    DELIVERY_RETRY_INITIAL_SECONDS * (2**session.delivery_attempt),
                    DELIVERY_RETRY_MAX_SECONDS,
                )
                session.delivery_attempt += 1
                session.next_delivery_at = now + delay
                print(
                    f'  [web:{session.label}] compact delivery deferred ({exc}); retrying in {delay:g}s',
                    flush=True,
                )
                return False
        except RuntimeError as exc:
            delay = min(
                DELIVERY_RETRY_INITIAL_SECONDS * (2**session.delivery_attempt),
                DELIVERY_RETRY_MAX_SECONDS,
            )
            session.delivery_attempt += 1
            session.next_delivery_at = now + delay
            print(
                f'  [web:{session.label}] delivery deferred ({exc}); retrying in {delay:g}s',
                flush=True,
            )
            return False

        session.pending_results.clear()
        session.pending_fallbacks.clear()
        session.delivery_attempt = 0
        session.next_delivery_at = 0.0
        session.delivered += 1
        print(f'  [web:{session.label}] sent results', flush=True)
        return True

    def step(self) -> bool:
        self.refresh_sessions()
        now = self.clock()
        progressed = False

        # Scan every conversation before delivering anything. A slow/busy tab cannot
        # redirect another tab's state, and simultaneous model turns are observed fairly.
        for session in list(self.sessions.values()):
            try:
                progressed = self.scan_session(session, now) or progressed
            except Exception as exc:
                print(f'  [web:{session.label}] scan error: {exc}', flush=True)

        for session in list(self.sessions.values()):
            try:
                progressed = self.deliver_session(session, now) or progressed
            except Exception as exc:
                print(f'  [web:{session.label}] delivery error: {exc}', flush=True)

        return progressed

    def run(self) -> None:
        self.refresh_sessions()
        print(
            f'chatgpt-web watcher ready: {len(self.sessions)} conversation tab(s); '
            'new conversation tabs are discovered automatically',
            flush=True,
        )
        if not self.sessions:
            print('  [web] waiting for a ChatGPT /c/... conversation tab', flush=True)
        try:
            while True:
                progressed = self.step()
                if not progressed:
                    time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
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
        ).run()
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
