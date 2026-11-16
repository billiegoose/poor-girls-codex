"""ACP v1 stdio adapter for the ChatGPT-browser Poor Girl's Codex harness.

ACP owns the client-facing turn; the existing browser watcher owns model-facing
execution. All Playwright operations run on one worker thread. stdout is reserved
exclusively for newline-delimited JSON-RPC; diagnostics go to stderr.
"""
from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, TextIO

import poor_girls_codex as pgc
from acp_session_store import AcpSessionStore
from acp_tool_display import acp_content, display_input, display_name, display_output, display_title
from chatgpt_web import (
    ChatGPTWeb, DEFAULT_CDP_URL, DEFAULT_SETTLE_SECONDS, THINKING_EFFORTS,
    WebSession, WebSessionWatcher,
)


_PULL_REQUEST_LINKING_OPEN = {'<pull_request_linking>', r'\<pull_request_linking>'}
_PULL_REQUEST_LINKING_CLOSE = {'</pull_request_linking>', r'\</pull_request_linking>'}


def strip_pull_request_linking(prompt: str) -> str:
    """Remove complete standalone T3 PR-instruction blocks before browser submission.

    Preserve inline mentions and unclosed blocks, including their original line
    endings. A subsequent opening tag also leaves a prior unclosed block intact.
    """
    output: list[str] = []
    pending: list[str] = []
    for line in prompt.splitlines(keepends=True):
        tag = line.strip()
        if tag in _PULL_REQUEST_LINKING_OPEN:
            output.extend(pending)
            pending = [line]
        elif pending:
            pending.append(line)
            if tag in _PULL_REQUEST_LINKING_CLOSE:
                pending = []
        else:
            output.append(line)
    output.extend(pending)
    return ''.join(output)


@dataclass
class Turn:
    request_id: int | str
    prompt: str
    previous_user_id: str | None = None
    user_id: str | None = None
    cancelled: bool = False
    completion_candidate: tuple[str, str] | None = None
    completion_deadline: float = 0.0


