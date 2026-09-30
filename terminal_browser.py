from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from typing import Any, Callable

from chatgpt_web import (
    MESSAGE_TOO_LONG_TEXT,
    MessageTooLongError,
    WebSession,
    WebSessionWatcher,
)


DEFAULT_COMMAND = "terminal-browser"
CHATGPT_URL = "https" + "://chatgpt.com/"
TAB_START_TIMEOUT_SECONDS = 15.0
TAB_START_POLL_SECONDS = 0.1
SUBMIT_ATTEMPTS = 3
SUBMIT_HYDRATION_SETTLE_SECONDS = 0.5
SUBMIT_VERIFY_TIMEOUT_SECONDS = 2.0


@dataclass(eq=False)
class TerminalBrowserPage:
    interface: "TerminalBrowserChatGPT"
    browser_key: str
    tab_id: int
    target_id: str | None
    url: str
    cached_title: str = ""

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, TerminalBrowserPage)
            and self.browser_key == other.browser_key
            and self.tab_id == other.tab_id
        )

    def __hash__(self) -> int:
        return hash((self.browser_key, self.tab_id))

    def title(self) -> str:
        return self.cached_title

    def close(self) -> None:
        self.interface.close_page(self)


class TerminalBrowserChatGPT:
    """Experimental ChatGPT transport backed by terminal-browser/agent-browser."""

    name = "terminal-browser"

    def __init__(self, command: str = DEFAULT_COMMAND) -> None:
        self.command = command
        self._iterm_tab_ttys_cache: frozenset[str] | None = None

    def connect(self) -> None:
        if shutil.which(self.command) is None:
            raise RuntimeError(
                f"terminal-browser backend requires {self.command!r} on PATH"
            )

    def close(self) -> None:
        # The browser is user-owned. Never shut terminal-browser down from PGC.
        return

    def _run(
        self,
        args: list[str],
        *,
        check: bool = True,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        try:
            completed = subprocess.run(
                [self.command, *args],
                text=True,
                capture_output=True,
                check=False,
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"terminal-browser command failed: {exc}") from exc
        if check and completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                f"terminal-browser {' '.join(args[:3])} failed"
                + (f": {detail}" if detail else "")
            )
        return completed

    def _decode_ls_payload(
        self,
        completed: subprocess.CompletedProcess[str],
        *,
        command: str,
    ) -> dict[str, Any]:
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"terminal-browser {command} returned invalid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("browsers"), list):
            raise RuntimeError(f"terminal-browser {command} returned an unexpected payload")
        return payload

    def _ls_payload(self) -> dict[str, Any]:
        scoped = self._decode_ls_payload(
            self._run(["ls", "--json"]),
            command="ls --json",
        )
        if scoped["browsers"]:
            return scoped

        all_payload = self._decode_ls_payload(
            self._run(["ls", "--all", "--json"]),
            command="ls --all --json",
        )

        # Pixel does not currently know how to scope iTerm2 panes. Do not compensate by
        # adopting a globally unique browser: each PGC iTerm tab owns its own browser
        # instance. iTerm2 can tell us every TTY in the current tab, including the sibling
        # split where terminal-browser itself runs, so filter global discovery by that set.
        if self._iterm_session_uuid() is not None:
            tab_ttys = self._iterm_tab_ttys()
            browsers = [
                browser
                for browser in all_payload["browsers"]
                if isinstance(browser, dict) and browser.get("tty") in tab_ttys
            ]
            if len(browsers) > 1:
                raise RuntimeError(
                    "multiple terminal-browser instances belong to this iTerm2 tab; "
                    "close the extra browser split before running PGC"
                )
            return {**all_payload, "browsers": browsers}

        # Outside iTerm2, retain the previous conservative fallback for terminals whose
        # pane association is unavailable: a single global browser is unambiguous.
        browsers = all_payload["browsers"]
        if len(browsers) <= 1:
            return all_payload
        raise RuntimeError(
            "terminal-browser cannot identify a browser in this terminal pane, and "
            f"{len(browsers)} browsers are running"
        )

    @staticmethod
    def _page_from_row(
        interface: "TerminalBrowserChatGPT",
        browser: dict[str, Any],
        tab: dict[str, Any],
    ) -> TerminalBrowserPage | None:
        browser_key = browser.get("key")
        tab_id = tab.get("id")
        url = tab.get("url")
        if not isinstance(browser_key, str) or not browser_key:
            return None
        if not isinstance(tab_id, int):
            return None
        if not isinstance(url, str):
            return None
        target_id = tab.get("targetId")
        title = tab.get("title")
        return TerminalBrowserPage(
            interface=interface,
            browser_key=browser_key,
            tab_id=tab_id,
            target_id=target_id if isinstance(target_id, str) and target_id else None,
            url=url,
            cached_title=title if isinstance(title, str) else "",
        )

    def all_pages(self) -> list[TerminalBrowserPage]:
        payload = self._ls_payload()
        pages: list[TerminalBrowserPage] = []
        for browser in payload["browsers"]:
            if not isinstance(browser, dict):
                continue
            tabs = browser.get("tabs")
            if not isinstance(tabs, list):
                continue
            for tab in tabs:
                if not isinstance(tab, dict):
                    continue
                page = self._page_from_row(self, browser, tab)
                if page is not None:
                    pages.append(page)
        return pages

    @staticmethod
    def conversation_id_for_url(url: str) -> str | None:
        from chatgpt_web import ChatGPTWeb

        return ChatGPTWeb.conversation_id_for_url(url)

    def pages(self) -> list[TerminalBrowserPage]:
        return [
            page
            for page in self.all_pages()
            if self.conversation_id_for_url(page.url) is not None
        ]

    def describe_page(self, page: TerminalBrowserPage) -> tuple[str, str]:
        conversation_id = self.conversation_id_for_url(page.url)
        if conversation_id is None:
            raise RuntimeError(f"not a ChatGPT conversation URL: {page.url}")
        return conversation_id, page.cached_title.strip() or f"chat-{conversation_id[:8]}"

    def _selector_args(self, page: TerminalBrowserPage) -> list[str]:
        if page.target_id:
            return ["--target", page.target_id]
        return ["--browser", page.browser_key, "--tab", str(page.tab_id)]

    def action(
        self,
        page: TerminalBrowserPage,
        *command: str,
        timeout: float = 15.0,
    ) -> subprocess.CompletedProcess[str]:
        return self._run(
            ["action", *self._selector_args(page), "--", *command],
            timeout=timeout,
        )

    def eval(self, page: TerminalBrowserPage, expression: str) -> Any:
        completed = self.action(page, "eval", expression, "--json")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("terminal-browser action eval returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("terminal-browser action eval returned an unexpected payload")
        data = payload.get("data")
        if isinstance(data, dict) and "result" in data:
            return data["result"]
        if "result" in payload:
            return payload["result"]
        raise RuntimeError("terminal-browser action eval did not contain a result")

    @staticmethod
    def _transcript_script() -> str:
        return r"""(() => {
  const old = [...document.querySelectorAll('[data-message-author-role]')];
  const nodes = old.length ? old : [...document.querySelectorAll('[data-chatgpt-search-unit-key$=":user"], [data-chatgpt-search-unit-key$=":assistant"]')];
  const rows = [];
  for (const node of nodes.slice(-40)) {
    let role = node.getAttribute('data-message-author-role');
    const key = node.getAttribute('data-chatgpt-search-unit-key') || '';
    if (role !== 'user' && role !== 'assistant') {
      role = key.endsWith(':user') ? 'user' : key.endsWith(':assistant') ? 'assistant' : null;
    }
    if (!role) continue;
    let id = node.getAttribute('data-message-id');
    if (!id) {
      const ids = node.getAttribute('data-chatgpt-search-message-ids');
      if (ids) id = ids.trim().split(/\s+/)[0] || null;
    }
    if (!id) {
      const selected = node.querySelector('[data-chatgpt-selection-message-id]');
      if (selected) id = selected.getAttribute('data-chatgpt-selection-message-id');
    }
    let text = node.innerText || '';
    text = text.trim();
    for (const prefix of ['ChatGPT said:', 'You said:']) {
      if (text === prefix) text = '';
      else if (text.startsWith(prefix + '\n')) text = text.slice(prefix.length + 1).trim();
    }
    for (const suffix of ['Show more', 'Show less']) {
      if (text.endsWith('\n' + suffix)) text = text.slice(0, -(suffix.length + 1)).trimEnd();
    }
    rows.push({role, id, text});
  }
  return rows;
})()"""

    def transcript(self, session: WebSession) -> list[dict[str, Any]]:
        value = self.eval(session.page, self._transcript_script())
        if not isinstance(value, list):
            return []
        return [row for row in value if isinstance(row, dict)]

    @staticmethod
    def json_candidates_from_text(text: str) -> list[str]:
        from chatgpt_web import ChatGPTWeb

        return ChatGPTWeb.json_candidates_from_text(text)

    def _valid_request_from_record(
        self,
        record: dict[str, Any],
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        text = record.get("text")
        if not isinstance(text, str):
            return None
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
        message_id = record.get("id")
        identity = (message_id if isinstance(message_id, str) else "") + "\0" + source
        fingerprint = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return source, request, fingerprint

    def valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, Any, str] | None:
        rows = self.transcript(session)
        for record in reversed(rows):
            if record.get("role") == "assistant":
                return self._valid_request_from_record(record, validate_request)
        return None

    def recent_valid_request(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
        *,
        limit: int = 10,
    ) -> tuple[str, Any, str] | None:
        rows = [row for row in self.transcript(session) if row.get("role") == "assistant"]
        for record in reversed(rows[-limit:]):
            request = self._valid_request_from_record(record, validate_request)
            if request is not None:
                return request
        return None

    def latest_message_is_assistant(self, session: WebSession) -> bool:
        rows = self.transcript(session)
        return bool(rows) and rows[-1].get("role") == "assistant"

    def latest_user_message(self, session: WebSession) -> tuple[str | None, str] | None:
        for record in reversed(self.transcript(session)):
            if record.get("role") != "user":
                continue
            message_id = record.get("id")
            text = record.get("text")
            return (
                message_id if isinstance(message_id, str) else None,
                text if isinstance(text, str) else "",
            )
        return None

    @staticmethod
    def normalize_message_text(text: str) -> str:
        from chatgpt_web import ChatGPTWeb

        return ChatGPTWeb.normalize_message_text(text)

    def invalid_json_candidate(self, session: WebSession) -> tuple[str, str, str] | None:
        rows = self.transcript(session)
        record = next(
            (record for record in reversed(rows) if record.get("role") == "assistant"),
            None,
        )
        if record is None or not isinstance(record.get("text"), str):
            return None
        candidates: list[tuple[int, str, str]] = []
        for source in self.json_candidates_from_text(record["text"]):
            source = source.strip()
            looks_like_toolcall = any(marker in source for marker in ('"tool"', '"calls"', '"session"'))
            if not source or source[0] not in "[{" or not looks_like_toolcall:
                continue
            try:
                json.loads(source)
            except json.JSONDecodeError as exc:
                detail = f"{exc.msg} at line {exc.lineno}, column {exc.colno}"
                candidates.append((len(source), source, detail))
        if not candidates:
            return None
        _, source, detail = min(candidates, key=lambda item: item[0])
        message_id = record.get("id")
        identity = (message_id if isinstance(message_id, str) else "") + "\0invalid-json\0" + source
        return source, detail, hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def invalid_request_candidate(
        self,
        session: WebSession,
        validate_request: Callable[[Any], None],
    ) -> tuple[str, str, str] | None:
        rows = self.transcript(session)
        record = next(
            (record for record in reversed(rows) if record.get("role") == "assistant"),
            None,
        )
        if record is None or not isinstance(record.get("text"), str):
            return None
        candidates: list[tuple[int, str, str]] = []
        for source in self.json_candidates_from_text(record["text"]):
            source = source.strip()
            looks_like_toolcall = any(marker in source for marker in ('"tool"', '"calls"', '"session"'))
            if not source or source[0] not in "[{" or not looks_like_toolcall:
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
        message_id = record.get("id")
        identity = (message_id if isinstance(message_id, str) else "") + "\0invalid-request\0" + source
        return source, detail, hashlib.sha256(identity.encode("utf-8")).hexdigest()

    def is_generating(self, session: WebSession) -> bool:
        value = self.eval(
            session.page,
            r"""(() => {
  const candidates = [
    document.querySelector('button[data-testid="stop-button"]'),
    [...document.querySelectorAll('button')].find(b => ['Stop answering', 'Stop'].includes((b.getAttribute('aria-label') || b.innerText || '').trim()))
  ].filter(Boolean);
  return candidates.some(e => !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length));
})()""",
        )
        return value is True

    def terminal_response_candidate(
        self,
        session: WebSession,
        expected_user_message_id: str,
    ) -> tuple[str, str] | None:
        rows = self.transcript(session)
        expected_index: int | None = None
        for index in range(len(rows) - 1, -1, -1):
            record = rows[index]
            if record.get("role") != "user":
                continue
            if record.get("id") == expected_user_message_id:
                expected_index = index
                break
            return None
        if expected_index is None:
            return None
        for record in reversed(rows[expected_index + 1 :]):
            if record.get("role") != "assistant":
                continue
            text = record.get("text")
            message_id = record.get("id")
            if not isinstance(text, str) or not isinstance(message_id, str):
                return None
            identity = message_id + "\0terminal-response\0" + text
            return text, hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return None

    def latest_too_long_error_id(self, session: WebSession) -> str | None:
        value = self.eval(
            session.page,
            "(() => document.body.innerText.includes(" + json.dumps(MESSAGE_TOO_LONG_TEXT) + "))()",
        )
        if value is not True:
            return None
        latest_user = self.latest_user_message(session)
        if latest_user is not None and latest_user[0] is not None:
            return f"user:{latest_user[0]}"
        return "visible-too-long-banner"

    def emulate_active_page(self, page: TerminalBrowserPage) -> None:
        # terminal-browser already keeps the selected tab agent-accessible.
        return None

    def instrument_conversations_responses(self, page: TerminalBrowserPage) -> None:
        return

    def archive_conversation(self, session: WebSession) -> None:
        # Experimental backend: closing the terminal-browser tab is reliable, while
        # ChatGPT's archive affordance is UI-version-dependent. Keep archival best-effort
        # and non-fatal so subagent retirement cannot wedge the watcher.
        return

    def submit(self, session: WebSession, text: str) -> None:
        before = self.latest_user_message(session)
        normalized_text = self.normalize_message_text(text)

        for attempt in range(SUBMIT_ATTEMPTS):
            try:
                # Stay at terminal-browser's semantic layer: locate the active composer
                # by accessibility role and use native browser input. A freshly opened
                # ChatGPT tab can still be hydrating after the textbox first appears, so
                # let that settle before filling. Fill exactly once: on contenteditables,
                # repeating agent-browser's semantic fill can append instead of replace.
                time.sleep(SUBMIT_HYDRATION_SETTLE_SECONDS)
                self.action(session.page, "find", "role", "textbox", "fill", text)
                self.action(session.page, "press", "Enter")
            except RuntimeError:
                if self.latest_too_long_error_id(session) is not None:
                    raise MessageTooLongError(f"{session.label}: ChatGPT message is too long")
                if attempt + 1 >= SUBMIT_ATTEMPTS:
                    raise
                time.sleep(SUBMIT_HYDRATION_SETTLE_SECONDS)
                continue

            deadline = time.monotonic() + SUBMIT_VERIFY_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                latest = self.latest_user_message(session)
                if latest is not None:
                    changed = before is None or latest != before
                    if changed and self.normalize_message_text(latest[1]) == normalized_text:
                        return
                time.sleep(TAB_START_POLL_SECONDS)

            if self.latest_too_long_error_id(session) is not None:
                raise MessageTooLongError(f"{session.label}: ChatGPT message is too long")
            time.sleep(SUBMIT_HYDRATION_SETTLE_SECONDS)

        raise RuntimeError(f"{session.label}: ChatGPT did not record the submitted message")

    @staticmethod
    def _iterm_session_uuid() -> str | None:
        if os.environ.get("TERM_PROGRAM") != "iTerm.app":
            return None
        session_id = os.environ.get("ITERM_SESSION_ID", "").strip()
        if not session_id:
            return None
        return session_id.rsplit(":", 1)[-1] or None

    def _iterm_tab_ttys(self) -> frozenset[str]:
        cached = self._iterm_tab_ttys_cache
        if cached is not None:
            return cached

        session_uuid = self._iterm_session_uuid()
        if session_uuid is None:
            return frozenset()

        script = r'''on run argv
  set wanted to item 1 of argv
  set outputText to ""
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        set matched to false
        repeat with s in sessions of t
          if (unique ID of s as text) is wanted then set matched to true
        end repeat
        if matched then
          repeat with s in sessions of t
            if outputText is not "" then set outputText to outputText & linefeed
            set outputText to outputText & (tty of s as text)
          end repeat
          return outputText
        end if
      end repeat
    end repeat
  end tell
  error "current iTerm2 session not found"
end run'''
        try:
            completed = subprocess.run(
                ["osascript", "-e", script, "--", session_uuid],
                text=True,
                capture_output=True,
                check=False,
                timeout=TAB_START_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not inspect current iTerm2 tab: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                "could not inspect current iTerm2 tab"
                + (f": {detail}" if detail else "")
            )

        ttys = frozenset(line.strip() for line in completed.stdout.splitlines() if line.strip())
        if not ttys:
            raise RuntimeError("current iTerm2 tab has no terminal sessions")
        self._iterm_tab_ttys_cache = ttys
        return ttys

    def _launch_iterm_browser(self, url: str) -> None:
        session_uuid = self._iterm_session_uuid()
        if session_uuid is None:
            raise RuntimeError("cannot launch an iTerm2 split without ITERM_SESSION_ID")

        executable = shutil.which(self.command) or self.command
        browser_command = f"exec {shlex.quote(executable)} open {shlex.quote(url)}"
        script = r'''on run argv
  set wanted to item 1 of argv
  set browserCommand to item 2 of argv
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if unique ID of s is wanted then
            set created to split vertically with same profile s
            delay 0.75
            write created text browserCommand
            return unique ID of created
          end if
        end repeat
      end repeat
    end repeat
  end tell
  error "current iTerm2 session not found"
end run'''
        try:
            completed = subprocess.run(
                ["osascript", "-e", script, "--", session_uuid, browser_command],
                text=True,
                capture_output=True,
                check=False,
                timeout=TAB_START_TIMEOUT_SECONDS,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"could not create iTerm2 browser split: {exc}") from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            raise RuntimeError(
                "could not create iTerm2 browser split"
                + (f": {detail}" if detail else "")
            )
        # The new terminal-browser pane has its own TTY, so refresh tab membership before
        # polling for the browser instance that just appeared.
        self._iterm_tab_ttys_cache = None

    def _wait_for_new_page(
        self,
        before: set[tuple[str, int]],
        *,
        browser_key: str | None = None,
    ) -> TerminalBrowserPage:
        deadline = time.monotonic() + TAB_START_TIMEOUT_SECONDS
        while True:
            for page in self.all_pages():
                identity = (page.browser_key, page.tab_id)
                if identity in before:
                    continue
                if browser_key is not None and page.browser_key != browser_key:
                    continue
                if page.target_id is not None:
                    return page
            if time.monotonic() >= deadline:
                raise RuntimeError("terminal-browser did not expose the new tab in time")
            time.sleep(TAB_START_POLL_SECONDS)

    def _new_tab(
        self,
        url: str,
        *,
        browser_key: str | None = None,
    ) -> TerminalBrowserPage:
        existing = self.all_pages()
        before = {(page.browser_key, page.tab_id) for page in existing}
        if browser_key is None and not existing and self._iterm_session_uuid() is not None:
            self._launch_iterm_browser(url)
        else:
            args = ["new-tab"]
            if browser_key is not None:
                args.extend(["--browser", browser_key])
            args.append(url)
            self._run(args, timeout=TAB_START_TIMEOUT_SECONDS)
        return self._wait_for_new_page(before, browser_key=browser_key)

    def _session_for_page(
        self,
        page: TerminalBrowserPage,
        *,
        label: str | None = None,
    ) -> WebSession:
        conversation_id = self.conversation_id_for_url(page.url)
        return WebSession(
            conversation_id or f"terminal-browser:{page.browser_key}:{page.tab_id}",
            label or page.cached_title.strip() or "new chat",
            page,
        )

    def _wait_for_conversation(self, session: WebSession) -> WebSession:
        deadline = time.monotonic() + 30.0
        while True:
            for page in self.all_pages():
                if page == session.page:
                    session.page = page
                    conversation_id = self.conversation_id_for_url(page.url)
                    if conversation_id is not None:
                        session.conversation_id = conversation_id
                        if not session.label or session.label == "new chat":
                            session.label = page.cached_title.strip() or f"chat-{conversation_id[:8]}"
                        return session
                    break
            if time.monotonic() >= deadline:
                raise RuntimeError("ChatGPT did not assign a conversation id in time")
            time.sleep(TAB_START_POLL_SECONDS)

    def create_root_conversation(self, prompt: str) -> WebSession:
        pages = self.all_pages()
        browser_key = pages[0].browser_key if pages else None
        page = self._new_tab(CHATGPT_URL, browser_key=browser_key)
        session = self._session_for_page(page)
        self.submit(session, prompt)
        return self._wait_for_conversation(session)

    def create_conversation(
        self,
        *,
        origin: WebSession,
        label: str,
        prompt: str,
    ) -> WebSession:
        page = self._new_tab(CHATGPT_URL, browser_key=origin.page.browser_key)
        session = self._session_for_page(page, label=label)
        self.submit(session, prompt)
        return self._wait_for_conversation(session)

    def open_conversation(self, conversation_id: str) -> WebSession:
        pages = self.all_pages()
        browser_key = pages[0].browser_key if pages else None
        page = self._new_tab(
            "https" + f"://chatgpt.com/c/{conversation_id}",
            browser_key=browser_key,
        )
        deadline = time.monotonic() + TAB_START_TIMEOUT_SECONDS
        while self.conversation_id_for_url(page.url) != conversation_id:
            refreshed = next((candidate for candidate in self.all_pages() if candidate == page), None)
            if refreshed is not None:
                page = refreshed
            if time.monotonic() >= deadline:
                raise RuntimeError(
                    f"expected ChatGPT conversation {conversation_id}, opened {page.url}"
                )
            time.sleep(TAB_START_POLL_SECONDS)
        _, label = self.describe_page(page)
        return WebSession(conversation_id, label, page)

    def close_page(self, page: TerminalBrowserPage) -> None:
        self._run(
            [
                "action",
                "--browser",
                page.browser_key,
                "--tab",
                str(page.tab_id),
                "--",
                "tab",
                "close",
            ],
            timeout=10.0,
        )


def focus_open_root_conversation(
    *,
    session_id: str,
    validate_request: Callable[[Any], None],
    command: str = DEFAULT_COMMAND,
) -> str | None:
    interface = TerminalBrowserChatGPT(command)
    interface.connect()
    for page in interface.pages():
        conversation_id, label = interface.describe_page(page)
        session = WebSession(conversation_id, label, page)
        recent = interface.recent_valid_request(session, validate_request)
        if recent is None:
            continue
        _, request, _ = recent
        if WebSessionWatcher.request_session_id(request) == session_id:
            return conversation_id
    return None


def run_terminal_browser_watcher(
    *,
    session_id: str,
    bootstrap_prompt: str,
    validate_request: Callable[[Any], None],
    execute_request: Callable[..., Any],
    fenced_result: Callable[[Any], str],
    too_long_fallback: Callable[[Any, Any], str],
    bootstrap_prompt_for_session: Callable[[str], str] | None = None,
    on_root_terminal: Callable[[WebSession, str], None] | None = None,
    root_conversation_id: str | None = None,
    on_session_attached: Callable[[WebSession], None] | None = None,
    command: str = DEFAULT_COMMAND,
) -> None:
    interface = TerminalBrowserChatGPT(command)
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
            on_session_attached=on_session_attached,
        ).run(bootstrap_prompt=bootstrap_prompt)
    finally:
        interface.close()
