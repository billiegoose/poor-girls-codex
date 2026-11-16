"""Human-readable ACP tool activity, independently of the PGC result transport."""
from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any


MAX_PREVIEW = 24_000


def _short(text: str, limit: int = 130) -> str:
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def display_name(call: dict[str, Any]) -> str:
    """Use actual executable names, not the generic `run` tool label."""
    tool = call.get('tool', 'unknown')
    if tool != 'run':
        return str(tool)
    if 'script' in call:
        return 'bash'
    command = call.get('command')
    if isinstance(command, list) and command:
        return Path(str(command[0])).name
    if isinstance(command, str):
        try:
            args = shlex.split(command)
            if args:
                return Path(args[0]).name
        except ValueError:
            pass
    return 'command'


def display_title(call: dict[str, Any], index: int) -> str:
    name = display_name(call)
    if call.get('tool') == 'run':
        if 'script' in call:
            script = str(call['script'])
            first = next((line.strip() for line in script.splitlines() if line.strip()), '')
            return f'bash: {_short(first)}' if first else 'bash'
        command = call.get('command', '')
        if isinstance(command, list):
            command = shlex.join(map(str, command))
        return _short(str(command)) or name
    details = call.get('path') if name in {'read', 'edit', 'write'} else (
        call.get('pattern') if name == 'find' else call.get('id', index)
    )
    return f'{name}: {_short(str(details))}'


def display_input(call: dict[str, Any]) -> dict[str, Any]:
    # Avoid accidentally displaying environment secrets or entire source-file
    # replacements in the ACP activity pane. The harness still receives them.
    keys = ('command', 'script', 'cwd', 'timeout_seconds') if call.get('tool') == 'run' else (
        'path', 'pattern', 'paths', 'start', 'end', 'depth', 'staged', 'cwd'
    )
    return {key: call[key] for key in keys if key in call}


def _preview(text: str) -> str:
    return text if len(text) <= MAX_PREVIEW else text[:MAX_PREVIEW] + '\n… (ACP preview truncated)'


def display_output(name: str, result: dict[str, Any]) -> str:
    if not result.get('ok', False):
        return _preview('Error: ' + str(result.get('error', 'Tool was not executed')))
    if name == 'run':
        code = result.get('exit_code', '?')
        stdout = str(result.get('stdout') or '')
        stderr = str(result.get('stderr') or '')
        parts = [f'Exit code: {code}']
        if stdout:
            parts.append('stdout:\n' + stdout)
        if stderr:
            parts.append('stderr:\n' + stderr)
        if not stdout and not stderr:
            parts.append('(No stdout or stderr)')
        if result.get('stdout_truncated') or result.get('stderr_truncated'):
            parts.append('(Harness output truncated)')
        return _preview('\n\n'.join(parts))
    for key in ('content', 'matches', 'tree', 'status', 'diff'):
        value = result.get(key)
        if isinstance(value, str):
            return _preview(value or '(Empty ' + key + ')')
    return _preview(', '.join(f'{key}={value}' for key, value in result.items()
                              if key not in {'id', 'tool', 'ok', 'sha256'}) or 'Completed')


def acp_content(text: str) -> list[dict[str, Any]]:
    return [{'type': 'content', 'content': {'type': 'text', 'text': text}}]
