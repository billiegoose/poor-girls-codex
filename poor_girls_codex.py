#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import select
import subprocess
import sys
import termios
import time
import tty
from dataclasses import dataclass, field
from enum import Enum, auto
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
SEND_RETRY_INITIAL_SECONDS = 0.25
SEND_RETRY_MAX_SECONDS = 8.0
RETURN_RETRY_INITIAL_SECONDS = 0.25
RETURN_RETRY_MAX_SECONDS = 8.0
MESSAGE_TOO_LONG_TEXT = "The message you submitted was too long, please edit it and resubmit."
SUPPORTED_TOOLS = {"read", "find", "tree", "status", "diff", "edit", "write", "patch", "run"}


class WatcherPhase(Enum):
    SCANNING = auto()
    EXECUTING = auto()
    DELIVERING = auto()


class DeliveryOutcome(Enum):
    SENT = auto()
    INTERRUPTED = auto()


@dataclass
class CompletedBatch:
    fingerprint: str
    calls: list[Any]
    results: list[Any]
    single: bool
    compact: bool = False

    def render(self) -> str:
        if self.compact:
            return too_long_fallback(self.calls, self.results)
        payload = self.results[0] if self.single else self.results
        return fenced_result(payload)


@dataclass
class WatcherState:
    phase: WatcherPhase = WatcherPhase.SCANNING
    seen_fingerprints: set[str] = field(default_factory=set)
    pending_batches: list[CompletedBatch] = field(default_factory=list)
    last_submitted_batches: list[CompletedBatch] = field(default_factory=list)

    def record_completed(self, fingerprint: str, request: Any, result: Any) -> None:
        calls, _, single, _ = request_calls(request)
        results = [result] if single else list(result)
        self.pending_batches.append(
            CompletedBatch(
                fingerprint=fingerprint,
                calls=list(calls),
                results=results,
                single=single,
            )
        )

    def has_pending_results(self) -> bool:
        return bool(self.pending_batches)

    def render_pending(self) -> str:
        return "\n".join(batch.render().rstrip("\n") for batch in self.pending_batches) + "\n"

    def mark_submitted(self) -> None:
        self.last_submitted_batches = self.pending_batches
        self.pending_batches = []

    def acknowledge_last_submission(self) -> None:
        self.last_submitted_batches = []

    def restore_last_submission_compact(self) -> None:
        if not self.last_submitted_batches:
            return
        restored = self.last_submitted_batches
        self.last_submitted_batches = []
        for batch in restored:
            batch.compact = True
        self.pending_batches = restored + self.pending_batches

    def last_submission_key(self) -> str | None:
        if not self.last_submitted_batches:
            return None
        return ":".join(batch.fingerprint for batch in self.last_submitted_batches)


def color(text: str, code: str) -> str:
    if not sys.stdout.isatty():
        return text
    return f"\033[{code}m{text}\033[0m"


class TerminalHotkeys:
    """Read single-key watcher commands without blocking the AX polling loop."""

    def __init__(self) -> None:
        self.fd: int | None = None
        self.saved: list[Any] | None = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self
        try:
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except (OSError, termios.error):
            self.fd = None
            self.saved = None
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if self.fd is not None and self.saved is not None:
            try:
                termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
            except (OSError, termios.error):
                pass
        return False

    def read(self) -> str | None:
        if self.fd is None:
            return None
        try:
            readable, _, _ = select.select([self.fd], [], [], 0)
            if not readable:
                return None
            return os.read(self.fd, 1).decode("utf-8", errors="ignore")
        except OSError:
            return None


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

A single call may be a JSON object. Multiple calls should use {"calls":[...]}. Every call should have a short descriptive "id". To stop later calls when one fails, put "stop_on_error": true on that individual call. Top-level "stop_on_error" is deprecated.

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


def save_accessibility_dump(exc: BaseException | None = None) -> str:
    """Save the current ChatGPT accessibility tree manually or after an error."""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = time.time_ns() % 1_000_000_000
    filename = f"poor-girls-codex-ax-dump-{stamp}-{suffix:09d}.txt"
    lines = [
        "Poor Girl's Codex watcher accessibility dump",
        f"error: {type(exc).__name__}: {exc}" if exc is not None else "reason: manual Ctrl-X dump",
        "",
        "=== Full accessibility tree ===",
    ]
    try:
        _, root = chatgpt_root()
        lines.extend(probe.dump_tree(root))
    except Exception as dump_exc:
        lines.append(f"<unable to dump ChatGPT accessibility tree: {dump_exc}>")

    with open(filename, "w", encoding="utf-8") as output_file:
        output_file.write("\n".join(lines) + "\n")
    return filename


