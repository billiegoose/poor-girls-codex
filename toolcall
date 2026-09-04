#!/usr/bin/env python3

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


DEFAULT_MAX_BYTES = 200_000
DEFAULT_TIMEOUT = 120


class ToolError(Exception):
    pass


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except Exception as exc:
        raise ToolError(f"{path}: {exc}") from exc


def read_text(path: Path) -> str:
    data = read_bytes(path)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"{path}: not valid UTF-8") from exc


def bounded(text: str, max_bytes: int) -> tuple[str, bool]:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False

    clipped = data[:max_bytes]
    while True:
        try:
            return clipped.decode("utf-8"), True
        except UnicodeDecodeError:
            clipped = clipped[:-1]


def run_process(
    argv: list[str],
    *,
    stdin: str | None = None,
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    process_env = os.environ.copy()
    process_env.update(
        {
            "NO_COLOR": "1",
            "CLICOLOR": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "TERM": "dumb",
        }
    )
    if env:
        process_env.update({str(k): str(v) for k, v in env.items()})

    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=process_env,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise ToolError(f"command timed out after {timeout}s") from exc
    except FileNotFoundError as exc:
        raise ToolError(f"command not found: {argv[0]}") from exc
    except Exception as exc:
        raise ToolError(str(exc)) from exc

    return {
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def require(call: dict[str, Any], key: str) -> Any:
    if key not in call:
        raise ToolError(f"missing required field: {key}")
    return call[key]


def tool_read(call: dict[str, Any]) -> dict[str, Any]:
    path = Path(require(call, "path"))
    text = read_text(path)

    start = int(call.get("start", 1))
    end = call.get("end")
    numbered = bool(call.get("numbered", True))
    max_bytes = int(call.get("max_bytes", DEFAULT_MAX_BYTES))

    if start < 1:
        raise ToolError("start must be >= 1")

    lines = text.splitlines(keepends=True)
    if end is None:
        end = len(lines)
    end = int(end)

    if end < start:
        raise ToolError("end must be >= start")

    selected = lines[start - 1 : end]

    if numbered:
        width = max(1, len(str(end)))
        content = "".join(
            f"{line_no:>{width}}  {line}"
            for line_no, line in enumerate(selected, start=start)
        )
    else:
        content = "".join(selected)

    content, truncated = bounded(content, max_bytes)

    raw = path.read_bytes()

    return {
        "path": str(path),
        "start": start,
        "end": min(end, len(lines)),
        "line_count": len(lines),
        "sha256": sha256_bytes(raw),
        "content": content,
        "truncated": truncated,
    }


def tool_find(call: dict[str, Any]) -> dict[str, Any]:
    pattern = str(require(call, "pattern"))
    paths = call.get("paths", ["."])
    if isinstance(paths, str):
        paths = [paths]

    max_bytes = int(call.get("max_bytes", DEFAULT_MAX_BYTES))

    argv = [
        "rg",
        "-n",
        "--no-heading",
        "--color",
        "never",
        "--hidden",
        "--glob",
        "!.git",
    ]

    if call.get("fixed_strings"):
        argv.append("-F")
    if call.get("ignore_case"):
        argv.append("-i")

    globs = call.get("globs", [])
    if isinstance(globs, str):
        globs = [globs]
    for glob in globs:
        argv.extend(["--glob", str(glob)])

    argv.append(pattern)
    argv.extend(str(p) for p in paths)

    result = run_process(
        argv,
        cwd=call.get("cwd"),
        timeout=int(call.get("timeout", DEFAULT_TIMEOUT)),
    )

    # ripgrep: 0 = matches, 1 = no matches, 2+ = error
    if result["exit_code"] not in (0, 1):
        raise ToolError(
            result["stderr"].strip()
            or f"rg failed with exit code {result['exit_code']}"
        )

    output, truncated = bounded(result["stdout"], max_bytes)

    return {
        "matches": output,
        "found": result["exit_code"] == 0,
        "truncated": truncated,
    }


def tool_tree(call: dict[str, Any]) -> dict[str, Any]:
    path = str(call.get("path", "."))
    depth = int(call.get("depth", 3))
    max_bytes = int(call.get("max_bytes", DEFAULT_MAX_BYTES))

    if depth < 0:
        raise ToolError("depth must be >= 0")

    # Prefer tree when available because its output is dramatically nicer.
    probe = run_process(["sh", "-c", "command -v tree >/dev/null 2>&1"])
    if probe["exit_code"] == 0:
        argv = [
            "tree",
            "-a",
            "--noreport",
            "-L",
            str(depth),
            "-I",
            ".git",
            path,
        ]
    else:
        argv = [
            "find",
            path,
            "-maxdepth",
            str(depth),
            "-not",
            "-path",
            "*/.git/*",
            "-print",
        ]

    result = run_process(
        argv,
        cwd=call.get("cwd"),
        timeout=int(call.get("timeout", DEFAULT_TIMEOUT)),
    )
    if result["exit_code"] != 0:
        raise ToolError(result["stderr"].strip() or "tree failed")

    output, truncated = bounded(result["stdout"], max_bytes)
    return {
        "tree": output,
        "truncated": truncated,
    }


def tool_status(call: dict[str, Any]) -> dict[str, Any]:
    result = run_process(
        [
            "git",
            "-c",
            "color.ui=false",
            "-c",
            "color.status=false",
            "status",
            "--short",
            "--branch",
        ],
        cwd=call.get("cwd"),
    )
    if result["exit_code"] != 0:
        raise ToolError(result["stderr"].strip() or "git status failed")

    return {"status": result["stdout"]}


def tool_diff(call: dict[str, Any]) -> dict[str, Any]:
    paths = call.get("paths", [])
    if isinstance(paths, str):
        paths = [paths]

    staged = bool(call.get("staged", False))
    max_bytes = int(call.get("max_bytes", DEFAULT_MAX_BYTES))

    argv = [
        "git",
        "-c",
        "color.ui=false",
        "diff",
        "--no-ext-diff",
    ]

    if staged:
        argv.append("--cached")

    argv.append("--")
    argv.extend(str(p) for p in paths)

    result = run_process(argv, cwd=call.get("cwd"))
    if result["exit_code"] != 0:
        raise ToolError(result["stderr"].strip() or "git diff failed")

    output, truncated = bounded(result["stdout"], max_bytes)

    return {
        "diff": output,
        "truncated": truncated,
    }


def check_expected_sha(path: Path, expected: str | None) -> None:
    if expected is None:
        return

    actual = sha256_bytes(read_bytes(path))
    if actual != expected:
        raise ToolError(
            f"{path}: SHA mismatch; expected {expected}, actual {actual}"
        )


def tool_edit(call: dict[str, Any]) -> dict[str, Any]:
    path = Path(require(call, "path"))
    old = str(require(call, "old"))
    new = str(require(call, "new"))
    replace_all = bool(call.get("replace_all", False))

    check_expected_sha(path, call.get("expected_sha256"))

    text = read_text(path)
    count = text.count(old)

    if count == 0:
        raise ToolError("old text not found")

    if count > 1 and not replace_all:
        raise ToolError(
            f"old text occurs {count} times; "
            "refusing ambiguous edit without replace_all=true"
        )

    if replace_all:
        updated = text.replace(old, new)
        replacements = count
    else:
        updated = text.replace(old, new, 1)
        replacements = 1

    path.write_text(updated)

    return {
        "path": str(path),
        "replacements": replacements,
        "sha256": sha256_bytes(path.read_bytes()),
    }


def tool_write(call: dict[str, Any]) -> dict[str, Any]:
    path = Path(require(call, "path"))
    content = str(require(call, "content"))

    exists = path.exists()

    if exists:
        if not call.get("overwrite", False):
            raise ToolError(
                f"{path}: already exists; set overwrite=true to replace it"
            )
        check_expected_sha(path, call.get("expected_sha256"))
    elif call.get("expected_sha256") is not None:
        raise ToolError(f"{path}: does not exist, cannot check expected SHA")

    if call.get("mkdirs", False):
        path.parent.mkdir(parents=True, exist_ok=True)

    path.write_text(content)

    return {
        "path": str(path),
        "created": not exists,
        "bytes": len(content.encode("utf-8")),
        "sha256": sha256_bytes(path.read_bytes()),
    }


def tool_patch(call: dict[str, Any]) -> dict[str, Any]:
    patch = str(require(call, "patch"))
    cwd = call.get("cwd")

    check = run_process(
        ["git", "apply", "--check", "-"],
        stdin=patch,
        cwd=cwd,
    )
    if check["exit_code"] != 0:
        raise ToolError(
            check["stderr"].strip()
            or check["stdout"].strip()
            or "git apply --check failed"
        )

    apply = run_process(
        ["git", "apply", "-"],
        stdin=patch,
        cwd=cwd,
    )
    if apply["exit_code"] != 0:
        raise ToolError(
            apply["stderr"].strip()
            or apply["stdout"].strip()
            or "git apply failed"
        )

    return {"applied": True}


def tool_run(call: dict[str, Any]) -> dict[str, Any]:
    command = call.get("command")
    script = call.get("script")

    if (command is None) == (script is None):
        raise ToolError("provide exactly one of command or script")

    timeout = int(call.get("timeout", DEFAULT_TIMEOUT))
    cwd = call.get("cwd")
    env = call.get("env")

    if command is not None:
        if isinstance(command, str):
            argv = shlex.split(command)
        elif isinstance(command, list):
            argv = [str(x) for x in command]
        else:
            raise ToolError("command must be a string or array")

        result = run_process(
            argv,
            cwd=cwd,
            timeout=timeout,
            env=env,
        )
    else:
        result = run_process(
            ["bash", "-o", "pipefail", "-c", str(script)],
            cwd=cwd,
            timeout=timeout,
            env=env,
        )

    max_bytes = int(call.get("max_bytes", DEFAULT_MAX_BYTES))

    stdout, stdout_truncated = bounded(result["stdout"], max_bytes)
    stderr, stderr_truncated = bounded(result["stderr"], max_bytes)

    return {
        "exit_code": result["exit_code"],
        "stdout": stdout,
        "stderr": stderr,
        "stdout_truncated": stdout_truncated,
        "stderr_truncated": stderr_truncated,
    }


TOOLS = {
    "read": tool_read,
    "find": tool_find,
    "tree": tool_tree,
    "status": tool_status,
    "diff": tool_diff,
    "edit": tool_edit,
    "write": tool_write,
    "patch": tool_patch,
    "run": tool_run,
}


def execute(call: dict[str, Any], index: int) -> dict[str, Any]:
    call_id = call.get("id", str(index))
    tool_name = call.get("tool")

    base = {
        "id": call_id,
        "tool": tool_name,
    }

    if not isinstance(tool_name, str):
        return {
            **base,
            "ok": False,
            "error": "missing or invalid tool",
        }

    handler = TOOLS.get(tool_name)
    if handler is None:
        return {
            **base,
            "ok": False,
            "error": f"unknown tool: {tool_name}",
        }

    try:
        result = handler(call)
        return {
            **base,
            "ok": True,
            **result,
        }
    except ToolError as exc:
        return {
            **base,
            "ok": False,
            "error": str(exc),
        }
    except Exception as exc:
        return {
            **base,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def main() -> None:
    if sys.stdin.isatty():
        print('''Poor Girl's Codex Harness

You have indirect access to the user's local development environment through a clipboard-based tool-call harness. The user acts only as the transport layer.

To call tools, emit raw JSON (preferably in a fenced code block for easy copying). The user will copy the JSON and run:

    pbpaste | toolcall | pbcopy

They will then paste the raw JSON result back to you. Treat that result exactly like the result of native coding-agent tool calls. Do not ask the user to interpret commands or results when the harness can do the work.

A single call is a JSON object:

    {"id":"read-1","tool":"read","path":"src/example.py","start":1,"end":100}

Prefer batches when calls can efficiently be issued together:

    {"calls":[
      {"id":"status","tool":"status"},
      {"id":"search","tool":"find","pattern":"foo","paths":["src","tests"]}
    ]}

Calls execute sequentially. Add "stop_on_error":true at the batch level when later calls depend on earlier calls succeeding.

Available tools:

  read   {path, start?, end?, numbered?, max_bytes?}
         Read a UTF-8 file. Returns content, line information, and SHA-256.

  find   {pattern, paths?, fixed_strings?, ignore_case?, globs?, cwd?, timeout?, max_bytes?}
         Search with ripgrep. No matches is a successful result with found=false.

  tree   {path?, depth?, cwd?, max_bytes?}
         Inspect directory structure.

  status {cwd?}
         Git short/branch status.

  diff   {paths?, staged?, cwd?, max_bytes?}
         Git diff.

  edit   {path, old, new, expected_sha256?, replace_all?}
         Exact textual replacement. By default refuses zero or multiple matches. Prefer this for normal edits. Use expected_sha256 when editing content previously returned by read if stale-file protection matters.

  write  {path, content, overwrite?, expected_sha256?, mkdirs?}
         Create or replace a UTF-8 file. Refuses overwriting by default.

  patch  {patch, cwd?}
         Apply a unified diff using git apply --check followed by git apply.

  run    {command | script, cwd?, timeout?, env?, max_bytes?}
         Execute a command. "command" accepts a string or argv array without a shell; "script" explicitly executes Bash. A subprocess nonzero exit is represented by ok=true with its exit_code; ok=false means the tool invocation itself failed.

Every call should have a unique descriptive id. Batch results preserve those ids.

TRANSPORT CONVENTION:
- When issuing tool calls, emit JSON in a fenced ```json code block. The user copies the contents of that block and runs `pbpaste | toolcall | pbcopy`.
- The harness output is JSON wrapped in a fenced ```json code block. The user pastes that entire fenced result back into chat. The fence is transport/presentation framing and is NOT part of the JSON protocol.
- Do not add prose, labels such as "toolcall output:", or instructions around tool-call blocks unless necessary. Keeping calls directly copyable minimizes the user's work.
- Never include Markdown fences in the JSON sent to toolcall itself; only the JSON contents are stdin.

Working style: operate as a coding agent. Inspect before editing, prefer constrained tools over run, make small verifiable edits, run relevant tests after changes, inspect diffs, and use the user only to shuttle JSON between you and the harness.''')
        return

    try:
        request = json.load(sys.stdin)
    except Exception as exc:
        json.dump(
            {
                "ok": False,
                "error": f"invalid JSON input: {exc}",
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)

    stop_on_error = False

    if isinstance(request, dict) and "calls" in request:
        calls = request["calls"]
        stop_on_error = bool(request.get("stop_on_error", False))
    elif isinstance(request, list):
        calls = request
    elif isinstance(request, dict):
        calls = [request]
    else:
        json.dump(
            {
                "ok": False,
                "error": "input must be a call object, array of calls, or {calls:[...]}",
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        sys.exit(1)

    if not isinstance(calls, list):
        raise SystemExit('"calls" must be an array')

    results: list[dict[str, Any]] = []

    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            result = {
                "id": str(index),
                "tool": None,
                "ok": False,
                "error": "call must be an object",
            }
        else:
            result = execute(call, index)

        results.append(result)

        if stop_on_error and not result["ok"]:
            break

    # A single input call returns a single result.
    # A batch returns an array.
    output: Any
    if isinstance(request, dict) and "calls" not in request:
        output = results[0]
    else:
        output = results

    sys.stdout.write("```json\n")
    json.dump(output, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n```\n")


if __name__ == "__main__":
    main()
