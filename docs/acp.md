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

## Thinking Effort

PGC exposes **Thinking Effort** in T3 Code's model options through ACP's `thought_level` configuration category: **Instant | Medium | High**. New ACP sessions default to **High**. Each session retains its own selection, including for its subagent conversations.

The selection is applied through ChatGPT's model-picker effort slider before each browser submission, including the initial prompt, follow-ups, and tool results. Changes made during generation take effect on the next submission. PGC verifies the selected label before sending; an unavailable or unsupported picker causes an error on prompt startup, or a delivery retry for tool results. This requires the ChatGPT UI with the three-position Instant/Medium/High slider.

ACP clients receive `configOptions` from `session/new` and can change the value without starting a browser:

```json
{"jsonrpc":"2.0","id":2,"method":"session/set_config_option","params":{"sessionId":"pgc-…","configId":"thinking_effort","value":"high"}}
```

The response contains the complete updated `configOptions`. Supported values are `instant`, `medium`, and `high`.

## Implementation and limitations

- Uses newline-delimited JSON-RPC 2.0 over stdio, with protocol negotiation down to ACP v1. **No ACP v2-only capabilities are advertised.**
- Supports `initialize`, `session/new`, `session/resume`, `session/close`, `session/set_config_option`, `session/prompt`, `session/cancel`, and `session/update` notifications for tool calls and final assistant text. Multiple sessions in one ACP process have separate browser conversations and workspace directories. Browser work is serialized on one Playwright thread.
- ACP `session/new` stores the session ID, workspace, routing identity, and thinking effort in a user-private, atomic session record; the ChatGPT conversation ID is saved as soon as the first browser conversation becomes persistent. `session/resume` is advertised and restores a session on a fresh ACP subprocess. The next prompt reopens **the original ChatGPT conversation** and continues there, instead of creating a new one. This resolves T3's `ACP agent does not advertise session/load or session/resume support` activation error for sessions created with persistence enabled. T3 checkpoint rollback may impose additional requirements that remain unsupported; this is not full checkpoint-rollback implementation. The adapter does not fabricate a replacement conversation if the original cannot be reopened.
- On macOS, records live under `~/Library/Application Support/poor-girls-codex/acp-sessions/`; on other platforms they use `$XDG_STATE_HOME/poor-girls-codex/acp-sessions/` (or `~/.local/state` by default). Set `PGC_ACP_SESSION_DIR` to an absolute path to override. Records contain identifiers and a workspace path, **not** prompts or tool results. They are written with owner-only file permissions and persist across ACP process restarts.
- `session/close` closes ACP-owned Chrome tabs, including active subagent tabs, but retains the persistent session record for future resumption. Clean stdin EOF and SIGTERM also close owned tabs before detaching Playwright. Tabs that have navigated away from their original ACP conversation and pre-existing/user-owned tabs are not closed. Closing a tab never deletes its ChatGPT conversation. A hard kill or crash may leave browser tabs open.
- Sessions created by earlier PGC versions, before durable session records were written, **cannot be automatically resumed** from their old `pgc-...` IDs. T3's initial thread activation may fall back to a new session, but restoring an old checkpoint still requires its original native session. Start a new T3 thread for reliable future restoration rather than treating a fresh ChatGPT conversation as the original history. Refresh/restart T3's ACP provider process so the newly advertised `session/resume` capability is discovered.
- If a resumed ChatGPT conversation has an unresolved tool call, PGC refuses to append another prompt rather than risk replaying side-effectful work. Inspect the original ChatGPT conversation or start a new T3 thread to recover safely.
- `session/load` (replaying the original transcript to the client), native session listing, and switching the underlying ChatGPT model are not implemented. `session/resume` preserves the ChatGPT conversation but does not replay messages to T3; T3 maintains its own thread transcript.
- Text prompts only. Image/audio/resource prompt blocks are rejected. PGC's *model-to-local-tool* image attachments still work through the normal browser workflow.
- T3's supplied MCP servers are not connected by this adapter. PGC uses its existing local tool set, including shell commands; it does **not** implement T3 permissions or sandboxing. Its tools have the permissions of the local PGC process. Configure the launcher only for workspaces you trust.
- New ChatGPT conversations may temporarily use a `local-chatgpt:` URL before receiving a persistent ID. PGC waits up to two minutes for both the persistent ID and the assistant turn to start, and ACP cancellation or shutdown can interrupt that wait. If the deadline expires, the error reports which readiness condition was missing. Browser-side network failures can still prevent startup.
- Cancellation attempts to stop ChatGPT's current generation and reports `cancelled`; a shell tool already running cannot be interrupted or rolled back. This is best effort.
- ACP tool activity includes descriptive titles (the actual executable for `run`, or `bash` for shell scripts), visible arguments, exit status, stdout/stderr, and failure diagnostics. Nonzero shell exit codes are displayed as failed calls. Large previews are truncated in the activity pane without changing the results returned to ChatGPT. Environment variables supplied with commands and source-code replacement bodies are omitted from the activity pane.
- Completed assistant responses are sent as Markdown reconstructed from ChatGPT's rendered message: headings, emphasis, links, lists, tables, quotations, and fenced code blocks are preserved when available. The completion decision still uses the verified plain-text response; if rich-content extraction is unavailable or fails, the adapter sends that plain text instead. This is best-effort Markdown conversion rather than a lossless copy of arbitrary interactive HTML components.
- The adapter does not yet stream intermediate assistant prose token-by-token. T3 sees tool activity and the completed assistant response.

Run protocol and regression tests:

```sh
uv run python -m unittest discover -s tests -p 'test_*.py'
```

A protocol-only test does not require a browser; an actual T3 prompt needs a working authenticated ChatGPT/CDP session.
