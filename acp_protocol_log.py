"""Append-only, metadata-only JSONL trace for ACP stdio traffic.

The trace deliberately excludes prompt text, assistant text, tool arguments,
result bodies, and error messages. A successful outbound record means PGC
flushed the JSON-RPC frame, not that the client consumed it.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def default_protocol_log_path() -> Path:
    configured = os.environ.get('PGC_ACP_LOG_PATH')
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError('PGC_ACP_LOG_PATH must be absolute')
        return path
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Logs' / 'poor-girls-codex' / 'acp-protocol.jsonl'
    state_root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return state_root / 'poor-girls-codex' / 'acp-protocol.jsonl'


class AcpProtocolLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, 'O_NOFOLLOW'):
            flags |= os.O_NOFOLLOW
        fd = os.open(self.path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            self._stream = os.fdopen(fd, 'a', encoding='utf-8', buffering=1)
        except BaseException:
            os.close(fd)
            raise
        self._lock = threading.Lock()
        self._failed = False

    def record(
        self, direction: str, message: Any = None, *,
        frame_bytes: int | None = None, write_error: str | None = None,
    ) -> None:
        event: dict[str, Any] = {
            'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'pid': os.getpid(),
            'direction': direction,
        }
        if frame_bytes is not None:
            event['frameBytes'] = frame_bytes
        if write_error is not None:
            event['writeErrorType'] = write_error
        if isinstance(message, dict):
            if 'id' in message:
                event['id'] = message['id']
            method = message.get('method')
            if isinstance(method, str):
                event['method'] = method
            params = message.get('params')
            if isinstance(params, dict):
                if isinstance(params.get('sessionId'), str):
                    event['sessionId'] = params['sessionId']
                update = params.get('update')
                if isinstance(update, dict):
                    for key in ('sessionUpdate', 'toolCallId', 'kind', 'status'):
                        if isinstance(update.get(key), str):
                            event[key] = update[key]
            result = message.get('result')
            if isinstance(result, dict):
                for key in ('stopReason', 'sessionId', 'protocolVersion'):
                    if key in result and isinstance(result[key], (str, int)):
                        event[key] = result[key]
            error = message.get('error')
            if isinstance(error, dict) and isinstance(error.get('code'), int):
                event['errorCode'] = error['code']
        with self._lock:
            if self._failed:
                return
            try:
                self._stream.write(json.dumps(event, ensure_ascii=False) + '\n')
                self._stream.flush()
            except (OSError, ValueError) as exc:
                self._failed = True
                print(f'  [acp] protocol logging failed: {type(exc).__name__}: {exc}', file=sys.stderr)

    def close(self) -> None:
        with self._lock:
            self._stream.close()
