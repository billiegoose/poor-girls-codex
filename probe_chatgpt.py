#!/usr/bin/env python3

import os
import subprocess
import sys
import textwrap
from datetime import datetime

try:
    import AppKit
    import Quartz
    import ApplicationServices as AS
except ImportError:
    print(
        textwrap.dedent(
            """
            Missing PyObjC.

            Install it with:

                python3 -m pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz pyobjc-framework-ApplicationServices

            Then run this script again.
            """
        ).strip(),
        file=sys.stderr,
    )
    sys.exit(2)


MAX_DEPTH = 16
MAX_NODES = 1000
MAX_TEXT = 180


def ax_call(fn, *args):
    """
    PyObjC signatures for AX 'Copy' APIs differ slightly across versions.
    Normalize them to (error, value).
    """
    try:
        result = fn(*args, None)
    except TypeError:
        result = fn(*args)

    if isinstance(result, tuple):
        if len(result) == 2:
            return result
        if len(result) == 1:
            return 0, result[0]

    return 0, result


def ax_attr(element, name):
    try:
        err, value = ax_call(AS.AXUIElementCopyAttributeValue, element, name)
        if err != 0:
            return None
        return value
    except Exception:
        return None


def ax_attribute_names(element):
    try:
        err, value = ax_call(AS.AXUIElementCopyAttributeNames, element)
        if err != 0 or value is None:
            return []
        return list(value)
    except Exception:
        return []


def ax_action_names(element):
    try:
        err, value = ax_call(AS.AXUIElementCopyActionNames, element)
        if err != 0 or value is None:
            return []
        return list(value)
    except Exception:
        return []


def printable(value):
    if value is None:
        return "None"

    # Avoid recursively exploding AX objects / collections.
    if isinstance(value, (list, tuple)):
        return f"<{type(value).__name__} len={len(value)}>"

    try:
        s = str(value)
    except Exception:
        return f"<{type(value).__name__}>"

    s = s.replace("\n", "\\n").replace("\r", "\\r")

    if len(s) > MAX_TEXT:
        s = s[: MAX_TEXT - 3] + "..."

    return repr(s)


INTERESTING_ATTRS = [
    "AXRole",
    "AXSubrole",
    "AXRoleDescription",
    "AXTitle",
    "AXDescription",
    "AXIdentifier",
    "AXHelp",
    "AXValue",
    "AXPlaceholderValue",
    "AXEnabled",
    "AXFocused",
    "AXSelected",
    "AXExpanded",
    "AXVisited",
    "AXDOMIdentifier",
    "AXDOMClassList",
    "AXURL",
    "AXPosition",
    "AXSize",
]


def describe_node(element):
    fields = []

    for attr in INTERESTING_ATTRS:
        value = ax_attr(element, attr)
        if value is None:
            continue

        rendered = printable(value)
        if rendered is not None:
            fields.append(f"{attr[2:] if attr.startswith('AX') else attr}={rendered}")

    actions = ax_action_names(element)
    if actions:
        fields.append("actions=" + repr(actions))

    children = ax_attr(element, "AXChildren")
    if children is not None:
        try:
            fields.append(f"children={len(children)}")
        except Exception:
            pass

    return fields