def ui_contains_text(root, needle: str) -> bool:
    wanted = needle.casefold()
    for node in walk(root):
        for attr in ("AXValue", "AXTitle", "AXDescription", "AXHelp"):
            value = probe.ax_attr(node, attr)
            if value is not None and wanted in str(value).casefold():
                return True
    return False


def ui_contains_text_outside_conversation(root, needle: str) -> bool:
    """Find UI text while ignoring transcript content that may quote it verbatim."""
    wanted = needle.casefold()
    try:
        conversation = conversation_group(root)
    except RuntimeError:
        conversation = None

    stack = [root]
    while stack:
        node = stack.pop()
        if conversation is not None and node == conversation:
            continue
        for attr in ("AXValue", "AXTitle", "AXDescription", "AXHelp"):
            value = probe.ax_attr(node, attr)
            if value is not None and wanted in str(value).casefold():
                return True
        stack.extend(reversed(children_of(node)))
    return False


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
    top_level_stop_on_error = False
    top_level_stop_present = False

    if isinstance(request, dict) and "calls" in request:
        calls = request["calls"]
        top_level_stop_present = "stop_on_error" in request
        if top_level_stop_present and not isinstance(request["stop_on_error"], bool):
            raise ValueError("top-level stop_on_error must be a boolean")
        top_level_stop_on_error = request.get("stop_on_error", False)
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
    return calls, top_level_stop_on_error, single, top_level_stop_present


def validate_request(request: Any) -> None:
    calls, _, _, _ = request_calls(request)
    if not calls:
        raise ValueError("toolcall request contains no calls")
    for index, call in enumerate(calls):
        if not isinstance(call, dict):
            raise ValueError(f"call {index} must be an object")
        tool = call.get("tool")
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"call {index} has unsupported tool {tool!r}")
        if "stop_on_error" in call and not isinstance(call["stop_on_error"], bool):
            raise ValueError(f"call {index} stop_on_error must be a boolean")


def tool_call_line(call: Any, index: int, status: str | None = None) -> str:
    if isinstance(call, dict):
        tool_name = f"{str(call.get('tool', '?')):<8}"
        call_id = str(call.get('id', index))
    else:
        tool_name = f"{'?':<8}"
        call_id = str(index)

    line = f"  {color('>', '1;35')} {color(tool_name, '1;36')} {color(call_id, '1')}"
    if status is not None:
        status_code = "1;32" if status == "done" else "1;33"
        line += f" {color(f'[{status}]', status_code)}"
    return line


class ToolCallProgress:
    def __init__(self, calls: list[Any]) -> None:
        self.calls = calls
        self.statuses: list[str | None] = [None] * len(calls)
        self.rendered = False

    def render(self) -> None:
        lines = [tool_call_line(call, index, self.statuses[index]) for index, call in enumerate(self.calls)]
        if self.rendered and sys.stdout.isatty() and lines:
            sys.stdout.write(f"\033[{len(lines)}A")
            for line in lines:
                sys.stdout.write(f"\r\033[2K{line}\n")
            sys.stdout.flush()
            return

        for line in lines:
            print(line, flush=True)
        self.rendered = True

    def set_status(self, index: int, status: str) -> None:
        self.statuses[index] = status
        self.render()


def skipped_result(call: Any, index: int) -> dict[str, Any]:
    if isinstance(call, dict):
        call_id = str(call.get("id", index))
        tool = call.get("tool")
    else:
        call_id = str(index)
        tool = None
    return {
        "id": call_id,
        "tool": tool,
        "ok": False,
        "skipped": True,
        "error": "skipped because an earlier call failed with stop_on_error enabled",
    }


def execute_request(request: Any, *, announce: bool = False) -> Any:
    calls, top_level_stop_on_error, single, top_level_stop_present = request_calls(request)
    if top_level_stop_present:
        print(
            "  deprecation warning: top-level stop_on_error is deprecated; "
            "put stop_on_error on the individual call instead",
            flush=True,
        )

    progress = ToolCallProgress(calls) if announce else None
    if progress is not None:
        progress.render()

    results = []
    stopped = False
    for index, call in enumerate(calls):
        if stopped:
            result = skipped_result(call, index)
            results.append(result)
            if progress is not None:
                progress.set_status(index, "skipped")
            continue

        if progress is not None:
            progress.set_status(index, "running")

        if not isinstance(call, dict):
            result = {
                "id": str(index),
                "tool": None,
                "ok": False,
                "error": "call must be an object",
            }
            call_stop_on_error = False
        else:
            call_stop_on_error = bool(call.get("stop_on_error", False))
            tool_input = dict(call)
            tool_input.pop("stop_on_error", None)
            result = toolcall_lib.execute(tool_input, index)

        results.append(result)
        if progress is not None:
            progress.set_status(index, "done")
        if not result["ok"] and (call_stop_on_error or top_level_stop_on_error):
            stopped = True

    return results[0] if single else results


