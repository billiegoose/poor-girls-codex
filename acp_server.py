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
from chatgpt_web import ChatGPTWeb, DEFAULT_CDP_URL, WebSession, WebSessionWatcher


@dataclass
class Turn:
    request_id: int | str
    prompt: str
    previous_user_id: str | None = None
    user_id: str | None = None
    cancelled: bool = False


class AcpSession:
    def __init__(self, server: AcpServer, session_id: str, cwd: str) -> None:
        self.server = server
        self.session_id = session_id
        self.cwd = cwd
        self.routing_id = 'acp-' + secrets.token_hex(12)
        self.turn: Turn | None = None
        self.web: WebSession | None = None
        self.watcher: WebSessionWatcher | None = None
        self.tool_counter = 0
        self.closing = False
        self.close_request_ids: list[int | str] = []

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
            self.server.update(self.session_id, {
                'sessionUpdate': 'tool_call', 'toolCallId': call_id,
                'title': f'{name}: {call.get("id", index)}' if isinstance(call, dict) else str(name),
                'kind': kind, 'status': 'in_progress',
            })
        try:
            result = pgc.execute_web_session_request(request, self.routing_id, **kwargs)
        except Exception:
            for call_id in ids:
                self.server.update(self.session_id, {
                    'sessionUpdate': 'tool_call_update', 'toolCallId': call_id, 'status': 'failed',
                })
            raise
        results = result if isinstance(result, list) else [result]
        for index, call_id in enumerate(ids):
            success = index < len(results) and isinstance(results[index], dict) and results[index].get('ok') is True
            self.server.update(self.session_id, {
                'sessionUpdate': 'tool_call_update', 'toolCallId': call_id,
                'status': 'completed' if success else 'failed',
            })
        return result

    def start(self, interface: ChatGPTWeb) -> None:
        assert self.turn is not None
        if self.watcher is None:
            self.watcher = WebSessionWatcher(
                interface, validate_request=self.validate, execute_request=self.tool_request,
                fenced_result=pgc.fenced_result,
                too_long_fallback=pgc.too_long_fallback_for_request,
                root_session_id=self.routing_id,
                bootstrap_prompt_for_session=pgc.subagent_web_bootstrap_prompt,
                on_root_terminal=lambda _session, text: self.complete(text),
                managed_only=True,
            )
        if self.web is None:
            web = interface.create_root_conversation(
                self.bootstrap(self.turn.prompt),
                should_abort=lambda: (
                    self.server.stopping.is_set() or self.closing
                    or self.turn is None or self.turn.cancelled
                ),
            )
            self.web = web
            self.watcher.sessions[web.conversation_id] = web
            self.watcher.bind_routing_session(web, self.routing_id)
            self.watcher.note_forward_progress(web, time.monotonic())
        else:
            if interface.is_generating(self.web):
                raise RuntimeError('ChatGPT is still generating the previous response')
            latest = interface.latest_user_message(self.web)
            self.turn.previous_user_id = latest[0] if latest else None
            interface.submit(self.web, self.turn.prompt)
            self.watcher.note_forward_progress(self.web, time.monotonic())

    def complete(self, text: str) -> None:
        turn = self.turn
        if turn is None or turn.cancelled:
            return
        self.server.update(self.session_id, {
            'sessionUpdate': 'agent_message_chunk',
            'content': {'type': 'text', 'text': text},
        })
        self.server.respond(turn.request_id, {'stopReason': 'end_turn'})
        self.turn = None

    def check_completion(self, interface: ChatGPTWeb) -> None:
        turn, web = self.turn, self.web
        if turn is None or web is None:
            return
        latest = interface.latest_user_message(web)
        if turn.user_id is None and latest and latest[0] != turn.previous_user_id:
            turn.user_id = latest[0]
        if turn.user_id is None or interface.is_generating(web) is True:
            return
        if web.pending_responses or web.retire_requested:
            return
        snapshot = interface.latest_assistant_snapshot(web)
        if snapshot is None or self.validate_snapshot_request(interface, snapshot):
            return
        result = interface.terminal_response_from_snapshot(web, snapshot, turn.user_id)
        if result is not None:
            self.complete(result[0])

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
    ) -> None:
        self.writer = writer
        self.cdp_url = cdp_url
        self.interface_factory = interface_factory
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
                    'agentCapabilities': {'sessionCapabilities': {'close': {}}},
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
                self.sessions[session_id] = AcpSession(self, session_id, str(Path(cwd).resolve()))
                self.respond(request_id, {'sessionId': session_id})
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
