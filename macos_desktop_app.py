from __future__ import annotations

import subprocess
import time
from typing import Any, Iterator

import ApplicationServices as AS
import Quartz

import probe_chatgpt as probe


COMPOSER_DESCRIPTION = "Message ChatGPT"
SEND_DESCRIPTION = "Send"
RETURN_RETRY_INITIAL_SECONDS = 0.25
RETURN_RETRY_MAX_SECONDS = 8.0


class MacOSDesktopApp:
    """ChatGPT macOS desktop-app frontend implemented with Accessibility APIs."""

    name = "macos_desktop_app"

    def trusted(self) -> bool:
        return bool(probe.trusted())

    def children_of(self, element) -> list[Any]:
        children = probe.ax_attr(element, "AXChildren")
        if not children:
            return []
        try:
            return list(children)
        except Exception:
            return []

    def walk(self, root) -> Iterator[Any]:
        stack = [root]
        while stack:
            element = stack.pop()
            yield element
            stack.extend(reversed(self.children_of(element)))

    def find_elements(
        self,
        root,
        *,
        role: str | None = None,
        description: str | None = None,
    ) -> list[Any]:
        matches = []
        for element in self.walk(root):
            if role is not None and str(probe.ax_attr(element, "AXRole") or "") != role:
                continue
            if description is not None and str(probe.ax_attr(element, "AXDescription") or "") != description:
                continue
            matches.append(element)
        return matches

    def root(self):
        app, _ = probe.find_chatgpt_app()
        if app is None:
            raise RuntimeError("ChatGPT is not running")
        pid = int(app.processIdentifier())
        return app, AS.AXUIElementCreateApplication(pid)

    def press(self, element) -> None:
        error = AS.AXUIElementPerformAction(element, AS.kAXPressAction)
        if error != 0:
            raise RuntimeError(f"AXPress failed with error {error}")

    def clipboard_write(self, text: str) -> None:
        # Clipboard use is intentionally limited to the startup convenience prompt.
        # Toolcall detection and extraction use Accessibility directly.
        subprocess.run(["pbcopy"], input=text, text=True, check=True)

    def save_debug_dump(self, exc: BaseException | None = None) -> str:
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
            _, root = self.root()
            lines.extend(probe.dump_tree(root))
        except Exception as dump_exc:
            lines.append(f"<unable to dump ChatGPT accessibility tree: {dump_exc}>")

        with open(filename, "w", encoding="utf-8") as output_file:
            output_file.write("\n".join(lines) + "\n")
        return filename

    def ui_contains_text(self, root, needle: str) -> bool:
        wanted = needle.casefold()
        for node in self.walk(root):
            for attr in ("AXValue", "AXTitle", "AXDescription", "AXHelp"):
                value = probe.ax_attr(node, attr)
                if value is not None and wanted in str(value).casefold():
                    return True
        return False

    def ui_contains_text_outside_conversation(self, root, needle: str) -> bool:
        """Find UI text while ignoring transcript content that may quote it verbatim."""
        wanted = needle.casefold()
        try:
            conversation = self.conversation_group(root)
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
            stack.extend(reversed(self.children_of(node)))
        return False

    def static_text(self, element) -> str:
        pieces = []
        for node in self.walk(element):
            if str(probe.ax_attr(node, "AXRole") or "") != "AXStaticText":
                continue
            value = probe.ax_attr(node, "AXValue")
            if value is not None:
                pieces.append(str(value))
        return "".join(pieces)

    def conversation_group(self, root):
        best = None
        best_score = 0
        for node in self.walk(root):
            children = self.children_of(node)
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

    def latest_assistant_content(self, root):
        children = self.children_of(self.conversation_group(root))
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

    def latest_assistant_json_candidates(self, root) -> list[str]:
        content = self.latest_assistant_content(root)
        candidates = []

        # Chromium exposes syntax-highlighted code as many AXStaticText tokens.
        # Return candidate group text to the protocol layer, which decides whether
        # any candidate is valid Poor Girl's Codex JSON.
        for node in self.walk(content):
            if str(probe.ax_attr(node, "AXRole") or "") != "AXGroup":
                continue
            source = self.static_text(node).strip()
            if source:
                candidates.append(source)
        return candidates

    def set_composer_text(self, root, text: str):
        composers = self.find_elements(
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

    def can_submit(self, root) -> bool:
        send_buttons = self.find_elements(root, role="AXButton", description=SEND_DESCRIPTION)
        return any(probe.ax_attr(button, "AXEnabled") is not False for button in send_buttons)

    def submit_composer(self, app, root, expected_text: str) -> None:
        composers = self.find_elements(root, role="AXTextArea", description=COMPOSER_DESCRIPTION)
        if not composers:
            raise RuntimeError("ChatGPT composer disappeared before submission")
        self._submit_composer_to_pid(app, composers[-1], expected_text)

    def _submit_composer_to_pid(self, app, composer, expected_text: str) -> None:
        # Keep input scoped to ChatGPT rather than the global HID stream. Setting
        # AXFocused on an inactive app does not activate it, and CGEventPostToPid
        # sends Return only to ChatGPT instead of hijacking the user's keyboard.
        pid = int(app.processIdentifier())
        attempt = 0

        while True:
            # Re-check before every retry. If the previous Return eventually took
            # effect after our confirmation timeout, do not send another Return.
            app, root = self.root()
            composers = self.find_elements(
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

            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                _, root = self.root()
                composers = self.find_elements(
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

    def dismiss_work_prompt(self, root) -> bool:
        for node in self.walk(root):
            if str(probe.ax_attr(node, "AXRole") or "") != "AXButton":
                continue
            text = " ".join(
                str(probe.ax_attr(node, attr) or "")
                for attr in ("AXDescription", "AXTitle", "AXValue")
            ).strip().lower()
            if "stay in chat" in text:
                self.press(node)
                return True
        return False