def fenced_result(result: Any) -> str:
    return "```json\n" + json.dumps(result, indent=2, ensure_ascii=False) + "\n```\n"


def too_long_fallback(calls: list[Any], results: list[Any]) -> str:
    summaries = []
    summary_limit = 12

    for index, call in enumerate(calls[:summary_limit]):
        if isinstance(call, dict):
            call_id = str(call.get("id", index))
            tool = str(call.get("tool", "?"))
        else:
            call_id = str(index)
            tool = "?"
        item = results[index] if index < len(results) else None
        status = "ok" if isinstance(item, dict) and item.get("ok") else "error"
        summaries.append(f"- {call_id}: {tool} ({status})")

    if len(calls) > summary_limit:
        summaries.append(f"- ... and {len(calls) - summary_limit} more tool calls")

    return (
        "Poor Girl's Codex delivery error: the full tool results were too large for ChatGPT.\n"
        "The tools already ran; do not repeat them merely because delivery failed.\n"
        "Tools run:\n"
        + "\n".join(summaries)
        + "\nRetry with smaller chunks, such as narrower reads/finds or lower max_bytes.\n"
    )


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
    pid = int(app.processIdentifier())
    attempt = 0

    while True:
        # Re-check before every retry. If the previous Return eventually took
        # effect after our confirmation timeout, do not send another Return.
        app, root = chatgpt_root()
        composers = find_elements(
            root,
            role="AXTextArea",
            description=COMPOSER_DESCRIPTION,
        )
        if not composers:
            return

        composer = composers[-1]
        value = str(probe.ax_attr(composer, "AXValue") or "")
        if expected_text.strip() not in value:
            return

        focus_error = AS.AXUIElementSetAttributeValue(composer, "AXFocused", True)
        if focus_error != 0:
            raise RuntimeError(f"focusing composer via AX failed with error {focus_error}")

        down = Quartz.CGEventCreateKeyboardEvent(None, 36, True)
        up = Quartz.CGEventCreateKeyboardEvent(None, 36, False)
        Quartz.CGEventPostToPid(pid, down)
        Quartz.CGEventPostToPid(pid, up)

        # Submission is asynchronous. Confirm that the payload left the
        # composer rather than assuming the Return event was accepted.
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            _, root = chatgpt_root()
            composers = find_elements(
                root,
                role="AXTextArea",
                description=COMPOSER_DESCRIPTION,
            )
            if not composers:
                return
            value = str(probe.ax_attr(composers[-1], "AXValue") or "")
            if expected_text.strip() not in value:
                return
            time.sleep(0.05)

        delay = min(
            RETURN_RETRY_INITIAL_SECONDS * (2**attempt),
            RETURN_RETRY_MAX_SECONDS,
        )
        print(
            "  watcher warning: ChatGPT did not submit the composer after PID-targeted Return; "
            f"retrying in {delay:g}s",
            flush=True,
        )
        time.sleep(delay)
        attempt += 1