def scan_selector_candidates(root):
    lines = []
    node_count = 0
    max_nodes = 10000
    interesting_roles = {
        "AXButton",
        "AXTextArea",
        "AXTextField",
        "AXScrollArea",
        "AXWebArea",
        "AXLink",
        "AXPopUpButton",
    }
    interesting_text = (
        "copy",
        "send",
        "stop",
        "stay in chat",
        "toolcall",
        "code",
    )

    def visit(element, path):
        nonlocal node_count
        if node_count >= max_nodes:
            return
        node_count += 1

        role = str(ax_attr(element, "AXRole") or "")
        title = str(ax_attr(element, "AXTitle") or "")
        description = str(ax_attr(element, "AXDescription") or "")
        value = str(ax_attr(element, "AXValue") or "")
        identifier = str(ax_attr(element, "AXIdentifier") or "")
        haystack = " ".join((title, description, value, identifier)).lower()

        if role in interesting_roles or any(term in haystack for term in interesting_text):
            fields = describe_node(element)
            lines.append(f"{path}: " + " | ".join(fields))

        children = ax_attr(element, "AXChildren")
        if not children:
            return
        try:
            children = list(children)
        except Exception:
            return

        role_counts = {}
        for child in children:
            child_role = str(ax_attr(child, "AXRole") or "?")
            index = role_counts.get(child_role, 0)
            role_counts[child_role] = index + 1
            visit(child, f"{path}/{child_role}[{index}]")

    visit(root, "APP")
    lines.append("")
    lines.append(f"Selector scan visited {node_count} AX nodes (limit {max_nodes})")
    return lines


def dump_tree(root):
    lines = []
    node_count = 0

    # Recursive enough for diagnostics, but bounded against pathological AX trees.
    def visit(element, depth, path):
        nonlocal node_count

        if node_count >= MAX_NODES:
            return

        node_count += 1

        fields = describe_node(element)
        prefix = "  " * depth

        if fields:
            lines.append(f"{prefix}{path}: " + " | ".join(fields))
        else:
            lines.append(f"{prefix}{path}: <no readable attributes>")

        if depth >= MAX_DEPTH:
            lines.append(f"{prefix}  … depth limit reached")
            return

        children = ax_attr(element, "AXChildren")
        if not children:
            return

        try:
            children = list(children)
        except Exception:
            return

        for index, child in enumerate(children):
            if node_count >= MAX_NODES:
                lines.append(f"{prefix}  … node limit reached")
                return

            role = ax_attr(child, "AXRole")
            role = str(role) if role else "?"
            visit(child, depth + 1, f"{path}/{role}[{index}]")

    visit(root, 0, "APP")

    lines.append("")
    lines.append(f"AX nodes dumped: {node_count}")
    lines.append(f"Limits: depth={MAX_DEPTH}, nodes={MAX_NODES}")

    return lines


def trusted():
    try:
        options = {AS.kAXTrustedCheckOptionPrompt: True}
        return bool(AS.AXIsProcessTrustedWithOptions(options))
    except Exception:
        try:
            return bool(AS.AXIsProcessTrusted())
        except Exception:
            return False


def find_chatgpt_app():
    workspace = AppKit.NSWorkspace.sharedWorkspace()
    frontmost = workspace.frontmostApplication()

    def is_chatgpt(app):
        name = str(app.localizedName() or "")
        bundle = str(app.bundleIdentifier() or "")
        return name == "ChatGPT" or bundle == "com.openai.codex"

    if frontmost is not None and is_chatgpt(frontmost):
        return frontmost, True

    candidates = []

    for app in workspace.runningApplications():
        try:
            if is_chatgpt(app):
                candidates.append(app)
        except Exception:
            pass

    if not candidates:
        return None, False

    candidates.sort(
        key=lambda app: (
            str(app.bundleIdentifier() or "") != "com.openai.codex",
            str(app.localizedName() or "") != "ChatGPT",
        )
    )
    return candidates[0], False


