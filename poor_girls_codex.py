#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from typing import Any

import ApplicationServices as AS
import Quartz

import probe_chatgpt as probe
import toolcall_lib


CHATGPT_BUNDLE_ID = "com.openai.codex"
COMPOSER_DESCRIPTION = "Message ChatGPT"
SEND_DESCRIPTION = "Send"
POLL_SECONDS = 0.5
SETTLE_SECONDS = 0.35
SUPPORTED_TOOLS = {"read", "find", "tree", "status", "diff", "edit", "write", "patch", "run"}


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


BOOTSTRAP_PROMPT = r'''You are operating as a coding agent through Poor Girl's Codex, a local tool harness.

When you need to use a local tool, make the tool request the final content of your response as one fenced ```json code block. Poor Girl's Codex will execute it and send the JSON result back automatically.

Available tools:
- read {path,start?,end?,numbered?,max_bytes?}
- find {pattern,paths?,fixed_strings?,ignore_case?,globs?,cwd?,timeout?,max_bytes?}
- tree {path?,depth?,cwd?,max_bytes?}
- status {cwd?}
- diff {paths?,staged?,cwd?,max_bytes?}
- edit {path,old,new,expected_sha256?,replace_all?}
- write {path,content,overwrite?,expected_sha256?,mkdirs?}
- patch {patch,cwd?}
- run {command|script,cwd?,timeout?,env?,max_bytes?}

A single call may be a JSON object. Multiple calls should use {"calls":[...]} and may include "stop_on_error": true. Every call should have a short descriptive "id".

Do not ask me to manually run commands, inspect files, or paste tool results when the harness can do it. Continue using tool calls until the task is complete, then answer normally.'''


def children_of(element):
    children = probe.ax_attr(element, "AXChildren")
    if not children:
        return []
    try:
        return list(children)
    except Exception:
        return []


def walk(root):
    stack = [root]
    while stack:
        element = stack.pop()
        yield element
        children = children_of(element)
        stack.extend(reversed(children))


def find_elements(root, *, role: str | None = None, description: str | None = None):
    matches = []
    for element in walk(root):
        if role is not None and str(probe.ax_attr(element, "AXRole") or "") != role:
            continue
        if description is not None and str(probe.ax_attr(element, "AXDescription") or "") != description:
            continue
        matches.append(element)
    return matches


def chatgpt_root():
    app, _ = probe.find_chatgpt_app()
    if app is None:
        raise RuntimeError("ChatGPT is not running")
    pid = int(app.processIdentifier())
    return app, AS.AXUIElementCreateApplication(pid)


def press(element) -> None:
    error = AS.AXUIElementPerformAction(element, AS.kAXPressAction)
    if error != 0:
        raise RuntimeError(f"AXPress failed with error {error}")


def clipboard_write(text: str) -> None:
    # Clipboard use is intentionally limited to the startup convenience prompt.
    # Toolcall detection and extraction use Accessibility directly.
    subprocess.run(["pbcopy"], input=text, text=True, check=True)


def static_text(element) -> str:
    pieces = []
    for node in walk(element):
        if str(probe.ax_attr(node, "AXRole") or "") != "AXStaticText":
            continue
        value = probe.ax_attr(node, "AXValue")
        if value is not None:
            pieces.append(str(value))
    return "".join(pieces)


def conversation_group(root):
    best = None
    best_score = 0
    for node in walk(root):
        children = children_of(node)
        if not children:
            continue
        headings = [
            str(probe.ax_attr(child, "AXTitle") or probe.ax_attr(child, "AXValue") or "")
            for child in children
            if str(probe.ax_attr(child, "AXRole") or "") == "AXHeading"
        ]
        chatgpt = headings.count("ChatGPT said:")
        user = headings.count("You said:")
        score = chatgpt + user
        if chatgpt and user and score > best_score:
            best = node
            best_score = score
    if best is None:
        raise RuntimeError("ChatGPT conversation group not found")
    return best


def latest_assistant_content(root):
    children = children_of(conversation_group(root))
    latest = None
    for index, child in enumerate(children[:-1]):
        if str(probe.ax_attr(child, "AXRole") or "") != "AXHeading":
            continue
        heading = str(probe.ax_attr(child, "AXTitle") or probe.ax_attr(child, "AXValue") or "")
        if heading == "ChatGPT said:":
            latest = children[index + 1]
    if latest is None:
        raise RuntimeError("latest ChatGPT response not found")
    return latest