class AcpSession:
    def __init__(
        self, server: AcpServer, session_id: str, cwd: str, *,
        routing_id: str | None = None, conversation_id: str | None = None,
        thinking_effort: str = 'high',
    ) -> None:
        self.server = server
        self.session_id = session_id
        self.cwd = cwd
        self.routing_id = routing_id or 'acp-' + secrets.token_hex(12)
        self.conversation_id = conversation_id
        self.turn: Turn | None = None
        self.web: WebSession | None = None
        self.watcher: WebSessionWatcher | None = None
        self.tool_counter = 0
        self.closing = False
        self.close_request_ids: list[int | str] = []
        self.thinking_effort = thinking_effort

    def config_options(self) -> list[dict[str, Any]]:
        return [{
            'id': 'thinking_effort', 'name': 'Thinking Effort',
            'category': 'thought_level', 'type': 'select',
            'description': 'ChatGPT thinking effort. Changes apply to the next browser submission.',
            'currentValue': self.thinking_effort,
            'options': [{'value': value, 'name': name} for value, name in THINKING_EFFORTS.items()],
        }]

    def sync_thinking_effort(self) -> None:
        # Called on the browser worker, including before tool-result delivery.
        # In-flight generations keep their effort; the next submission uses
        # the latest selection, including in this session's subagent tabs.
        if self.web is not None:
            self.web.thinking_effort = self.thinking_effort
        if self.watcher is not None:
            for web in self.watcher.sessions.values():
                web.thinking_effort = self.thinking_effort

    def validate(self, request: Any) -> None:
        pgc.validate_web_session_request(request, self.routing_id)
        calls, _, _, _ = pgc.request_calls(request)
        if any(isinstance(call, dict) and call.get('tool') == 'restart' for call in calls):
            raise ValueError('restart is unavailable in ACP mode')

    def bootstrap(self, task: str) -> str:
        # Retain the exact routing sentence recognized by the DOM recovery logic.
        # ACP owns restart and cancellation, so don't advertise PGC's restart tool.
        return (
            f'You are working on the `{Path(self.cwd).name}` codebase.\n'
            f'Working directory: {self.cwd}\n\n'
            + pgc.BOOTSTRAP_PROMPT
            + '\n\nAgent role:\nYou are the root coding agent. Finish each task with a normal prose response.\n'
            + '\nAdditional chatgpt-web tools:\n'
            + '- attach_image {path} - attach a local image to a tool result.\n'
            + '- camera {camera} - request an authorized board or monitor JPEG.\n'
            + '- subagent {name,prompt} - create a ChatGPT subagent.\n'
            + '\nWeb session routing:\n'
            + f'Every executable Poor Girl\'s Codex request MUST include the exact top-level field "session": "{self.routing_id}". '
            + f'This watcher accepts this session and descendants beginning with "{self.routing_id}::". '
            + 'Requests outside this session tree are ignored.\n\n'
            + '---\n\nUser request:\n' + task
        )

    def tool_request(self, request: Any, **kwargs: Any) -> Any:
        calls, _, _, _ = pgc.request_calls(request)
        ids: list[str] = []
        for index, call in enumerate(calls):
            self.tool_counter += 1
            call_id = f'{self.routing_id}-{self.tool_counter}'
            ids.append(call_id)
            name = call.get('tool', '?') if isinstance(call, dict) else '?'
            kind = {'read': 'read', 'find': 'search', 'tree': 'search',
                    'edit': 'edit', 'write': 'edit', 'run': 'execute'}.get(name, 'other')
            details = call if isinstance(call, dict) else {'tool': str(name)}
            self.server.update(self.session_id, {
                'sessionUpdate': 'tool_call', 'toolCallId': call_id,
                'name': display_name(details),
                'title': display_title(details, index),
                'kind': kind, 'status': 'in_progress',
                'rawInput': display_input(details),
            })
        try:
            result = pgc.execute_web_session_request(request, self.routing_id, **kwargs)
        except Exception as exc:
            for call_id in ids:
                self.server.update(self.session_id, {
                    'sessionUpdate': 'tool_call_update', 'toolCallId': call_id,
                    'status': 'failed', 'content': acp_content(f'Execution failed: {exc}'),
                })
            raise
        results = result if isinstance(result, list) else [result]
        for index, call_id in enumerate(ids):
            item = results[index] if index < len(results) and isinstance(results[index], dict) else {
                'ok': False, 'error': 'Skipped or missing tool result',
            }
            command_failed = calls[index].get('tool') == 'run' and item.get('exit_code', 0) != 0
            success = item.get('ok') is True and not command_failed
            name = calls[index].get('tool', '?')
            self.server.update(self.session_id, {
                'sessionUpdate': 'tool_call_update', 'toolCallId': call_id,
                'status': 'completed' if success else 'failed',
                'content': acp_content(display_output(name, item)),
                'rawOutput': item,
            })
        return result

    def start(self, interface: ChatGPTWeb) -> None:
        assert self.turn is not None
        self.sync_thinking_effort()
        browser_prompt = strip_pull_request_linking(self.turn.prompt)
        if self.watcher is None:
            self.watcher = WebSessionWatcher(
                interface, validate_request=self.validate, execute_request=self.tool_request,
                fenced_result=pgc.fenced_result,
                too_long_fallback=pgc.too_long_fallback_for_request,
                root_session_id=self.routing_id,
                bootstrap_prompt_for_session=pgc.subagent_web_bootstrap_prompt,
                on_root_terminal=lambda web, text: self.complete_from_watcher(interface, web, text),
                managed_only=True,
            )
        if self.web is None and self.conversation_id is not None:
            # Reattach to the original conversation after the ACP subprocess
            # exits. Never replace a known conversation with an empty one.
            web = interface.open_conversation(self.conversation_id)
            if web.conversation_id != self.conversation_id:
                try:
                    web.page.close()
                finally:
                    raise RuntimeError(
                        'Reopened ChatGPT conversation does not match the persisted ACP session'
                    )
            web.thinking_effort = self.thinking_effort
            self.watcher.sessions[web.conversation_id] = web
            try:
                self.watcher.bind_routing_session(web, self.routing_id)
                # Read the transcript before resuming: a completed call must
                # not execute twice, but a pending call must not silently vanish.
                protocol = interface.conversation_protocol_state(web, self.validate)
                if protocol.routing_session_id not in (None, self.routing_id):
                    raise RuntimeError(
                        'ChatGPT conversation belongs to a different PGC routing session'
                    )
                if protocol.request_pending:
                    raise RuntimeError(
                        'Recovered ChatGPT conversation has an unresolved tool request; '
                        'refusing to submit another prompt because replay could duplicate '
                        'side effects. Inspect the original ChatGPT conversation before '
                        'continuing or start a new T3 thread.'
                    )
                if protocol.recent_request is not None:
                    web.seen_fingerprints.add(protocol.recent_request[2])
                self.watcher.prime_recovered_terminal(web, protocol)
            except Exception:
                self.watcher.sessions.pop(web.conversation_id, None)
                web.page.close()
                raise
            self.web = web
            self.watcher.note_forward_progress(web, time.monotonic())
        if self.web is None:
            web = interface.create_root_conversation(
                self.bootstrap(browser_prompt),
                thinking_effort=self.thinking_effort,
                should_abort=lambda: (
                    self.server.stopping.is_set() or self.closing
                    or self.turn is None or self.turn.cancelled
                ),
            )
            self.web = web
            self.conversation_id = web.conversation_id
            self.server.persist_session(self)
            self.watcher.sessions[web.conversation_id] = web
            self.watcher.bind_routing_session(web, self.routing_id)
            self.watcher.note_forward_progress(web, time.monotonic())
        else:
            if interface.is_generating(self.web):
                raise RuntimeError('ChatGPT is still generating the previous response')
            latest = interface.latest_user_message(self.web)
            self.turn.previous_user_id = latest[0] if latest else None
            interface.submit(self.web, browser_prompt)
            self.watcher.note_forward_progress(self.web, time.monotonic())

    def complete(self, text: str, *, markdown: str | None = None) -> None:
        turn = self.turn
        if turn is None or turn.cancelled or not text.strip():
            # A newly mounted assistant node can be empty even after ChatGPT
            # hides its Stop button. Never acknowledge an empty ACP answer.
            return
        self.server.update(self.session_id, {
            'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': markdown if isinstance(markdown, str) and markdown.strip() else text},
        })
        self.server.respond(turn.request_id, {'stopReason': 'end_turn'})
        self.turn = None

    def complete_from_watcher(self, interface: ChatGPTWeb, web: WebSession, text: str) -> None:
        """Preserve formatting for the watcher's already-confirmed terminal answer.

        The watcher normally completes ACP before check_completion gets a turn.
        Only use Markdown from the same visible message as the accepted answer;
        otherwise retain the original confirmed text.
        """
        markdown = None
        if self.turn is not None and self.web is web:
            try:
                snapshot = interface.latest_assistant_snapshot(web)
                if snapshot is not None and snapshot.message_id and snapshot.text == text:
                    markdown = interface.markdown_from_assistant_snapshot(web, snapshot)
            except Exception as exc:
                print(f'  [acp] Markdown extraction failed: {exc}', file=sys.stderr)
        self.complete(text, markdown=markdown)

    def check_completion(self, interface: ChatGPTWeb) -> None:
        turn, web = self.turn, self.web
        if turn is None or web is None:
            return
        latest = interface.latest_user_message(web)
        if turn.user_id is None and latest and latest[0] != turn.previous_user_id:
            turn.user_id = latest[0]
        elif turn.user_id is not None and latest and latest[0] != turn.user_id:
            expectation = web.terminal_expectation
            if (
                expectation is not None
                and expectation.previous_user_message_id == turn.user_id
                and latest[0] is not None
                and interface.latest_user_matches_submission(web, expectation.submitted_text)
            ):
                # A tool-result submission creates a new ChatGPT user turn.
                # Bind completion to that turn, not the earlier ACP prompt.
                # Do this while generating too, so subsequent tool calls do not
                # leave the expectation permanently attached to the old turn.
                turn.user_id = latest[0]
        if turn.user_id is None or interface.is_generating(web) is True:
            turn.completion_candidate = None
            return
        if web.pending_responses or web.retire_requested:
            turn.completion_candidate = None
            return
        snapshot = interface.latest_assistant_snapshot(web)
        if snapshot is None or self.validate_snapshot_request(interface, snapshot):
            turn.completion_candidate = None
            return
        result = interface.terminal_response_from_snapshot(web, snapshot, turn.user_id)
        if result is None or not result[0].strip():
            turn.completion_candidate = None
            return
        now = time.monotonic()
        if turn.completion_candidate != result:
            # The DOM can briefly contain partial prose after the Stop button
            # disappears. Require an unchanged message across polling cycles.
            turn.completion_candidate = result
            turn.completion_deadline = now + DEFAULT_SETTLE_SECONDS
            return
        if now >= turn.completion_deadline:
            # Markdown is presentation-only. Completion remains tied to the
            # stable, identity-checked plain-text snapshot above.
            markdown = None
            try:
                markdown = interface.markdown_from_assistant_snapshot(web, snapshot)
            except Exception as exc:
                print(f'  [acp] Markdown extraction failed: {exc}', file=sys.stderr)
            self.complete(result[0], markdown=markdown)

    def validate_snapshot_request(self, interface: ChatGPTWeb, snapshot: Any) -> bool:
        return interface.valid_request_from_snapshot(snapshot, self.validate) is not None

    def cancel(self, interface: ChatGPTWeb | None) -> None:
        turn = self.turn
        if turn is None:
            return
        turn.cancelled = True
        if interface is not None and self.web is not None:
            try:
                button = interface.stop_button(self.web.page)
                if button.count() and button.last.is_visible():
                    button.last.evaluate('element => element.click()')
            except Exception as exc:
                print(f'  [acp] unable to stop ChatGPT generation: {exc}', file=sys.stderr)
            self.web.pending_responses.clear()
            self.web.last_submitted_responses.clear()
            self.web.terminal_expectation = None
        self.server.respond(turn.request_id, {'stopReason': 'cancelled'})
        self.turn = None

    def close_owned_tabs(self) -> None:
        """Close only pages created for this ACP session, on the Playwright thread.

        Do not close a page that the user navigated away from its original
        ChatGPT conversation. Do not touch other browser pages or contexts.
        """
        owned = list(self.watcher.sessions.values()) if self.watcher is not None else []
        if self.web is not None:
            owned.append(self.web)
        seen_pages: set[int] = set()
        failures: list[str] = []
        for web in owned:
            page = web.page
            if id(page) in seen_pages:
                continue
            seen_pages.add(id(page))
            try:
                if page.is_closed():
                    continue
                if ChatGPTWeb.conversation_id_for_url(str(page.url)) != web.conversation_id:
                    # The tab has been repurposed; it no longer belongs to ACP.
                    continue
                if web.cdp_session is not None:
                    try:
                        web.cdp_session.detach()
                    except Exception:
                        pass
                    web.cdp_session = None
                page.close()
            except Exception as exc:
                failures.append(f'{web.conversation_id}: {exc}')
        if failures:
            # Retain ownership records for a later retry (including shutdown).
            raise RuntimeError('unable to close ACP tabs: ' + '; '.join(failures))
        if self.watcher is not None:
            self.watcher.sessions.clear()
            self.watcher.sessions_by_routing_id.clear()
        self.web = None
        self.watcher = None