def paste_result_into_composer(
    app,
    root,
    text: str,
    *,
    send: bool,
    known_fingerprints: set[str] | None = None,
) -> DeliveryOutcome:
    if not send:
        set_composer_text(root, text)
        return DeliveryOutcome.SENT

    def interrupted(current_root) -> bool:
        if known_fingerprints is None:
            return False
        candidate = latest_valid_request(current_root)
        if candidate is None or candidate[2] in known_fingerprints:
            return False
        # The completed results remain durable in WatcherState. Clear only this
        # obsolete composer snapshot; the watcher will execute the newly visible
        # calls and rebuild a delivery containing every still-undelivered result.
        try:
            set_composer_text(current_root, "")
        except RuntimeError:
            pass
        return True

    # AXValue reaches Chromium/React asynchronously. Delivery retries are
    # interruptible: while Send is unavailable, a newly visible tool call wins
    # control immediately, but the completed results themselves are retained.
    attempt = 0
    while True:
        if interrupted(root):
            return DeliveryOutcome.INTERRUPTED

        set_composer_text(root, text)
        time.sleep(0.1)

        app, root = chatgpt_root()
        if interrupted(root):
            return DeliveryOutcome.INTERRUPTED

        send_buttons = find_elements(root, role="AXButton", description=SEND_DESCRIPTION)
        enabled = [b for b in send_buttons if probe.ax_attr(b, "AXEnabled") is not False]
        if enabled:
            composers = find_elements(root, role="AXTextArea", description=COMPOSER_DESCRIPTION)
            if not composers:
                raise RuntimeError("ChatGPT composer disappeared before submission")
            submit_composer_to_pid(app, composers[-1], text)
            return DeliveryOutcome.SENT

        delay = min(
            SEND_RETRY_INITIAL_SECONDS * (2**attempt),
            SEND_RETRY_MAX_SECONDS,
        )
        print(
            "  watcher warning: enabled Send button not found after setting composer AXValue; "
            f"retrying in {delay:g}s",
            flush=True,
        )

        if known_fingerprints is None:
            time.sleep(delay)
        else:
            remaining = delay
            while remaining > 0:
                step = min(0.1, remaining)
                time.sleep(step)
                remaining -= step
                app, root = chatgpt_root()
                if interrupted(root):
                    return DeliveryOutcome.INTERRUPTED

        attempt += 1
        app, root = chatgpt_root()


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

    state = WatcherState()

    # Preserve the current startup behavior for now: a request already visible
    # when PGC launches is considered pre-existing. A later recovery slice can
    # reconstruct unanswered calls from conversation history explicitly.
    _, root = chatgpt_root()
    existing = latest_valid_request(root)
    if existing is not None:
        state.seen_fingerprints.add(existing[2])

    too_long_visible = ui_contains_text_outside_conversation(root, MESSAGE_TOO_LONG_TEXT)
    handled_too_long_for: str | None = None

    print("  Press Ctrl-X to save the ChatGPT accessibility tree.", flush=True)
    with TerminalHotkeys() as hotkeys:
        while True:
            try:
                if hotkeys.read() == "\x18":
                    print(f"  accessibility dump: {save_accessibility_dump()}", flush=True)

                state.phase = WatcherPhase.SCANNING
                app, root = chatgpt_root()
                if dismiss_work_prompt(root):
                    time.sleep(0.2)
                    app, root = chatgpt_root()

                # A too-large submission was not actually delivered. Restore the
                # corresponding completed batches as compact pending deliveries;
                # any newly discovered calls will be executed before resubmission.
                current_too_long_visible = ui_contains_text_outside_conversation(root, MESSAGE_TOO_LONG_TEXT)
                if current_too_long_visible and not too_long_visible:
                    submission_key = state.last_submission_key()
                    if submission_key is not None and handled_too_long_for != submission_key:
                        handled_too_long_for = submission_key
                        state.restore_last_submission_compact()
                        print(
                            "  watcher warning: ChatGPT rejected tool results as too long; "
                            "queued a compact retry while retaining completed results",
                            flush=True,
                        )
                too_long_visible = current_too_long_visible

                candidate = latest_valid_request(root)
                if candidate is not None:
                    source, request, fingerprint = candidate
                    if fingerprint not in state.seen_fingerprints:
                        # A streaming code block can briefly become parseable before
                        # the assistant is done. Require the same request after the
                        # settling delay before treating it as conversation progress.
                        time.sleep(SETTLE_SECONDS)
                        _, settled_root = chatgpt_root()
                        settled = latest_valid_request(settled_root)
                        if settled is None or settled[2] != fingerprint:
                            continue

                        # A genuinely newer settled assistant tool-call turn means
                        # ChatGPT accepted the previous submission. It can no longer
                        # be the subject of a future delivery-error recovery.
                        state.acknowledge_last_submission()

                        # Claim before execution. Side-effecting calls are never run
                        # twice merely because delivery later fails or is interrupted.
                        state.seen_fingerprints.add(fingerprint)
                        state.phase = WatcherPhase.EXECUTING
                        print(color("tool calls", "1;35") + ":", flush=True)
                        result = execute_request(request, announce=True)
                        state.record_completed(fingerprint, request, result)
                        handled_too_long_for = None

                        # Rescan before delivery. If another request appeared while
                        # tools were running, execute it first and grow the pending
                        # delivery snapshot rather than sending a partial snapshot.
                        continue

                if state.has_pending_results():
                    state.phase = WatcherPhase.DELIVERING
                    rendered = state.render_pending()
                    app, root = chatgpt_root()
                    outcome = paste_result_into_composer(
                        app,
                        root,
                        rendered,
                        send=True,
                        known_fingerprints=state.seen_fingerprints,
                    )
                    if outcome is DeliveryOutcome.INTERRUPTED:
                        print(
                            "  delivery interrupted by new tool calls; retaining completed results",
                            flush=True,
                        )
                        continue

                    state.mark_submitted()
                    print(f"  {color('OK', '1;32')} sent results\n", flush=True)
                    continue

                time.sleep(POLL_SECONDS)
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
