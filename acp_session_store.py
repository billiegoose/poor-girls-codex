"""Durable, per-user ACP session identities for reconnecting T3 to ChatGPT.

One file per session avoids lost updates when separate ACP subprocesses serve
separate T3 threads. Files contain only identifiers and workspace paths, not
prompts, conversation content, credentials, or tool results.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

SESSION_ID_PATTERN = re.compile(r'pgc-[0-9a-f]{24}\Z')
ROUTING_ID_PATTERN = re.compile(r'acp-[0-9a-f]{24}\Z')
THINKING_EFFORTS = {'instant', 'medium', 'high'}


def default_store_directory() -> Path:
    configured = os.environ.get('PGC_ACP_SESSION_DIR')
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute():
            raise ValueError('PGC_ACP_SESSION_DIR must be an absolute path')
        return path
    if sys.platform == 'darwin':
        return Path.home() / 'Library' / 'Application Support' / 'poor-girls-codex' / 'acp-sessions'
    root = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state')
    return root / 'poor-girls-codex' / 'acp-sessions'


class AcpSessionStore:
    def __init__(self, directory: Path | None = None) -> None:
        self.directory = Path(directory) if directory is not None else default_store_directory()

    def path_for(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or SESSION_ID_PATTERN.fullmatch(session_id) is None:
            raise ValueError('Invalid ACP sessionId')
        return self.directory / f'{session_id}.json'

    def load(self, session_id: str) -> dict[str, Any] | None:
        path = self.path_for(session_id)
        if path.is_symlink():
            raise ValueError('Refusing to load an ACP session through a symlink')
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return None
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError(f'Corrupt ACP session record: {session_id}') from exc
        if (
            not isinstance(record, dict)
            or record.get('version') != 1
            or record.get('sessionId') != session_id
            or not isinstance(record.get('cwd'), str)
            or not os.path.isabs(record['cwd'])
            or not isinstance(record.get('routingId'), str)
            or ROUTING_ID_PATTERN.fullmatch(record['routingId']) is None
            or (record.get('conversationId') is not None and (
                not isinstance(record['conversationId'], str)
                or re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', record['conversationId']) is None
            ))
            or record.get('thinkingEffort') not in THINKING_EFFORTS
        ):
            raise ValueError(f'Invalid ACP session record: {session_id}')
        return record

    def save(
        self, session_id: str, cwd: str, routing_id: str,
        conversation_id: str | None, thinking_effort: str,
    ) -> None:
        path = self.path_for(session_id)
        record = {
            'version': 1, 'sessionId': session_id, 'cwd': cwd,
            'routingId': routing_id, 'conversationId': conversation_id,
            'thinkingEffort': thinking_effort,
        }
        if ROUTING_ID_PATTERN.fullmatch(routing_id) is None:
            raise ValueError('Invalid ACP routing ID')
        if thinking_effort not in THINKING_EFFORTS:
            raise ValueError('Invalid ACP thinking effort')
        if not os.path.isabs(cwd):
            raise ValueError('ACP session cwd must be absolute')
        if conversation_id is not None and re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', conversation_id) is None:
            raise ValueError('Invalid ChatGPT conversation ID')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.is_symlink():
            raise ValueError('Refusing to write ACP sessions into a symlink directory')
        if path.is_symlink():
            raise ValueError('Refusing to overwrite an ACP session symlink')
        temp_path: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', prefix=f'.{session_id}-', suffix='.tmp',
                dir=self.directory, delete=False,
            ) as stream:
                temp_path = stream.name
                os.chmod(temp_path, 0o600)
                json.dump(record, stream, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, path)
        finally:
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except FileNotFoundError:
                    pass