class AcpServer:
    def __init__(
        self, *, writer: TextIO, cdp_url: str = DEFAULT_CDP_URL,
        interface_factory: Callable[[str], ChatGPTWeb] = ChatGPTWeb,
        session_store: AcpSessionStore | None = None,
    ) -> None:
        self.writer = writer
        self.cdp_url = cdp_url
        self.interface_factory = interface_factory
        self.session_store = session_store if session_store is not None else AcpSessionStore()
        self.state_lock = threading.RLock()
        self.write_lock = threading.Lock()
        self.sessions: dict[str, AcpSession] = {}
        self.jobs: queue.Queue[tuple[str, str]] = queue.Queue()
        self.initialized = False
        self.stopping = threading.Event()
        self.worker: threading.Thread | None = None

    def send(self, message: dict[str, Any]) -> None:
        with self.write_lock:
            self.writer.write(json.dumps({'jsonrpc': '2.0', **message}, ensure_ascii=False) + '\n')
            self.writer.flush()

    def respond(self, request_id: int | str, result: dict[str, Any]) -> None:
        self.send({'id': request_id, 'result': result})

    def error(self, request_id: int | str | None, code: int, message: str) -> None:
        self.send({'id': request_id, 'error': {'code': code, 'message': message}})

    def update(self, session_id: str, update: dict[str, Any]) -> None:
        self.send({'method': 'session/update', 'params': {'sessionId': session_id, 'update': update}})

    def persist_session(self, session: AcpSession) -> None:
        with self.state_lock:
            self.session_store.save(
                session.session_id, session.cwd, session.routing_id,
                session.conversation_id, session.thinking_effort,
            )

    @staticmethod
    def params_object(message: dict[str, Any]) -> dict[str, Any]:
        params = message.get('params', {})
        if not isinstance(params, dict):
            raise ValueError('params must be an object')
        return params

    @staticmethod
    def prompt_text(prompt: Any) -> str:
        if not isinstance(prompt, list) or not prompt:
            raise ValueError('prompt must contain at least one text block')
        fragments = []
        for block in prompt:
            if not isinstance(block, dict) or block.get('type') != 'text' or not isinstance(block.get('text'), str):
                raise ValueError('PGC ACP currently supports text prompt blocks only')
            fragments.append(block['text'])
        return '\n'.join(fragments)

    def handle(self, message: Any) -> None:
        if not isinstance(message, dict) or message.get('jsonrpc') != '2.0' or not isinstance(message.get('method'), str):
            self.error(message.get('id') if isinstance(message, dict) else None, -32600, 'Invalid JSON-RPC request')
            return
        method, request_id = message['method'], message.get('id')
        is_request = 'id' in message
        if is_request and (isinstance(request_id, bool) or not isinstance(request_id, (str, int))):
            self.error(None, -32600, 'Invalid request ID')
            return
        try:
            params = self.params_object(message)
            if method == 'initialize':
                if not is_request:
                    return
                version = params.get('protocolVersion')
                if not isinstance(version, int) or isinstance(version, bool):
                    raise ValueError('protocolVersion must be an integer')
                if version < 1:
                    self.error(request_id, -32022, 'Unsupported protocol version')
                    return
                self.initialized = True
                self.respond(request_id, {
                    'protocolVersion': 1,
                    'agentCapabilities': {'sessionCapabilities': {'close': {}, 'resume': {}}},
                    'agentInfo': {'name': 'poor-girls-codex', 'title': "Poor Girl's Codex", 'version': '0.1.0'},
                    'authMethods': [],
                })
                return
            if not self.initialized:
                self.error(request_id if is_request else None, -32600, 'Initialize first')
                return
            if method == 'session/new':
                if not is_request:
                    return
                cwd = params.get('cwd')
                if not isinstance(cwd, str) or not os.path.isabs(cwd) or not Path(cwd).is_dir():
                    raise ValueError('cwd must be an existing absolute directory')
                if not isinstance(params.get('mcpServers', []), list):
                    raise ValueError('mcpServers must be an array')
                session_id = 'pgc-' + secrets.token_hex(12)
                session = AcpSession(self, session_id, str(Path(cwd).resolve()))
                self.persist_session(session)
                self.sessions[session_id] = session
                self.respond(request_id, {'sessionId': session_id, 'configOptions': session.config_options()})
                return
            if method == 'session/resume':
                if not is_request:
                    return
                session_id = params.get('sessionId')
                if not isinstance(session_id, str):
                    raise ValueError('sessionId must be a string')
                cwd = params.get('cwd')
                if not isinstance(cwd, str) or not os.path.isabs(cwd) or not Path(cwd).is_dir():
                    raise ValueError('cwd must be an existing absolute directory')
                if not isinstance(params.get('mcpServers', []), list):
                    raise ValueError('mcpServers must be an array')
                resolved_cwd = str(Path(cwd).resolve())
                session = self.sessions.get(session_id)
                if session is None:
                    record = self.session_store.load(session_id)
                    if record is None:
                        raise ValueError(
                            'Cannot resume unknown ACP sessionId (sessions created before '
                            'persistent restoration was implemented cannot be reconstructed automatically)'
                        )
                    if record['cwd'] != resolved_cwd:
                        raise ValueError('ACP session workspace does not match its original cwd')
                    session = AcpSession(
                        self, session_id, resolved_cwd,
                        routing_id=record['routingId'],
                        conversation_id=record['conversationId'],
                        thinking_effort=record['thinkingEffort'],
                    )
                    self.sessions[session_id] = session
                if session.cwd != resolved_cwd or session.closing:
                    raise ValueError('ACP session workspace mismatch or session is closing')
                self.respond(request_id, {'configOptions': session.config_options()})
                return
            if method == 'session/set_config_option':
                if not is_request:
                    return
                session = self.get_session(params)
                if session.closing:
                    self.error(request_id, -32000, 'This session is closing')
                    return
                if params.get('configId') != 'thinking_effort':
                    raise ValueError('Unknown configId')
                value = params.get('value')
                if not isinstance(value, str) or value not in THINKING_EFFORTS:
                    raise ValueError('Thinking Effort must be instant, medium, or high')
                with self.state_lock:
                    session.thinking_effort = value
                    self.persist_session(session)
                self.respond(request_id, {'configOptions': session.config_options()})
                return
            if method == 'session/prompt':
                if not is_request:
                    return
                session = self.get_session(params)
                if session.closing:
                    self.error(request_id, -32000, 'This session is closing')
                    return
                if session.turn is not None:
                    self.error(request_id, -32000, 'A prompt is already running in this session')
                    return
                session.turn = Turn(request_id, self.prompt_text(params.get('prompt')))
                self.jobs.put(('prompt', session.session_id))
                self.ensure_worker()
                return
            if method == 'session/cancel':
                session = self.get_session(params)
                if session.turn is not None:
                    session.turn.cancelled = True
                    self.jobs.put(('cancel', session.session_id))
                return
            if method == 'session/close':
                if not is_request:
                    return
                session_id = params.get('sessionId')
                if not isinstance(session_id, str) or not session_id:
                    raise ValueError('sessionId must be a non-empty string')
                session = self.sessions.get(session_id)
                if session is None:
                    self.respond(request_id, {})
                    return
                session.close_request_ids.append(request_id)
                if session.closing:
                    return
                session.closing = True
                if session.turn is not None:
                    session.turn.cancelled = True
                if session.web is None and session.turn is None:
                    self.finish_close(session, None)
                else:
                    self.jobs.put(('close', session_id))
                    self.ensure_worker()
                return
            if is_request:
                self.error(request_id, -32601, f'Method not found: {method}')
        except ValueError as exc:
            if is_request:
                self.error(request_id, -32602, str(exc))
        except OSError as exc:
            if is_request:
                self.error(request_id, -32000, f'ACP session persistence failed: {exc}')

    def get_session(self, params: dict[str, Any]) -> AcpSession:
        session_id = params.get('sessionId')
        if not isinstance(session_id, str) or session_id not in self.sessions:
            raise ValueError('Unknown sessionId')
        return self.sessions[session_id]

    def finish_close(self, session: AcpSession, interface: ChatGPTWeb | None) -> None:
        session.cancel(interface)
        try:
            session.close_owned_tabs()
        except Exception as exc:
            for request_id in session.close_request_ids:
                self.error(request_id, -32000, str(exc))
            session.close_request_ids.clear()
            session.closing = False
            return
        self.sessions.pop(session.session_id, None)
        for request_id in session.close_request_ids:
            self.respond(request_id, {})
        session.close_request_ids.clear()

    def ensure_worker(self) -> None:
        if self.worker is None:
            self.worker = threading.Thread(target=self.worker_loop, name='pgc-acp-browser', daemon=True)
            self.worker.start()

    def worker_loop(self) -> None:
        interface: ChatGPTWeb | None = None
        try:
            while not self.stopping.is_set():
                try:
                    action, session_id = self.jobs.get(timeout=0.15)
                except queue.Empty:
                    action, session_id = '', ''
                if action:
                    session = self.sessions.get(session_id)
                    if session is not None:
                        if action == 'cancel':
                            session.cancel(interface)
                        elif action == 'close':
                            self.finish_close(session, interface)
                        elif action == 'prompt' and session.turn is not None and not session.turn.cancelled and not session.closing:
                            first_start = session.web is None
                            try:
                                if interface is None:
                                    interface = self.interface_factory(self.cdp_url)
                                    interface.connect()
                                self.in_workspace(session, lambda: session.start(interface))
                            except Exception as exc:
                                if first_start and session.web is not None:
                                    try:
                                        session.close_owned_tabs()
                                    except Exception as cleanup_exc:
                                        print(f'  [acp] failed-start cleanup error: {cleanup_exc}', file=sys.stderr)
                                if session.turn is not None and (
                                    session.turn.cancelled or session.closing or self.stopping.is_set()
                                ):
                                    session.cancel(interface)
                                elif session.turn is not None:
                                    self.error(session.turn.request_id, -32000, f'ChatGPT browser: {exc}')
                                    session.turn = None
                if interface is None:
                    continue
                for session in list(self.sessions.values()):
                    if session.closing or session.turn is None or session.watcher is None:
                        continue
                    if session.turn.cancelled:
                        session.cancel(interface)
                        continue
                    try:
                        session.sync_thinking_effort()
                        self.in_workspace(session, lambda: (session.watcher.step(), session.check_completion(interface)))
                    except Exception as exc:
                        print(f'  [acp:{session.session_id}] watcher error: {exc}', file=sys.stderr)
                        # Retain the active turn: transient browser errors may recover.
        finally:
            if interface is not None:
                try:
                    for session in list(self.sessions.values()):
                        try:
                            session.close_owned_tabs()
                        except Exception as exc:
                            print(f'  [acp:{session.session_id}] shutdown cleanup error: {exc}', file=sys.stderr)
                finally:
                    interface.close()

    @staticmethod
    def in_workspace(session: AcpSession, operation: Callable[[], Any]) -> Any:
        original = os.getcwd()
        try:
            os.chdir(session.cwd)
            return operation()
        finally:
            os.chdir(original)

    def serve(self, reader: TextIO) -> None:
        try:
            for line in reader:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    self.error(None, -32700, 'Parse error')
                    continue
                self.handle(message)
        finally:
            self.stopping.set()
            if self.worker is not None:
                self.worker.join(timeout=10)


def serve_stdio(*, cdp_url: str = DEFAULT_CDP_URL) -> None:
    # The watcher and tool executor use ordinary print() for diagnostic output.
    # Keep an untouched handle for JSON-RPC and redirect all other output.
    wire = sys.stdout
    sys.stdout = sys.stderr
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def stop_on_sigterm(_signum: int, _frame: Any) -> None:
        # Raise on the main thread, unwinding serve() through its cleanup block.
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop_on_sigterm)
    try:
        AcpServer(writer=wire, cdp_url=cdp_url).serve(sys.stdin)
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)


if __name__ == '__main__':
    serve_stdio()