def cg_window_diagnostics(pid):
    lines = ["", "=== CoreGraphics windows ==="]

    try:
        options = (
            Quartz.kCGWindowListOptionOnScreenOnly
            | Quartz.kCGWindowListExcludeDesktopElements
        )

        windows = Quartz.CGWindowListCopyWindowInfo(
            options,
            Quartz.kCGNullWindowID,
        )

        matching = [
            w
            for w in windows
            if int(w.get(Quartz.kCGWindowOwnerPID, -1)) == pid
        ]

        if not matching:
            lines.append("No on-screen CoreGraphics windows found for this PID.")
            return lines

        for i, window in enumerate(matching):
            lines.append(f"window[{i}]")
            lines.append(
                f"  number={window.get(Quartz.kCGWindowNumber)!r}"
            )
            lines.append(
                f"  owner={window.get(Quartz.kCGWindowOwnerName)!r}"
            )
            lines.append(
                f"  name={window.get(Quartz.kCGWindowName)!r}"
            )
            lines.append(
                f"  layer={window.get(Quartz.kCGWindowLayer)!r}"
            )
            lines.append(
                f"  alpha={window.get(Quartz.kCGWindowAlpha)!r}"
            )
            lines.append(
                f"  bounds={window.get(Quartz.kCGWindowBounds)!r}"
            )
            lines.append(
                f"  onscreen={window.get(Quartz.kCGWindowIsOnscreen)!r}"
            )

    except Exception as exc:
        lines.append(f"CoreGraphics enumeration failed: {exc!r}")

    return lines


def main():
    if not trusted():
        print(
            textwrap.dedent(
                """
                Accessibility permission has not been granted.

                macOS should have prompted you. If not, go to:

                  System Settings
                    → Privacy & Security
                    → Accessibility

                and enable the terminal application you are running this from
                (Terminal, iTerm2, etc.).

                Then run the script again.
                """
            ).strip(),
            file=sys.stderr,
        )
        sys.exit(3)

    app, was_frontmost = find_chatgpt_app()

    if app is None:
        print(
            "Couldn't find a running ChatGPT/OpenAI desktop application.",
            file=sys.stderr,
        )
        sys.exit(4)

    pid = int(app.processIdentifier())
    name = str(app.localizedName() or "")
    bundle = str(app.bundleIdentifier() or "")

    report = [
        "=== Poor Girl's Codex: ChatGPT Accessibility Probe ===",
        f"timestamp={datetime.now().isoformat()}",
        f"probe_pid={os.getpid()}",
        "",
        "=== Target application ===",
        f"name={name!r}",
        f"bundle_id={bundle!r}",
        f"pid={pid}",
        f"was_frontmost={was_frontmost}",
        f"active={bool(app.isActive())}",
        f"hidden={bool(app.isHidden())}",
    ]

    report.extend(cg_window_diagnostics(pid))

    app_element = AS.AXUIElementCreateApplication(pid)

    report += [
        "",
        "=== Application AX attributes ===",
        "available_attributes=" + repr(ax_attribute_names(app_element)),
        "available_actions=" + repr(ax_action_names(app_element)),
    ]

    windows = ax_attr(app_element, "AXWindows")

    if windows:
        try:
            windows = list(windows)
        except Exception:
            windows = []

        report.append(f"AXWindows count={len(windows)}")

        for i, window in enumerate(windows):
            report += [
                "",
                f"=== AX window {i} summary ===",
            ]

            for line in describe_node(window):
                report.append(line)

    else:
        report.append("AXWindows unavailable or empty.")

    focused_window = ax_attr(app_element, "AXFocusedWindow")

    report += [
        "",
        "=== Focus ===",
        "focused_window=" + printable(focused_window),
    ]

    focused_element = ax_attr(app_element, "AXFocusedUIElement")
    report.append("focused_ui_element=" + printable(focused_element))

    report += [
        "",
        "=== Selector candidates ===",
    ]
    report.extend(scan_selector_candidates(app_element))

    report += [
        "",
        "=== Full accessibility tree ===",
    ]

    report.extend(dump_tree(app_element))

    output = "\n".join(report) + "\n"

    print(output, end="")

    try:
        subprocess.run(
            ["pbcopy"],
            input=output,
            text=True,
            check=True,
        )
    except Exception as exc:
        print(f"\nWARNING: pbcopy failed: {exc}", file=sys.stderr)
        sys.exit(5)

    print(
        "\n[diagnostic report copied to clipboard]",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