def latest_assistant_toolcall(root):
    content = latest_assistant_content(root)
    candidates = []

    # Chromium exposes syntax-highlighted code as many AXStaticText tokens.
    # Find the smallest descendant group whose concatenated text is a valid
    # Poor Girl's Codex request. This avoids pressing Copy or touching the
    # clipboard and naturally ignores surrounding assistant prose/controls.
    for node in walk(content):
        if str(probe.ax_attr(node, "AXRole") or "") != "AXGroup":
            continue
        source = static_text(node).strip()
        if not source:
            continue
        try:
            request = json.loads(source)
            validate_request(request)
        except (ValueError, json.JSONDecodeError):
            continue
        candidates.append((len(source), source, request))

    if not candidates:
        raise RuntimeError("latest ChatGPT response contains no valid toolcall JSON")

    _, source, request = min(candidates, key=lambda item: item[0])
    return source, request


def unwrap_json(text: str) -> Any:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            lines = lines[1:-1]
            stripped = "\n".join(lines)
    return json.loads(stripped)


def request_calls(request: Any):
    stop_on_error = False

    if isinstance(request, dict) and "calls" in request:
        calls = request["calls"]
        stop_on_error = bool(request.get("stop_on_error", False))
        single = False
    elif isinstance(request, list):
        calls = request
        single = False
    elif isinstance(request, dict):
        calls = [request]
        single = True
    else:
        raise ValueError("toolcall JSON must be an object, array, or {calls:[...]}")

    if not isinstance(calls, list):
        raise ValueError('"calls" must be an array')
    return calls, stop_on_error, single


def validate_request(request: Any) -> None:
    calls, _, _ = request_calls(request)
    if not calls:
        raise ValueError("toolcall request contains no calls")
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise ValueError(f"call {index} must be an object")
        tool = call.get("tool")
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"call {index} has unsupported tool {tool!r}")


def execute_request(request: Any, *, announce: bool = False) -> Any:
    calls, stop_on_error, single = request_calls(request)

    results = []
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            result = {
                "id": str(index),
                "tool": None,
                "ok": False,
                "error": "call must be an object",
            }
        else:
            if announce:
                tool_name = f"{str(call.get('tool', '?')):<8}"
                call_id = str(call.get('id', index))
                print(f"  {color('>', '1;35')} {color(tool_name, '1;36')} {color(call_id, '1')}", flush=True)
            result = toolcall_lib.execute(call, index)
        results.append(result)
        if stop_on_error and not result["ok"]:
            break

    return results[0] if single else results


def fenced_result(result: Any) -> str:
    return "```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```\n"


def set_composer_text(root, text: str):
    composers = find_elements(
        root,
        role="AXTextArea",
        description=COMPOSER_DESCRIPTION,
    )
    if not composers:
        raise RuntimeError("ChatGPT composer not found")

    composer = composers[-1]
    error = AS.AXUIElementSetAttributeValue(composer, "AXValue", text)
    if error != 0:
        raise RuntimeError(f"setting composer AXValue failed with error {error}")
    return composer


def submit_composer_to_pid(app, composer, expected_text: str) -> None:
    # Keep input scoped to ChatGPT rather than the global HID stream. Setting
    # AXFocused on an inactive app does not activate it, and CGEventPostToPid
    # sends Return only to ChatGPT instead of hijacking the user's keyboard.
    focus_error = AS.AXUIElementSetAttributeValue(composer, "AXFocused", True)
    if focus_error != 0:
        raise RuntimeError(f"focusing composer via AX failed with error {focus_error}")

    pid = int(app.processIdentifier())
    down = Quartz.CGEventCreateKeyboardEvent(None, 36, True)
    up = Quartz.CGEventCreateKeyboardEvent(None, 36, False)
    Quartz.CGEventPostToPid(pid, down)
    Quartz.CGEventPostToPid(pid, up)

    # Submission is asynchronous. Confirm that the payload left the composer
    # rather than assuming the Return event was accepted.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        _, root = chatgpt_root()
        composers = find_elements(
            root,
            role="AXTextArea",
            description=COMPOSER_DESCRIPTION,
        )
        if composers:
            value = str(probe.ax_attr(composers[-1], "AXValue") or "")
            if expected_text.strip() not in value:
                return
        time.sleep(0.05)

    raise RuntimeError("ChatGPT did not submit the composer after PID-targeted Return")


