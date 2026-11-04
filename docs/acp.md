# ACP (experimental)

Poor Girl's Codex can act as an **Agent Client Protocol v1** agent over stdio. This lets ACP clients such as T3 Code drive ChatGPT through PGC's existing Playwright/CDP browser harness. T3 Code prefers ACP v2, but it can negotiate v1 with an agent that supports only v1.

## T3 Code setup

1. Ensure ChatGPT is signed in to the Chrome profile that PGC uses. The browser must be available over CDP (by default `http://127.0.0.1:9222`). As in ordinary PGC operation, the backend may launch its dedicated Chrome profile if needed.
2. In **T3 Code → Settings → Providers → Add provider → Show more → Local ACP command**, configure:
   - **Executable:** the **absolute path** to the `poor_girls_codex` launcher in this checkout, for example `/Users/you/code/poor-girls-codex/poor_girls_codex`
   - **Arguments:** `acp` (one argument)
   - **Display name:** `Poor Girl's Codex`
3. Create a T3 thread against your target workspace. T3 supplies that workspace as ACP `cwd`. PGC opens one ChatGPT conversation per ACP session and executes its tools relative to that workspace.

To use a different Chrome CDP endpoint, add `--cdp-url` and the URL as two additional arguments. The standard `watch` mode is unaffected by the `acp` mode.

## Implementation and limitations

- Uses newline-delimited JSON-RPC 2.0 over stdio, with protocol negotiation down to ACP v1. **No ACP v2-only capabilities are advertised.**
- Supports `initialize`, `session/new`, `session/prompt`, `session/cancel`, and `session/update` notifications for tool calls and final assistant text. Multiple sessions in one ACP process have separate browser conversations and workspace directories. Browser work is serialized on one Playwright thread.
- Sessions are in-memory for the lifetime of the ACP process. `session/close` is supported and closes ACP-owned Chrome tabs, including any active subagent tabs. Clean stdin EOF and SIGTERM also close remaining ACP-owned tabs before detaching Playwright. Tabs that have navigated away from their original ACP conversation are left alone, as are all pre-existing/user-owned tabs. Closing a tab does not delete its ChatGPT conversation. A hard kill (`SIGKILL`) or process crash cannot run cleanup; tabs orphaned by earlier adapter versions are not automatically collected.
- `session/load`, `session/resume`, native session listing, and configuration/model pickers are not yet implemented or advertised. T3 can create new threads, but cannot restore the agent's conversation if the ACP subprocess exits.
- Text prompts only. Image/audio/resource prompt blocks are rejected. PGC's *model-to-local-tool* image attachments still work through the normal browser workflow.
- T3's supplied MCP servers are not connected by this adapter. PGC uses its existing local tool set, including shell commands; it does **not** implement T3 permissions, sandboxing, or model selection. Its tools have the permissions of the local PGC process. Configure the launcher only for workspaces you trust.
- New ChatGPT conversations may temporarily use a `local-chatgpt:` URL before receiving a persistent ID. PGC waits up to two minutes for both the persistent ID and the assistant turn to start, and ACP cancellation or shutdown can interrupt that wait. If the deadline expires, the error reports which readiness condition was missing. Browser-side network failures can still prevent startup.
- Cancellation attempts to stop ChatGPT's current generation and reports `cancelled`; a shell tool already running cannot be interrupted or rolled back. This is best effort.
- The adapter does not yet stream intermediate assistant prose token-by-token. T3 sees tool activity and the completed assistant response.

Run protocol and regression tests:

```sh
uv run python -m unittest discover -s tests -p 'test_*.py'
```

A protocol-only test does not require a browser; an actual T3 prompt needs a working authenticated ChatGPT/CDP session.
