# ChatGPT Web Interface

## Status

The original two-tab POC succeeded against a real Chrome session. Poor Girl's Codex can attach to an already-running Chromium browser over CDP, observe multiple existing ChatGPT conversations in background tabs, execute tool calls independently, and route each result back to the conversation that emitted it.

`--interface chatgpt-web` now runs a continuous multi-session watcher rather than the one-shot POC.

## Usage

Start Chrome with a remote-debugging endpoint and a dedicated user-data directory. PGC does not launch or own the browser.

```text
poor_girls_codex --interface chatgpt-web
```

The default CDP endpoint is `http://127.0.0.1:9222`. Override it when needed:

```text
poor_girls_codex --interface chatgpt-web --cdp-url http://127.0.0.1:9333
```

Open one or more normal ChatGPT conversation URLs (`https://chatgpt.com/c/...`) in that browser. Paste the normal PGC bootstrap prompt into any conversation you want to use as a coding agent. The watcher discovers conversation tabs automatically; opening and closing tabs does not require restarting PGC.

Unlike the macOS Accessibility interface, the web watcher does not use the clipboard, mouse, keyboard focus, screen coordinates, or `page.bring_to_front()`.

## Architecture

```text
                         Poor Girl's Codex
                                 |
                         protocol / tools
                                 |
                    +------------+------------+
                    |                         |
             chatgpt-macos                chatgpt-web
                    |                         |
             macOS AX APIs             Playwright + CDP
                    |                         |
          ChatGPT desktop app          existing Chromium
                                              |
                              +---------------+---------------+
                              |               |               |
                         conversation A  conversation B  conversation C
                              |               |               |
                          WebSession       WebSession       WebSession
                              |               |               |
                          watcher state   watcher state   watcher state
                              +---------------+---------------+
                                              |
                                      local toolcall_lib
```

The unit of isolation is the ChatGPT conversation, identified by the UUID in `/c/<uuid>`. Each `WebSession` owns its request fingerprints, settling state, pending result queue, and delivery retry state. The page title is only a human-readable label and may change.

## Scheduler behavior

The watcher performs a round-robin loop across all currently open ChatGPT conversation tabs:

1. Discover current `chatgpt.com/c/...` pages and bind them by conversation UUID.
2. Prime newly discovered tabs so a tool call already visible before attachment is not accidentally replayed.
3. Inspect only the newest assistant turn in each conversation.
4. Parse and validate fenced JSON tool-call candidates with the normal PGC validator.
5. Require the same candidate to survive a settling interval before execution, avoiding partially streamed JSON.
6. Claim the request fingerprint before executing it so delivery failure cannot replay side effects.
7. Execute through the normal `toolcall_lib` path.
8. Queue the rendered result on that same `WebSession`.
9. Scan every session before delivering results, preserving fair observation of simultaneous model turns.
10. Submit each session's pending results only through that session's own page.
11. If ChatGPT is temporarily unable to accept a submission, retain that session's results and retry with exponential backoff without blocking or corrupting other sessions.
12. Discover newly opened conversation tabs and detach closed ones automatically.

Local tools are currently executed synchronously. The ChatGPT generations themselves remain concurrent in their browser tabs. Parallel local tool execution can be added later without changing the session identity/routing model.

## DOM boundary

Selectors are intentionally localized to `chatgpt_web.py`:

- assistant turns: `[data-message-author-role="assistant"]`
- assistant identity: `data-message-id`
- composer: `#prompt-textarea`
- send control: `button[data-testid="send-button"]`, with accessible-name fallback
- JSON candidates: `pre code`, then `pre`

The current live ChatGPT DOM exposes the composer as a contenteditable `DIV` with textbox semantics and exposes the send control only when text is present. Playwright's `fill()` and DOM button click work in background tabs.

## Safety properties

- Non-conversation ChatGPT pages are ignored.
- Host matching is exact (`chatgpt.com`), not a substring test.
- Existing visible tool calls are marked seen when a tab is first attached.
- Fingerprints include the assistant `data-message-id`, so identical JSON intentionally emitted in later turns is still a new request.
- A fingerprint is recorded before local execution.
- Pending results are stored per conversation and never in browser-global state.
- Delivery errors in one conversation do not discard another conversation's results.
- PGC detaches from Playwright without closing the user's browser.

## Remaining differences from the macOS watcher

The mature macOS path still has a few recovery features not yet mirrored in the web adapter, notably ChatGPT's explicit "message too long" recovery and manual accessibility-tree dumping. The web implementation instead keeps DOM/Playwright behavior contained in `chatgpt_web.py`; equivalent web diagnostics can be added when a concrete failure mode appears.

The old `run_two_tab_poc()` helper remains available in the module as a focused regression/smoke test, but it is no longer the normal CLI path.