def paste_result_into_composer(app, root, text: str, *, send: bool) -> None:
    composer = set_composer_text(root, text)
    time.sleep(0.1)

    if not send:
        return

    # Ensure React noticed the AXValue mutation before attempting submission.
    app, root = chatgpt_root()
    send_buttons = find_elements(root, role="AXButton", description=SEND_DESCRIPTION)
    enabled = [b for b in send_buttons if probe.ax_attr(b, "AXEnabled") is not False]
    if not enabled:
        raise RuntimeError("enabled Send button not found after setting composer AXValue")

    composers = find_elements(root, role="AXTextArea", description=COMPOSER_DESCRIPTION)
    if not composers:
        raise RuntimeError("ChatGPT composer disappeared before submission")
    submit_composer_to_pid(app, composers[-1], text)


def dismiss_work_prompt(root) -> bool:
    for node in walk(root):
        if str(probe.ax_attr(node, "AXRole") or "") != "AXButton":
            continue
        text = " ".join(
            str(probe.ax_attr(node, attr) or "")
            for attr in ("AXDescription", "AXTitle", "AXValue")
        ).strip().lower()
        if "stay in chat" in text:
            press(node)
            return True
    return False


def latest_valid_request(root):
    try:
        source, request = latest_assistant_toolcall(root)
    except (RuntimeError, ValueError, json.JSONDecodeError):
        return None
    fingerprint = hashlib.sha256(source.strip().encode("utf-8")).hexdigest()
    return source, request, fingerprint


def watch_loop() -> None:
    border = "+================================================================+"
    print(color(border, "1;36"))
    print(color("|                     POOR GIRL'S CODEX                          |", "1;35"))
    print(color("|                the hacky ChatGPT coding harness                |", "36"))
    print(color(border, "1;36"))
    print()
    print(f"  {color('[1]', '1;35')} Open a regular chat in the ChatGPT desktop app.")
    print(f"  {color('[2]', '1;35')} Paste the bootstrap prompt already on your clipboard.")
    print(f"  {color('[3]', '1;35')} Leave me running; I'll handle tool calls in the background.")
    print()
    print(f"  {color('[ready]', '1;32')} Waiting for ChatGPT tool calls...", flush=True)
    print()

    clipboard_write(BOOTSTRAP_PROMPT)

    # Ignore any valid harness call already visible when the watcher starts.
    _, root = chatgpt_root()
    existing = latest_valid_request(root)
    last_fingerprint = existing[2] if existing else None

    while True:
        try:
            app, root = chatgpt_root()
            if dismiss_work_prompt(root):
                time.sleep(0.2)
                app, root = chatgpt_root()

            candidate = latest_valid_request(root)
            if candidate is None:
                time.sleep(POLL_SECONDS)
                continue

            source, request, fingerprint = candidate
            if fingerprint == last_fingerprint:
                time.sleep(POLL_SECONDS)
                continue

            # A streaming code block can briefly become parseable before the
            # assistant is done. Require the same call after a settling delay.
            time.sleep(SETTLE_SECONDS)
            _, settled_root = chatgpt_root()
            settled = latest_valid_request(settled_root)
            if settled is None or settled[2] != fingerprint:
                continue

            print(color("tool calls", "1;35") + ":", flush=True)
            result = execute_request(request, announce=True)
            rendered = fenced_result(result)
            app, root = chatgpt_root()
            paste_result_into_composer(app, root, rendered, send=True)
            last_fingerprint = fingerprint
            print(f"  {color('OK', '1;32')} sent results\n", flush=True)
        except KeyboardInterrupt:
            print("\nPoor Girl's Codex stopped.")
            return
        except Exception as exc:
            print(f"  watcher error: {exc}", flush=True)
            time.sleep(1.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Clipboard/Accessibility bridge for Poor Girl's Codex")
    parser.add_argument(
        "mode",
        nargs="?",
        choices=("watch", "copy", "execute", "paste", "once"),
        default="watch",
        help="watch=continuous background harness; copy/execute/paste/once are debugging modes",
    )
    parser.add_argument(
        "--source-file",
        help="read toolcall JSON from a file instead of copying the latest ChatGPT code block",
    )
    args = parser.parse_args()

    if not probe.trusted():
        raise SystemExit("Accessibility permission is required")

    if args.mode == "watch":
        watch_loop()
        return

    app, root = chatgpt_root()
    if args.source_file:
        with open(args.source_file, encoding="utf-8") as source_file:
            source = source_file.read()
        request = unwrap_json(source)
    else:
        source, request = latest_assistant_toolcall(root)

    if args.mode == "copy":
        print(source, end="")
        return
    result = execute_request(request)
    rendered = fenced_result(result)

    if args.mode == "execute":
        print(rendered, end="")
        return

    paste_result_into_composer(app, root, rendered, send=args.mode == "once")
    print(rendered, end="")


if __name__ == "__main__":
    main()
